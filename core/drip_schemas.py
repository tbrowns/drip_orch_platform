"""
core/drip_schemas.py — Pydantic request/response models for the DRIP endpoints
and the DB-free logic that turns a request plus looked-up market data into
engine inputs.

Kept out of main.py so it can be unit-tested without a database connection
(main.py connects to DATABASE_URL at import time).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from scraper.drip import (
    DEFAULT_BROKERAGE_RATE,
    DEFAULT_MIN_BROKERAGE_KES,
    RESIDENT_WHT_RATE,
    DripAssumptions,
    DripProjection,
)
from scraper.models import DividendData, QuoteData, estimate_annual_dividend_per_share

MAX_YEARS = 40
PaymentsPerYear = Literal[1, 2, 4]


class InputResolutionError(Exception):
    """Raised when a request cannot be turned into engine inputs."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ── Request models ────────────────────────────────────────────────────────────

class DripAssumptionsIn(BaseModel):
    """Overridable engine assumptions. Defaults are the Kenyan resident case."""

    withholding_tax_rate: float = Field(
        RESIDENT_WHT_RATE, ge=0, lt=1,
        description="Final withholding tax on dividends. 0.05 resident individual, 0.15 non-resident.",
    )
    brokerage_rate: float = Field(
        DEFAULT_BROKERAGE_RATE, ge=0, lt=1,
        description="All-in purchase cost as a fraction of consideration (commission + VAT + NSE/CMA/CDSC/ICF levies). Approximate.",
    )
    min_brokerage_kes: float = Field(
        DEFAULT_MIN_BROKERAGE_KES, ge=0,
        description="Minimum commission per trade in KES. 0 disables.",
    )
    allow_fractional_shares: bool = Field(False, description="NSE has no fractional shares; True only for what-ifs.")
    reinvest_leftover: bool = Field(True, description="Carry cash that could not buy a whole share into the next period.")

    def to_engine(self) -> DripAssumptions:
        return DripAssumptions(**self.model_dump())


class DripSimulateRequest(BaseModel):
    """
    Body of POST /drip/simulate.

    Give a ``ticker`` (price and dividend looked up from the scraped tables,
    explicit values win when supplied) or give both ``price`` and
    ``dividend_per_share`` explicitly.
    """

    ticker: Optional[str] = Field(None, min_length=1, max_length=20, description="NSE ticker, e.g. SCOM.")
    price: Optional[float] = Field(None, gt=0, description="Share price in KES. Overrides the scraped quote.")
    dividend_per_share: Optional[float] = Field(
        None, ge=0, description="Annual gross dividend per share in KES. Overrides the scraped announcements."
    )
    shares_held: int = Field(..., ge=1, description="Shares held at the start.")
    years: int = Field(10, ge=1, le=MAX_YEARS)
    payments_per_year: PaymentsPerYear = Field(1, description="1 (final only), 2 (interim + final) or 4.")
    price_growth_rate: float = Field(0.0, gt=-1, le=1, description="Annual price growth, e.g. 0.05 for 5 %.")
    dividend_growth_rate: float = Field(0.0, gt=-1, le=1, description="Annual dividend growth, e.g. 0.03 for 3 %.")
    assumptions: DripAssumptionsIn = Field(default_factory=DripAssumptionsIn)

    @model_validator(mode="after")
    def _ticker_or_explicit_values(self) -> "DripSimulateRequest":
        if self.ticker is not None:
            self.ticker = self.ticker.strip().upper()
            if not self.ticker:
                raise ValueError("ticker must not be blank")
        if self.ticker is None and (self.price is None or self.dividend_per_share is None):
            raise ValueError("Provide a ticker, or both price and dividend_per_share.")
        return self


# ── Response models ───────────────────────────────────────────────────────────

class DividendEventOut(BaseModel):
    ticker: str
    amount: Optional[float]
    date: Optional[str]
    event_type: Optional[str] = None
    dividend_type: Optional[str] = None
    company: Optional[str] = None
    description: str = ""


class DripInputsOut(BaseModel):
    ticker: Optional[str] = None
    name: Optional[str] = None
    price: float
    price_source: str                       # "request" | "db:stock_quotes.previous" | ...
    dividend_per_share_annual: float
    dividend_source: str                    # "request" | "db:dividend_announcements"
    shares_held: float
    years: int
    payments_per_year: int
    price_growth_rate: float
    dividend_growth_rate: float
    annual_dividend_yield_pct: float
    net_dividend_yield_pct: float
    quote_scraped_at: Optional[str] = None
    dividend_events_used: list[DividendEventOut] = Field(default_factory=list)


class DripPeriodOut(BaseModel):
    period: int
    year: int
    payment_in_year: int
    price: float
    shares_start: float
    gross_dividend: float
    withholding_tax: float
    net_dividend: float
    cash_available: float
    shares_bought: float
    fees: float
    cash_carried: float
    shares_end: float
    portfolio_value: float


class DripTotalsOut(BaseModel):
    initial_shares: float
    shares_end: float
    shares_bought: float
    total_gross_dividends: float
    total_tax_paid: float
    total_net_dividends: float
    total_fees_paid: float
    cash_carried_end: float
    cash_paid_out: float
    ending_price: float
    ending_value: float
    vs_no_reinvest_value: float
    reinvestment_gain: float
    reinvestment_gain_pct: float


class DripSimulateResponse(BaseModel):
    inputs: DripInputsOut
    assumptions: DripAssumptionsIn
    periods: list[DripPeriodOut]
    totals: DripTotalsOut


class DripPortfolioPositionOut(BaseModel):
    ticker: str
    name: Optional[str] = None
    inputs: DripInputsOut
    totals: DripTotalsOut
    periods: list[DripPeriodOut] = Field(default_factory=list)


class SkippedPositionOut(BaseModel):
    ticker: str
    shares_held: float
    reason: str


class DripPortfolioAggregateOut(BaseModel):
    positions: int
    initial_value: float
    ending_value: float
    vs_no_reinvest_value: float
    reinvestment_gain: float
    total_net_dividends: float
    total_tax_paid: float
    total_fees_paid: float


class DripPortfolioResponse(BaseModel):
    portfolio_id: int
    portfolio_name: str
    cash_balance: float
    years: int
    payments_per_year: int
    assumptions: DripAssumptionsIn
    positions: list[DripPortfolioPositionOut]
    skipped: list[SkippedPositionOut]
    aggregate: DripPortfolioAggregateOut


# ── Resolution: request + market data -> engine inputs ────────────────────────

class ResolvedInputs(BaseModel):
    ticker: Optional[str]
    name: Optional[str]
    price: float
    price_source: str
    dividend_per_share_annual: float
    dividend_source: str
    quote_scraped_at: Optional[str]
    dividend_events_used: list[DividendEventOut]


def resolve_market_inputs(
    ticker: Optional[str],
    explicit_price: Optional[float],
    explicit_dividend: Optional[float],
    quote: Optional[QuoteData],
    dividends: list[DividendData],
) -> ResolvedInputs:
    """
    Decide which price and annual dividend the engine will use.

    Explicit request values always win. Otherwise the scraped quote supplies
    the price and the announcements supply a de-duplicated annual dividend.
    Raises ``InputResolutionError`` (404 for a missing quote, 422 for data the
    caller must supply explicitly) when neither source has what is needed.
    """
    label = ticker or "the requested stock"

    if explicit_price is not None:
        price, price_source = float(explicit_price), "request"
    elif quote is not None and quote.price is not None and quote.price > 0:
        price, price_source = float(quote.price), f"db:stock_quotes.{quote.price_source}"
    elif ticker is not None and quote is None:
        raise InputResolutionError(
            404, f"No quote found for ticker '{ticker}'. Pass 'price' explicitly or check the ticker.",
        )
    else:
        raise InputResolutionError(
            422, f"The scraped quote for {label} has no usable price. Pass 'price' explicitly.",
        )

    events_used: list[DividendData] = []
    if explicit_dividend is not None:
        dps, dividend_source = float(explicit_dividend), "request"
    else:
        estimate, events_used = estimate_annual_dividend_per_share(dividends)
        if estimate is None:
            raise InputResolutionError(
                422,
                f"No dividend announcement with an amount found for {label}. "
                "Pass 'dividend_per_share' (annual, gross, KES) explicitly.",
            )
        dps, dividend_source = estimate, "db:dividend_announcements"

    return ResolvedInputs(
        ticker=ticker,
        name=quote.name if quote else None,
        price=price,
        price_source=price_source,
        dividend_per_share_annual=dps,
        dividend_source=dividend_source,
        quote_scraped_at=_iso(quote.scraped_at) if quote else None,
        dividend_events_used=[DividendEventOut(**e.to_dict()) for e in events_used],
    )


def build_inputs_out(
    resolved: ResolvedInputs,
    projection: DripProjection,
    shares_held: float,
    years: int,
    payments_per_year: int,
    price_growth_rate: float,
    dividend_growth_rate: float,
) -> DripInputsOut:
    return DripInputsOut(
        ticker=resolved.ticker,
        name=resolved.name,
        price=resolved.price,
        price_source=resolved.price_source,
        dividend_per_share_annual=resolved.dividend_per_share_annual,
        dividend_source=resolved.dividend_source,
        shares_held=float(shares_held),
        years=years,
        payments_per_year=payments_per_year,
        price_growth_rate=price_growth_rate,
        dividend_growth_rate=dividend_growth_rate,
        annual_dividend_yield_pct=round(projection.inputs["annual_dividend_yield_pct"], 4),
        net_dividend_yield_pct=round(projection.inputs["net_dividend_yield_pct"], 4),
        quote_scraped_at=resolved.quote_scraped_at,
        dividend_events_used=resolved.dividend_events_used,
    )


def projection_to_response(
    resolved: ResolvedInputs,
    projection: DripProjection,
    assumptions: DripAssumptionsIn,
    shares_held: float,
    years: int,
    payments_per_year: int,
    price_growth_rate: float,
    dividend_growth_rate: float,
) -> DripSimulateResponse:
    data = projection.to_dict(money_decimals=2)
    return DripSimulateResponse(
        inputs=build_inputs_out(
            resolved, projection, shares_held, years, payments_per_year,
            price_growth_rate, dividend_growth_rate,
        ),
        assumptions=assumptions,
        periods=[DripPeriodOut(**p) for p in data["periods"]],
        totals=DripTotalsOut(**data["totals"]),
    )


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
