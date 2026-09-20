"""
scraper/models.py — plain data carriers shared by the scrapers, the database
layer and the DRIP engine.

The DRIP engine (scraper/drip.py) is pure and must not know about SQLAlchemy.
These dataclasses are the boundary, and ``quote_from_row`` /
``dividend_from_row`` adapt the ORM rows in db/models.py into them.

What the scrapers actually give us (see nse_scraper.py):

* ``StockQuote`` — one row per ticker from live.mystocks.co.ke. There is no
  "last price" column. The numeric fields are ``previous`` (previous close),
  ``open`` and ``average`` (volume-weighted average for the session). There is
  no dividend or dividend-yield column at all, so ``QuoteData.dividend`` is
  only populated when a caller derives it from announcements.
* ``Announcement`` — one row per line on the mystocks corporate calendar.
  ``amount_kes`` is a *string* as scraped ("1.20", "0.35", "2,000.00"),
  ``event_type`` is "Payment" / "Book closure" / "Announced" / "Other" and
  ``dividend_type`` is "final dividend" / "interim dividend" /
  "first and final dividend" or None. The same dividend normally appears
  several times (announced, book closure, payment) carrying the same amount,
  so anything that adds amounts up must de-duplicate first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

# Columns on StockQuote that can serve as a reference price, in order of
# preference. "previous" is the previous close, which is the conventional
# reference for a dividend-reinvestment projection; "average" (session VWAP)
# and "open" are fallbacks for tickers whose previous close did not parse.
PRICE_COLUMNS: tuple[str, ...] = ("previous", "average", "open")

_AMOUNT_RE = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass
class QuoteData:
    """A stock quote reduced to what the DRIP engine needs."""

    ticker: str
    price: Optional[float]                  # KES per share, None if unknown
    dividend: Optional[float] = None        # annual KES per share, if known
    dividend_yield: Optional[float] = None  # percent, if known
    name: Optional[str] = None
    sector: Optional[str] = None
    price_source: Optional[str] = None      # StockQuote column that supplied price
    scraped_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["scraped_at"] = self.scraped_at.isoformat() if self.scraped_at else None
        return data


@dataclass
class DividendData:
    """One dividend calendar event for a ticker."""

    ticker: str
    amount: Optional[float]                 # KES per share, None if not parseable
    date: Optional[date]                    # calendar date of the event
    event_type: Optional[str] = None        # Payment / Book closure / Announced / Other
    dividend_type: Optional[str] = None     # final / interim / first and final
    company: Optional[str] = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["date"] = self.date.isoformat() if self.date else None
        return data


# ── Parsing helpers ───────────────────────────────────────────────────────────

def parse_kes_amount(raw: Any) -> Optional[float]:
    """
    Turn a scraped amount ("1.20", "KES 0.35", "2,000.00", 1.2) into a float.

    Returns None for None/blank/unparseable input and for negative values —
    a negative dividend is never meaningful, so it is treated as "unknown".
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if value >= 0 else None
    text = str(raw).replace(",", "").strip()
    if not text:
        return None
    match = _AMOUNT_RE.search(text)
    if not match:
        return None
    try:
        value = float(match.group(0))
    except ValueError:
        return None
    return value if value >= 0 else None


def _coerce_date(value: Any) -> Optional[date]:
    """Accept date, datetime, ISO string or None and return a date (or None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%b %d %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _positive_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


# ── Adapters from SQLAlchemy rows ─────────────────────────────────────────────

def quote_from_row(row: Any) -> QuoteData:
    """
    Adapt a ``db.models.StockQuote`` row (or anything with the same attribute
    names) into a ``QuoteData``. The reference price is the first positive
    value among ``PRICE_COLUMNS``.
    """
    price: Optional[float] = None
    source: Optional[str] = None
    for column in PRICE_COLUMNS:
        candidate = _positive_float(getattr(row, column, None))
        if candidate is not None:
            price, source = candidate, column
            break

    return QuoteData(
        ticker=str(getattr(row, "ticker", "") or "").upper(),
        price=price,
        dividend=None,
        dividend_yield=None,
        name=getattr(row, "name", None),
        sector=getattr(row, "sector", None),
        price_source=source,
        scraped_at=getattr(row, "scraped_at", None),
    )


def dividend_from_row(row: Any) -> DividendData:
    """Adapt a ``db.models.Announcement`` row into a ``DividendData``."""
    return DividendData(
        ticker=str(getattr(row, "ticker", "") or "").upper(),
        amount=parse_kes_amount(getattr(row, "amount_kes", None)),
        date=_coerce_date(getattr(row, "date", None)),
        event_type=getattr(row, "event_type", None),
        dividend_type=getattr(row, "dividend_type", None),
        company=getattr(row, "company", None),
        description=getattr(row, "description", None) or "",
    )


def dividends_from_rows(rows: Iterable[Any]) -> list[DividendData]:
    """
    Adapt announcement rows and keep only those that carry a positive dividend
    amount, newest first. Rows without a date sort last.
    """
    events = [dividend_from_row(r) for r in rows]
    events = [e for e in events if e.amount is not None and e.amount > 0]
    events.sort(key=lambda e: (e.date is not None, e.date or date.min), reverse=True)
    return events


# ── Dividend selection ────────────────────────────────────────────────────────

def latest_dividend(
    dividends: Iterable[DividendData],
    as_of: Optional[date] = None,
) -> Optional[DividendData]:
    """
    The most relevant single dividend event for a ticker:

    1. the most recent event dated on or before ``as_of`` (default: today), else
    2. the nearest future event (the calendar is forward-looking, so a freshly
       declared dividend is often the only data we have), else
    3. an undated event, if that is all there is.
    """
    as_of = as_of or date.today()
    priced = [d for d in dividends if d.amount is not None and d.amount > 0]
    if not priced:
        return None

    past = [d for d in priced if d.date is not None and d.date <= as_of]
    if past:
        return max(past, key=lambda d: d.date)  # type: ignore[arg-type,return-value]

    future = [d for d in priced if d.date is not None and d.date > as_of]
    if future:
        return min(future, key=lambda d: d.date)  # type: ignore[arg-type,return-value]

    return priced[0]


def estimate_annual_dividend_per_share(
    dividends: Iterable[DividendData],
    as_of: Optional[date] = None,
    window_days: int = 365,
) -> tuple[Optional[float], list[DividendData]]:
    """
    Estimate the annual dividend per share from calendar events.

    Anchors on ``latest_dividend`` and sums the *distinct* dividends whose
    date falls in the ``window_days`` ending on the anchor's date. Distinct
    means one dividend per ``dividend_type`` ("interim dividend", "final
    dividend", "first and final dividend", ...), the most recent one winning.
    That collapses the announced / book-closure / payment rows of one
    dividend into a single amount, adds an interim and a final together, and
    never adds last year's final to this year's: payment dates recur at about
    the same time each year, so a 365-day window ending on this year's final
    routinely contains last year's as well, with a different amount whenever
    the dividend grew. Rows without a ``dividend_type`` cannot be told apart
    that way and fall back to de-duplication by amount (skipped when the
    amount matches a typed dividend already kept).

    Returns ``(annual_dps, events_used)``; ``(None, [])`` when nothing usable.
    """
    events = [d for d in dividends if d.amount is not None and d.amount > 0]
    anchor = latest_dividend(events, as_of=as_of)
    if anchor is None:
        return None, []

    if anchor.date is None:
        return anchor.amount, [anchor]

    window_start = anchor.date - timedelta(days=window_days)
    in_window = [
        d for d in events
        if d.date is not None and window_start < d.date <= anchor.date
    ]

    newest_first = sorted(in_window, key=lambda d: d.date, reverse=True)  # type: ignore[arg-type,return-value]
    used: list[DividendData] = []
    seen_types: set[str] = set()
    seen_amounts: set[float] = set()

    # Pass 1: typed rows, one per dividend_type, most recent wins.
    for event in newest_first:
        dividend_type = (event.dividend_type or "").strip().lower()
        if not dividend_type or dividend_type in seen_types:
            continue
        seen_types.add(dividend_type)
        seen_amounts.add(round(event.amount or 0.0, 4))
        used.append(event)

    # Pass 2: untyped rows, de-duplicated by amount against everything kept.
    for event in newest_first:
        if (event.dividend_type or "").strip():
            continue
        amount = round(event.amount or 0.0, 4)
        if amount in seen_amounts:
            continue
        seen_amounts.add(amount)
        used.append(event)

    used.sort(key=lambda d: d.date, reverse=True)  # type: ignore[arg-type,return-value]
    total = sum(e.amount or 0.0 for e in used)
    return (total if total > 0 else None), used
