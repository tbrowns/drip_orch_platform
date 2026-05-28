"""
NSE Scraper - live.mystocks.co.ke
Scrapes stock quotes and corporate announcements (dividend calendar) from the Kenyan stock market.
"""
import logging
import re
import time
from datetime import datetime, UTC
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup

from db.models import StockQuote, Announcement

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────
BASE_STOCK_URL = "https://live.mystocks.co.ke/m/stock="
CALENDAR_URL = "https://live.mystocks.co.ke/m/calendar"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

ALL_TICKERS = [
    "ABSA", "GLD", "ALP", "AMAC", "ARM", "BAMB", "BKG", "BOC", "BRIT",
    "BAT", "CGEN", "CARB", "CTUM", "CIC", "COOP", "CRWN", "DCON", "DTK",
    "EGAD", "EABL", "CABL", "PORT", "EQTY", "EVRD", "XPRS", "FTGH", "HFCK",
    "HAFR", "HBE", "IMH", "JUB", "KUKZ", "KAPC", "KCB", "KQ", "KEGN",
    "KPC", "KPLC", "KNRE", "KURV", "LAPR", "LBTY", "LIMT", "LKL", "MSC",
    "NBV", "NSE", "NMG", "NCBA", "OCH", "SCOM", "SMER", "SLAM", "SASN",
    "SMWF", "SKL", "SBIC", "SCBK", "SGL", "TOTL", "TPSE", "TCL", "UCHM",
    "UMME", "UNGA", "WTK", "SCAN",
]


@dataclass
class StockData:
    """Holds all scraped data for a single NSE stock quote page."""
    ticker: str
    name: Optional[str] = None
    sector: Optional[str] = None
    previous: Optional[str] = None
    open: Optional[str] = None
    average: Optional[str] = None
    deals: Optional[str] = None
    volume: Optional[str] = None
    turnover: Optional[str] = None
    day_range: Optional[str] = None
    week_52_range: Optional[str] = None
    average_volume: Optional[str] = None
    beta: Optional[str] = None
    shares_issued: Optional[str] = None
    year_end: Optional[str] = None
    par_value: Optional[str] = None
    profile: Optional[str] = None
    error: Optional[str] = None


@dataclass
class AnnouncementData:
    """Holds a single corporate announcement from the calendar page."""
    date: str
    ticker: str
    company: str
    event_type: str
    amount_kes: Optional[str] = None
    dividend_type: Optional[str] = None
    description: str = ""


class NSEScraper:
    """Scrapes stock pages and announcements from live.mystocks.co.ke."""

    def __init__(self, delay: float = 2.0, logger: Optional[logging.Logger] = None):
        self.delay = delay
        self._session = requests.Session()
        self._session.headers.update(HEADERS)
        self.logger = logger or logging.getLogger(self.__class__.__name__)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._session.close()

    def close(self):
        self._session.close()

    def get_stock(self, ticker: str) -> StockData:
        ticker = ticker.upper().strip()
        data = StockData(ticker=ticker)
        url = f"{BASE_STOCK_URL}{ticker}"

        try:
            resp = self._session.get(url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            data.error = str(exc)
            self.logger.error("Failed to fetch %s: %s", ticker, exc)
            return data

        self._parse_stock(BeautifulSoup(resp.text, "html.parser"), data)
        return data

    def get_stocks(self, tickers: list[str], delay: Optional[float] = None) -> list[StockData]:
        pause = delay if delay is not None else self.delay
        results: list[StockData] = []
        for index, ticker in enumerate(tickers):
            results.append(self.get_stock(ticker))
            if index < len(tickers) - 1:
                time.sleep(pause)
        return results

    def get_all_stocks(self, **kwargs) -> list[StockData]:
        return self.get_stocks(ALL_TICKERS, **kwargs)

    def get_announcements(self) -> list[AnnouncementData]:
        try:
            resp = self._session.get(CALENDAR_URL, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            self.logger.error("Calendar fetch failed: %s", exc)
            return []

        return self._parse_calendar(BeautifulSoup(resp.text, "html.parser"))

    def _parse_stock(self, soup: BeautifulSoup, data: StockData) -> None:
        tables = soup.find_all("table", class_="shareInfo")

        h1 = soup.find("h1")
        if h1:
            data.name = h1.get_text(strip=True).split(" (")[0].strip()

        sector_div = soup.find("div", id="sector")
        if sector_div:
            strong = sector_div.find("strong")
            if strong:
                strong.decompose()
            lines = [
                l.strip()
                for l in sector_div.get_text(separator="\n", strip=True).splitlines()
                if l.strip()
            ]
            if lines:
                data.sector = lines[0]

        if tables:
            for row in tables[0].find_all("tr"):
                ths = row.find_all("th")
                tds = row.find_all("td")
                if len(ths) == 2 and len(tds) == 2:
                    pairs = list(zip(ths, tds))
                    label_map = {
                        "Previous": "previous",
                        "Open": "open",
                        "Average": "average",
                        "Deals": "deals",
                        "Volume": "volume",
                        "Turnover": "turnover",
                        "Day": "day_range",
                        "52-week": "week_52_range",
                    }
                    for th, td in pairs:
                        label = th.get_text(strip=True).rstrip(":")
                        value = td.get_text(strip=True)
                        if label in label_map:
                            setattr(data, label_map[label], value)

        if len(tables) >= 2:
            label_map = {
                "Average Volume": "average_volume",
                "BETA": "beta",
                "Shares Issued": "shares_issued",
                "Year End": "year_end",
                "Par Value": "par_value",
            }
            for row in tables[1].find_all("tr"):
                ths = row.find_all("th")
                tds = row.find_all("td")
                if ths and tds:
                    label = ths[0].get_text(strip=True).rstrip(":").replace("\xa0", " ").strip()
                    value = tds[0].get_text(strip=True)
                    if label in label_map:
                        setattr(data, label_map[label], value)

        profile_h2 = soup.find("h2", string=lambda t: t and "Profile" in t)
        if profile_h2:
            paragraphs = []
            for sibling in profile_h2.find_next_siblings():
                if sibling.name == "h2":
                    break
                if sibling.name == "p":
                    text = sibling.get_text(" ", strip=True)
                    if text:
                        paragraphs.append(text)
            if paragraphs:
                data.profile = " ".join(paragraphs)

    def _parse_calendar(self, soup: BeautifulSoup) -> list[AnnouncementData]:
        main = soup.find("div", id="main")
        if not main:
            return []

        results: list[AnnouncementData] = []
        current_date = ""

        for tag in main.children:
            if not hasattr(tag, "name"):
                continue
            if tag.name == "h3":
                current_date = tag.get_text(strip=True)
            elif tag.name == "div" and current_date:
                a_tag = tag.find("a")
                if not a_tag:
                    continue

                ticker = a_tag.get_text(strip=True)
                full_text = tag.get_text(" ", strip=True)
                rest = full_text[len(ticker):].strip().lstrip()
                if ":" in rest:
                    company_part, event_part = rest.split(":", 1)
                    company = company_part.strip()
                    description = event_part.strip()
                else:
                    company = rest.strip()
                    description = ""

                event_type, amount_kes, dividend_type = self._parse_event(description)
                results.append(AnnouncementData(
                    date=current_date,
                    ticker=ticker,
                    company=company,
                    event_type=event_type,
                    amount_kes=amount_kes,
                    dividend_type=dividend_type,
                    description=description,
                ))
        return results

    def _parse_event(self, description: str) -> tuple[str, Optional[str], Optional[str]]:
        event_type = "Other"
        amount_kes = None
        dividend_type = None

        desc_lower = description.lower()
        if desc_lower.startswith("payment"):
            event_type = "Payment"
        elif desc_lower.startswith("book closure"):
            event_type = "Book closure"
        elif desc_lower.startswith("announced"):
            event_type = "Announced"

        amount_match = re.search(r"KES\s+([\d,.]+)", description)
        if amount_match:
            amount_kes = amount_match.group(1)

        div_match = re.search(
            r"(first and final dividend|interim dividend|final dividend)",
            desc_lower,
        )
        if div_match:
            dividend_type = div_match.group(1)

        return event_type, amount_kes, dividend_type


class NSEDatabaseScraper:
    """Scrape NSE stock quotes and announcements and save them to the database."""

    def __init__(
        self,
        session_factory,
        tickers: Optional[list[str]] = None,
        delay: float = 1.0,
        logger: Optional[logging.Logger] = None,
    ):
        self.session_factory = session_factory
        self.tickers = tickers or ALL_TICKERS
        self.delay = delay
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self.scraper = NSEScraper(delay=self.delay, logger=self.logger)

    @staticmethod
    def _safe_float(value: Optional[str]) -> Optional[float]:
        """Safely convert a string to float, returning None if conversion fails."""
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    def run(self) -> tuple[list[StockQuote], list[AnnouncementData]]:
        stocks = self.scraper.get_stocks(self.tickers)
        announcements = self.scraper.get_announcements()
        self.save_stock_data(stocks)
        self.save_announcements(announcements)
        return stocks, announcements

    def save_stock_data(self, stock_data: list[StockData]) -> int:
        with self.session_factory() as session:
            for stock in stock_data:
                row = (
                    session.query(StockQuote)
                    .filter(StockQuote.ticker == stock.ticker)
                    .first()
                )
                if not row:
                    row = StockQuote(ticker=stock.ticker)
                    session.add(row)

                row.name = stock.name
                row.sector = stock.sector
                row.previous = self._safe_float(stock.previous)
                row.open = self._safe_float(stock.open)
                row.average = self._safe_float(stock.average)
                row.deals = stock.deals
                row.volume = stock.volume
                row.turnover = stock.turnover
                row.day_range = stock.day_range
                row.week_52_range = stock.week_52_range
                row.average_volume = stock.average_volume
                row.beta = stock.beta
                row.shares_issued = stock.shares_issued
                row.year_end = stock.year_end
                row.par_value = stock.par_value
                row.profile = stock.profile
                row.error = stock.error
                row.scraped_at = datetime.utcnow()

            session.commit()
        self.logger.info("Saved %d stock quote rows", len(stock_data))
        return len(stock_data)

    def save_announcements(self, announcements: list[AnnouncementData]) -> int:
        saved = 0
        with self.session_factory() as session:
            for announcement in announcements:
                existing = (
                    session.query(Announcement)
                    .filter(
                        Announcement.date == announcement.date,
                        Announcement.ticker == announcement.ticker,
                        Announcement.description == announcement.description,
                    )
                    .first()
                )
                if existing:
                    continue

                session.add(Announcement(
                    date=announcement.date,
                    ticker=announcement.ticker,
                    company=announcement.company,
                    event_type=announcement.event_type,
                    amount_kes=announcement.amount_kes,
                    dividend_type=announcement.dividend_type,
                    description=announcement.description,
                    scraped_at=datetime.utcnow(),
                ))
                saved += 1
            session.commit()
        self.logger.info("Saved %d announcements", saved)
        return saved

    def close(self) -> None:
        self.scraper.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def scrape(self, tickers: Optional[list[str]] = None) -> tuple[list[StockData], list[AnnouncementData]]:
        return self.scraper.get_stocks(tickers or self.tickers), self.scraper.get_announcements()

    def scrape_and_store(self, tickers: Optional[list[str]] = None) -> tuple[int, int]:
        stocks, announcements = self.scrape(tickers)
        return self.save_stock_data(stocks), self.save_announcements(announcements)

    def run_once(self, tickers: Optional[list[str]] = None) -> tuple[int, int]:
        return self.scrape_and_store(tickers)

    def run_periodic(self, interval_minutes: int = 10) -> None:
        import schedule
        schedule.every(interval_minutes).minutes.do(self.run_once)
        while True:
            schedule.run_pending()
            time.sleep(1)

    def __repr__(self):
        return f"<NSEDatabaseScraper tickers={len(self.tickers)} delay={self.delay}>"