"""
scraper/mystocks.py — Scrapes live.mystocks.co.ke/m/ for NSE stock data.

Strategy:
  • Uses the /m/ (mobile) version — lightweight HTML, no JS required.
  • /m/pricelist  → full market table (all tickers in one request)
  • /m/stock=XXXX → individual stock page for deeper data + dividends

The mobile site is plain server-rendered HTML, so BeautifulSoup + requests
is sufficient; no Selenium/Playwright needed.
"""

import re
import time
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup

from scraper.groq_client import validate_quote_data, validate_dividend_data

logger = logging.getLogger(__name__)

BASE_URL  = "https://live.mystocks.co.ke/m"
HEADERS   = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 11; Pixel 5) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/90.0.4430.91 Mobile Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
REQUEST_TIMEOUT = 15   # seconds
POLITE_DELAY    = 2    # seconds between per-stock requests


# ─── Data containers ─────────────────────────────────────────────────────────

@dataclass
class QuoteData:
    ticker:         str
    name:           str           = ""
    sector:         str           = ""
    price:          Optional[float] = None
    change:         Optional[float] = None
    change_pct:     Optional[float] = None
    open_price:     Optional[float] = None
    high:           Optional[float] = None
    low:            Optional[float] = None
    volume:         Optional[int]   = None
    previous_close: Optional[float] = None
    eps:            Optional[float] = None
    pe_ratio:       Optional[float] = None
    dividend:       Optional[float] = None
    dividend_yield: Optional[float] = None
    book_value:     Optional[float] = None
    market_cap:     Optional[float] = None
    shares_issued:  Optional[float] = None
    scraped_at:     datetime        = field(default_factory=datetime.utcnow)


@dataclass
class DividendData:
    ticker:      str
    amount:      float
    ex_date:     Optional[str] = None
    pay_date:    Optional[str] = None
    note:        str           = ""


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get(url: str, session: requests.Session, retries: int = 3) -> Optional[BeautifulSoup]:
    """GET a URL with retries; return BeautifulSoup or None on failure."""
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as exc:
            logger.warning("Attempt %d/%d failed for %s: %s", attempt, retries, url, exc)
            if attempt < retries:
                time.sleep(2 ** attempt)   # exponential back-off
    return None


def _parse_float(text: str) -> Optional[float]:
    """Clean KES-formatted numbers like '29.50', '1,234.56', '-' → float."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", text.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int(text: str) -> Optional[int]:
    v = _parse_float(text)
    return int(v) if v is not None else None


def _parse_date(text: str) -> Optional[str]:
    """Try to parse dates in various formats; return ISO string or None."""
    text = text.strip()
    for fmt in ("%d %b %Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ─── Pricelist scraper ────────────────────────────────────────────────────────

def scrape_pricelist(session: requests.Session) -> dict[str, QuoteData]:
    """
    Scrape /m/pricelist — returns a dict of ticker → QuoteData with
    basic price / change data for ALL listed NSE counters.
    This is the fast bulk pass; use scrape_stock() for deeper data.
    """
    url  = f"{BASE_URL}/pricelist"
    soup = _get(url, session)
    if not soup:
        logger.error("Failed to fetch pricelist")
        return {}

    quotes: dict[str, QuoteData] = {}
    table = soup.find("table")
    if not table:
        logger.warning("No table found on pricelist page — site structure may have changed")
        return quotes

    rows = table.find_all("tr")
    for row in rows[1:]:   # skip header
        cols = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cols) < 4:
            continue

        # Typical columns: Name | Price | Change | %Change | Volume | ...
        # Column positions can vary; we try to be resilient.
        try:
            name       = cols[0]
            price      = _parse_float(cols[1])
            change     = _parse_float(cols[2]) if len(cols) > 2 else None
            change_pct = _parse_float(cols[3]) if len(cols) > 3 else None
            volume     = _parse_int(cols[4])   if len(cols) > 4 else None

            # Extract ticker from the row's link  (<a href="/m/stock=SCOM">)
            link = row.find("a", href=re.compile(r"stock="))
            if link:
                ticker = link["href"].split("stock=")[-1].upper().strip()
            else:
                # Fallback: derive ticker from name column link text
                ticker = re.sub(r"\s+", "", name[:6]).upper()

            if ticker:
                quotes[ticker] = QuoteData(
                    ticker     = ticker,
                    name       = name,
                    price      = price,
                    change     = change,
                    change_pct = change_pct,
                    volume     = volume,
                )
        except (IndexError, ValueError) as exc:
            logger.debug("Skipping malformed row: %s", exc)

    logger.info("Pricelist scraped: %d counters found", len(quotes))
    return quotes


# ─── Individual stock page scraper ───────────────────────────────────────────

def scrape_stock(ticker: str, session: requests.Session) -> tuple[QuoteData, list[DividendData]]:
    """
    Scrape /m/stock=TICKER — returns (QuoteData, [DividendData]).
    Provides richer data: OHLC, EPS, P/E, dividend, book value, etc.
    """
    url  = f"{BASE_URL}/stock={ticker}"
    soup = _get(url, session)
    if not soup:
        logger.error("Failed to fetch stock page for %s", ticker)
        return QuoteData(ticker=ticker), []

    q = QuoteData(ticker=ticker)

    # ── Company name & sector ─────────────────────────────────────────────
    title_el = soup.find("h1") or soup.find("h2")
    if title_el:
        q.name = title_el.get_text(strip=True)

    sector_el = soup.find(string=re.compile(r"Sector", re.I))
    if sector_el:
        parent = sector_el.parent
        q.sector = parent.get_text(strip=True).replace("Sector:", "").replace("Sector", "").strip()

    # ── Current price block ───────────────────────────────────────────────
    # mystocks mobile shows the price prominently; try several selectors
    price_candidates = [
        soup.find(class_=re.compile(r"price|quote|last", re.I)),
        soup.find("span", string=re.compile(r"^\d[\d,\.]+$")),
    ]
    for el in price_candidates:
        if el:
            val = _parse_float(el.get_text(strip=True))
            if val and val > 0:
                q.price = val
                break

    # ── Key stats table ───────────────────────────────────────────────────
    # The mobile page renders stats as a <table> or definition list.
    # We build a flat label→value map then extract what we need.
    stats: dict[str, str] = {}

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True).lower()
                value = cells[1].get_text(strip=True)
                if label:
                    stats[label] = value

    # Also check definition-list style layouts
    for dl in soup.find_all(["dl", "ul"]):
        items = dl.find_all(["dt", "li"])
        for item in items:
            text = item.get_text(separator="|", strip=True)
            if "|" in text:
                lbl, _, val = text.partition("|")
                stats[lbl.lower().strip()] = val.strip()

    def _stat(*keys) -> Optional[str]:
        for k in keys:
            for sk in stats:
                if k in sk:
                    return stats[sk]
        return None

    q.price          = q.price or _parse_float(_stat("last", "price", "close"))
    q.open_price     = _parse_float(_stat("open"))
    q.high           = _parse_float(_stat("high"))
    q.low            = _parse_float(_stat("low"))
    q.previous_close = _parse_float(_stat("prev", "previous"))
    q.volume         = _parse_int(_stat("volume", "vol"))
    q.change         = _parse_float(_stat("change"))
    q.change_pct     = _parse_float(_stat("% change", "percent", "chg%"))
    q.eps            = _parse_float(_stat("eps", "earning per share"))
    q.pe_ratio       = _parse_float(_stat("p/e", "pe ratio", "price/earn"))
    q.dividend       = _parse_float(_stat("dividend", "dps", "div per share"))
    q.dividend_yield = _parse_float(_stat("yield", "div yield"))
    q.book_value     = _parse_float(_stat("book value", "nav", "net asset"))
    q.market_cap     = _parse_float(_stat("market cap", "mktcap"))
    q.shares_issued  = _parse_float(_stat("shares issued", "shares in issue"))

    # ── Dividend events ───────────────────────────────────────────────────
    dividends: list[DividendData] = []

    # Look for dividend history table (common on mystocks stock pages)
    div_sections = soup.find_all(string=re.compile(r"dividend", re.I))
    for ds in div_sections:
        section = ds.find_parent("table") or ds.find_parent("section")
        if not section:
            continue
        for row in section.find_all("tr")[1:]:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) >= 2:
                amount = _parse_float(cols[0])
                if amount and amount > 0:
                    dividends.append(DividendData(
                        ticker   = ticker,
                        amount   = amount,
                        ex_date  = _parse_date(cols[1]) if len(cols) > 1 else None,
                        pay_date = _parse_date(cols[2]) if len(cols) > 2 else None,
                        note     = " | ".join(cols[3:]) if len(cols) > 3 else "",
                    ))

    # If no table found but we scraped a dividend value, create a single event
    if not dividends and q.dividend:
        dividends.append(DividendData(ticker=ticker, amount=q.dividend))

    logger.debug("Scraped %s: price=%.2f div=%s", ticker,
                 q.price or 0, q.dividend)
    return q, dividends


# ─── Data Validation with Groq ────────────────────────────────────────────────

def validate_quote_with_groq(ticker: str, q: QuoteData) -> QuoteData:
    """
    Validate and optionally clean quote data using Groq LLM.
    If validation fails, logs a warning but returns original data.
    """
    try:
        quote_dict = {
            "name": q.name,
            "sector": q.sector,
            "price": q.price,
            "dividend": q.dividend,
            "change_pct": q.change_pct,
        }
        is_valid, cleaned, warnings = validate_quote_data(ticker, quote_dict)
        
        if not is_valid:
            logger.warning("Quote validation failed for %s (keeping original). Warnings: %s",
                          ticker, "; ".join(warnings))
            return q
        
        # Apply cleaned values if they are not None
        if cleaned.get("name"):
            q.name = cleaned["name"]
        if cleaned.get("sector"):
            q.sector = cleaned["sector"]
        if cleaned.get("price") is not None:
            q.price = cleaned["price"]
        if cleaned.get("dividend") is not None:
            q.dividend = cleaned["dividend"]
        if cleaned.get("change_pct") is not None:
            q.change_pct = cleaned["change_pct"]
        
        return q
    except Exception as exc:
        logger.debug("Groq validation skipped for %s: %s", ticker, exc)
        return q


def validate_dividend_with_groq(ticker: str, d: DividendData) -> Optional[DividendData]:
    """
    Validate dividend data using Groq LLM.
    Returns None if validation fails, otherwise returns (possibly cleaned) dividend data.
    """
    try:
        div_dict = {
            "amount": d.amount,
            "ex_date": d.ex_date,
            "pay_date": d.pay_date,
        }
        is_valid, cleaned, warnings = validate_dividend_data(ticker, div_dict)
        
        if not is_valid:
            logger.warning("Dividend validation failed for %s (skipping). Warnings: %s",
                          ticker, "; ".join(warnings))
            return None
        
        # Apply cleaned values
        if cleaned.get("amount") is not None:
            d.amount = cleaned["amount"]
        if cleaned.get("ex_date"):
            d.ex_date = cleaned["ex_date"]
        
        return d
    except Exception as exc:
        logger.debug("Groq dividend validation skipped for %s: %s", ticker, exc)
        return d


# ─── Batch scraper ────────────────────────────────────────────────────────────

def scrape_watchlist(
    tickers: list[str],
    full_detail: bool = True,
    delay: float = POLITE_DELAY,
    validate_with_groq: bool = False,
) -> tuple[list[QuoteData], list[DividendData]]:
    """
    Scrape a list of tickers.
    1. First does a pricelist pass for bulk prices.
    2. If full_detail=True, augments with per-stock detail pages.
    3. Optionally validates data with Groq LLM.
    Returns (quotes, dividends).
    """
    session = requests.Session()
    all_quotes:    list[QuoteData]    = []
    all_dividends: list[DividendData] = []

    # Bulk pricelist pass
    bulk = scrape_pricelist(session)

    for ticker in tickers:
        ticker = ticker.upper().strip()

        if full_detail:
            time.sleep(delay)   # be polite
            q, divs = scrape_stock(ticker, session)

            # Fill price from pricelist if stock page didn't yield one
            if q.price is None and ticker in bulk:
                q.price      = bulk[ticker].price
                q.change     = bulk[ticker].change
                q.change_pct = bulk[ticker].change_pct
                q.volume     = bulk[ticker].volume

            # Validate with Groq if enabled
            if validate_with_groq:
                q = validate_quote_with_groq(ticker, q)
                validated_divs = []
                for d in divs:
                    validated_d = validate_dividend_with_groq(ticker, d)
                    if validated_d:
                        validated_divs.append(validated_d)
                divs = validated_divs

            all_quotes.append(q)
            all_dividends.extend(divs)
        else:
            if ticker in bulk:
                all_quotes.append(bulk[ticker])
            else:
                logger.warning("Ticker %s not found in pricelist", ticker)

    return all_quotes, all_dividends