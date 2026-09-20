"""
scraper/drip.py — pure DRIP (Dividend Reinvestment Plan) engine for NSE equities.

Kenya has no broker-level DRIP, so "reinvesting" a dividend means the investor
receives the cash and places an ordinary buy order. This module models exactly
that, period by period, with the frictions a Nairobi Securities Exchange
investor actually faces:

* **Withholding tax.** Dividends are paid net of a final withholding tax:
  5 % for resident individuals, 15 % for non-residents. Only the net cash can
  be reinvested.
* **Transaction costs on the purchase.** A buy order carries broker commission
  plus statutory levies (NSE, CMA, CDSC, Investor Compensation Fund), usually
  with a minimum commission per trade. Small reinvestments are hit hardest.
* **Whole shares only.** The NSE has no fractional shares. Purchases are
  floored to whole shares and the remainder is carried as cash into the next
  period, where it is added to the next net dividend.
* **Compounding.** Shares bought in one period earn dividends in the next.
  ``project_drip`` runs the reinvestment loop for N years with one, two or
  four payments a year and reports the path plus a comparison against simply
  keeping the (net) dividends as cash.

The module is pure: no database, no I/O, no logging side effects. Everything
is a function of its arguments plus an explicit ``DripAssumptions``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field
from typing import Any, Optional

from scraper.models import (
    QuoteData,
    DividendData,
    estimate_annual_dividend_per_share,
    latest_dividend,
)

__all__ = [
    "RESIDENT_WHT_RATE",
    "NON_RESIDENT_WHT_RATE",
    "DEFAULT_BROKERAGE_RATE",
    "DEFAULT_MIN_BROKERAGE_KES",
    "DripAssumptions",
    "ReinvestmentResult",
    "DripPeriod",
    "DripTotals",
    "DripProjection",
    "DRIPResult",
    "max_affordable_shares",
    "compute_reinvestment",
    "project_drip",
    "compute_drip",
    "compute_portfolio_drip",
]

# ── Kenyan defaults ───────────────────────────────────────────────────────────

#: Final withholding tax on dividends paid to a Kenyan-resident individual.
RESIDENT_WHT_RATE = 0.05
#: Final withholding tax on dividends paid to a non-resident.
NON_RESIDENT_WHT_RATE = 0.15

#: Approximate all-in cost of a small (< KES 100,000) NSE buy order as a
#: fraction of the consideration. This is an APPROXIMATION built from the
#: statutory levy schedule plus the CMA-capped broker commission:
#:
#:     broker commission (cap for orders up to KES 100,000) ... 1.50 %
#:     16 % VAT charged on that commission ..................... 0.24 %
#:     NSE transaction levy .................................... 0.12 %
#:     CMA transaction levy .................................... 0.12 %
#:     CDSC transaction levy ................................... 0.08 %
#:     Investor Compensation Fund levy ......................... 0.01 %
#:     ------------------------------------------------------------------
#:     ≈ 2.07 %, rounded up to 2.12 % to leave a small cushion for
#:     contract-note charges and broker rounding.
#:
#: Brokers differ, larger orders are cheaper, and levies get revised, so
#: treat this as a sensible default and override it with your broker's
#: actual schedule via ``DripAssumptions(brokerage_rate=...)``.
DEFAULT_BROKERAGE_RATE = 0.0212

#: Typical minimum commission a Kenyan broker charges per trade (KES). Also
#: an approximation; some brokers charge more. Override as needed.
DEFAULT_MIN_BROKERAGE_KES = 100.0

# Tolerance used when flooring to whole shares, so that an exactly affordable
# quantity such as 3000 / (300 * 1.0) is not knocked down to 9 by floating
# point noise (9.999999... -> 9).
_EPS = 1e-9


@dataclass(frozen=True)
class DripAssumptions:
    """
    Every knob of the engine, explicit, with Kenyan defaults.

    withholding_tax_rate:
        Final WHT deducted from the gross dividend before it reaches the
        investor. 0.05 for resident individuals (default), 0.15 for
        non-residents (see ``DripAssumptions.non_resident()``).
    brokerage_rate:
        All-in transaction cost of the reinvestment purchase as a fraction of
        the share consideration (commission + VAT + NSE/CMA/CDSC/ICF levies).
        Default 0.0212, an approximation — see ``DEFAULT_BROKERAGE_RATE``.
    min_brokerage_kes:
        Minimum fee per trade. A purchase costs
        ``max(consideration * brokerage_rate, min_brokerage_kes)``. Default
        100. Set to 0 to disable.
    allow_fractional_shares:
        False (default) because the NSE trades whole shares only. True is
        offered for what-if comparisons.
    reinvest_leftover:
        True (default) carries cash that could not buy a whole share into
        the next period. False treats it as paid out to the investor.
    """

    withholding_tax_rate: float = RESIDENT_WHT_RATE
    brokerage_rate: float = DEFAULT_BROKERAGE_RATE
    min_brokerage_kes: float = DEFAULT_MIN_BROKERAGE_KES
    allow_fractional_shares: bool = False
    reinvest_leftover: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.withholding_tax_rate < 1.0:
            raise ValueError("withholding_tax_rate must be in [0, 1)")
        if not 0.0 <= self.brokerage_rate < 1.0:
            raise ValueError("brokerage_rate must be in [0, 1)")
        if self.min_brokerage_kes < 0.0:
            raise ValueError("min_brokerage_kes must be >= 0")

    @classmethod
    def non_resident(cls, **overrides: Any) -> "DripAssumptions":
        """Defaults for a non-resident investor (15 % withholding tax)."""
        overrides.setdefault("withholding_tax_rate", NON_RESIDENT_WHT_RATE)
        return cls(**overrides)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Single reinvestment event ─────────────────────────────────────────────────

@dataclass
class ReinvestmentResult:
    """Outcome of reinvesting one dividend payment."""

    shares_start: float
    price: float
    gross_dividend: float        # shares_start * gross dividend per share
    withholding_tax: float       # gross_dividend * withholding_tax_rate
    net_dividend: float          # gross_dividend - withholding_tax
    cash_carried_in: float       # uninvested cash brought into this period
    cash_available: float        # net_dividend + cash_carried_in
    shares_bought: float         # whole shares unless fractional allowed
    purchase_cost: float         # shares_bought * price (excludes fees)
    fees: float                  # max(purchase_cost * brokerage_rate, min) or 0
    leftover_cash: float         # cash_available - purchase_cost - fees
    cash_carried: float          # leftover carried forward (0 if not reinvesting leftover)
    cash_paid_out: float         # leftover handed back (0 if reinvesting leftover)
    shares_end: float            # shares_start + shares_bought

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _purchase_fee(consideration: float, assumptions: DripAssumptions) -> float:
    """Fee on a buy of ``consideration`` KES worth of shares; 0 if nothing bought."""
    if consideration <= 0.0:
        return 0.0
    return max(consideration * assumptions.brokerage_rate, assumptions.min_brokerage_kes)


def max_affordable_shares(
    cash: float,
    price: float,
    assumptions: Optional[DripAssumptions] = None,
) -> float:
    """
    Largest quantity ``n`` such that ``n * price + fee(n * price) <= cash``,
    where ``fee(x) = max(x * brokerage_rate, min_brokerage_kes)``.

    Because ``max(a, b) <= c`` iff ``a <= c and b <= c``, the constraint splits
    into two linear ones that must both hold::

        n * price * (1 + brokerage_rate) <= cash        (percentage fee binds)
        n * price + min_brokerage_kes    <= cash        (minimum fee binds)

    so ``n = min(cash / (price * (1 + r)), (cash - min) / price)``, floored to a
    whole share unless fractional shares are allowed, and never negative.

    Raises ValueError if ``price <= 0``.
    """
    a = assumptions or DripAssumptions()
    if price <= 0.0:
        raise ValueError("price must be > 0")
    if cash <= 0.0:
        return 0.0

    by_rate = cash / (price * (1.0 + a.brokerage_rate))
    by_min = (cash - a.min_brokerage_kes) / price
    quantity = min(by_rate, by_min)
    if quantity <= 0.0:
        return 0.0

    if a.allow_fractional_shares:
        return quantity

    whole = float(math.floor(quantity + _EPS))
    # Defensive: never let the epsilon push us over budget.
    while whole > 0 and whole * price + _purchase_fee(whole * price, a) > cash + 1e-6:
        whole -= 1
    return whole


def compute_reinvestment(
    gross_dividend_per_share: float,
    shares_held: float,
    price: float,
    cash_carried: float = 0.0,
    assumptions: Optional[DripAssumptions] = None,
) -> ReinvestmentResult:
    """
    Reinvest one dividend payment.

    Steps:
        gross   = shares_held * gross_dividend_per_share
        tax     = gross * withholding_tax_rate
        net     = gross - tax
        cash    = net + cash_carried
        shares  = max_affordable_shares(cash, price)        (whole shares)
        fees    = max(shares * price * brokerage_rate, min_brokerage_kes)
        left    = cash - shares * price - fees

    Edge cases:
        * ``price <= 0`` raises ValueError (nothing can be priced).
        * A zero dividend is fine: nothing is bought unless carried cash
          alone affords a share.
        * A negative dividend, negative share count or negative carried cash
          raises ValueError: they are data errors, not scenarios.
        * When the minimum commission makes a tiny purchase uneconomic the
          engine buys 0 shares, charges no fee and carries the cash forward.
    """
    a = assumptions or DripAssumptions()
    if price <= 0.0:
        raise ValueError("price must be > 0")
    if gross_dividend_per_share < 0.0:
        raise ValueError("gross_dividend_per_share must be >= 0")
    if shares_held < 0.0:
        raise ValueError("shares_held must be >= 0")
    if cash_carried < 0.0:
        raise ValueError("cash_carried must be >= 0")

    gross = shares_held * gross_dividend_per_share
    tax = gross * a.withholding_tax_rate
    net = gross - tax
    cash = net + cash_carried

    bought = max_affordable_shares(cash, price, a)
    cost = bought * price
    fees = _purchase_fee(cost, a)
    leftover = cash - cost - fees
    if leftover < 0.0 and leftover > -1e-6:
        leftover = 0.0

    return ReinvestmentResult(
        shares_start=shares_held,
        price=price,
        gross_dividend=gross,
        withholding_tax=tax,
        net_dividend=net,
        cash_carried_in=cash_carried,
        cash_available=cash,
        shares_bought=bought,
        purchase_cost=cost,
        fees=fees,
        leftover_cash=leftover,
        cash_carried=leftover if a.reinvest_leftover else 0.0,
        cash_paid_out=0.0 if a.reinvest_leftover else leftover,
        shares_end=shares_held + bought,
    )


# ── Multi-period projection ───────────────────────────────────────────────────

@dataclass
class DripPeriod:
    """One dividend payment period in a projection."""

    period: int                  # 1-based, across the whole projection
    year: int                    # 1-based
    payment_in_year: int         # 1-based within the year
    price: float                 # share price used this period
    shares_start: float
    gross_dividend: float
    withholding_tax: float
    net_dividend: float
    cash_available: float
    shares_bought: float
    fees: float
    cash_carried: float          # uninvested cash carried out of this period
    shares_end: float
    portfolio_value: float       # shares_end * price + cash_carried

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DripTotals:
    """Summary of a projection."""

    initial_shares: float
    shares_end: float
    shares_bought: float
    total_gross_dividends: float
    total_tax_paid: float
    total_net_dividends: float
    total_fees_paid: float
    cash_carried_end: float          # uninvested cash at the end
    cash_paid_out: float             # leftover handed back (reinvest_leftover=False)
    ending_price: float
    ending_value: float              # shares_end * ending_price + cash_carried_end + cash_paid_out
    vs_no_reinvest_value: float      # initial_shares * ending_price + net dividends kept as cash
    reinvestment_gain: float         # ending_value - vs_no_reinvest_value
    reinvestment_gain_pct: float     # gain as % of vs_no_reinvest_value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DripProjection:
    """Full output of ``project_drip``."""

    inputs: dict[str, Any]
    assumptions: DripAssumptions
    periods: list[DripPeriod] = field(default_factory=list)
    totals: Optional[DripTotals] = None

    def to_dict(self, money_decimals: Optional[int] = 2, share_decimals: int = 6) -> dict[str, Any]:
        """
        Serialise for an API. Money is rounded to ``money_decimals`` places
        (None keeps full precision); share counts to ``share_decimals``.
        """
        def r(value: Any) -> Any:
            if isinstance(value, float) and money_decimals is not None:
                return round(value, money_decimals)
            return value

        share_keys = {"shares_start", "shares_bought", "shares_end", "initial_shares"}

        def clean(row: dict[str, Any]) -> dict[str, Any]:
            return {
                k: (round(v, share_decimals) if k in share_keys and isinstance(v, float) else r(v))
                for k, v in row.items()
            }

        return {
            "inputs": self.inputs,
            "assumptions": self.assumptions.to_dict(),
            "periods": [clean(p.to_dict()) for p in self.periods],
            "totals": clean(self.totals.to_dict()) if self.totals else None,
        }


def project_drip(
    initial_shares: float,
    price: float,
    dividend_per_share_annual: float,
    payments_per_year: int = 1,
    years: int = 10,
    assumptions: Optional[DripAssumptions] = None,
    price_growth_rate: float = 0.0,
    dividend_growth_rate: float = 0.0,
) -> DripProjection:
    """
    Project a DRIP for ``years`` years.

    Each year the annual dividend per share is split evenly across
    ``payments_per_year`` payments (NSE companies typically pay one final
    dividend, sometimes plus an interim — so 1 or 2; 4 is allowed for
    what-ifs). Each payment is run through ``compute_reinvestment`` with
    whole-share flooring and the leftover cash carried into the next payment.

    ``price_growth_rate`` and ``dividend_growth_rate`` are annual rates applied
    once per year: year 1 uses the inputs as given, year ``y`` uses
    ``price * (1 + g) ** (y - 1)``. Within a year the price is held constant.

    The comparison baseline ``vs_no_reinvest_value`` holds the initial shares
    only and keeps every net (after-tax) dividend as cash: it is what the
    investor would have if they never reinvested.

    Raises ValueError for ``price <= 0``, ``initial_shares <= 0``,
    ``years < 1``, ``payments_per_year < 1``, negative dividend, or growth
    rates <= -100 %.
    """
    a = assumptions or DripAssumptions()
    if price <= 0.0:
        raise ValueError("price must be > 0")
    if initial_shares <= 0.0:
        raise ValueError("initial_shares must be > 0")
    if dividend_per_share_annual < 0.0:
        raise ValueError("dividend_per_share_annual must be >= 0")
    if int(years) != years or years < 1:
        raise ValueError("years must be a positive integer")
    if int(payments_per_year) != payments_per_year or payments_per_year < 1:
        raise ValueError("payments_per_year must be a positive integer")
    if price_growth_rate <= -1.0 or dividend_growth_rate <= -1.0:
        raise ValueError("growth rates must be greater than -1.0")

    years = int(years)
    payments_per_year = int(payments_per_year)

    periods: list[DripPeriod] = []
    shares = float(initial_shares)
    carried = 0.0
    paid_out = 0.0
    total_gross = total_tax = total_net = total_fees = 0.0
    baseline_cash = 0.0        # net dividends on the initial shares, kept as cash
    period_no = 0
    price_y = float(price)

    for year in range(1, years + 1):
        price_y = price * (1.0 + price_growth_rate) ** (year - 1)
        dps_y = dividend_per_share_annual * (1.0 + dividend_growth_rate) ** (year - 1)
        dps_period = dps_y / payments_per_year

        for payment in range(1, payments_per_year + 1):
            period_no += 1
            step = compute_reinvestment(dps_period, shares, price_y, carried, a)

            total_gross += step.gross_dividend
            total_tax += step.withholding_tax
            total_net += step.net_dividend
            total_fees += step.fees
            paid_out += step.cash_paid_out
            baseline_cash += initial_shares * dps_period * (1.0 - a.withholding_tax_rate)

            shares = step.shares_end
            carried = step.cash_carried

            periods.append(DripPeriod(
                period=period_no,
                year=year,
                payment_in_year=payment,
                price=price_y,
                shares_start=step.shares_start,
                gross_dividend=step.gross_dividend,
                withholding_tax=step.withholding_tax,
                net_dividend=step.net_dividend,
                cash_available=step.cash_available,
                shares_bought=step.shares_bought,
                fees=step.fees,
                cash_carried=step.cash_carried,
                shares_end=step.shares_end,
                portfolio_value=step.shares_end * price_y + step.cash_carried,
            ))

    ending_value = shares * price_y + carried + paid_out
    baseline_value = initial_shares * price_y + baseline_cash
    gain = ending_value - baseline_value

    totals = DripTotals(
        initial_shares=float(initial_shares),
        shares_end=shares,
        shares_bought=shares - initial_shares,
        total_gross_dividends=total_gross,
        total_tax_paid=total_tax,
        total_net_dividends=total_net,
        total_fees_paid=total_fees,
        cash_carried_end=carried,
        cash_paid_out=paid_out,
        ending_price=price_y,
        ending_value=ending_value,
        vs_no_reinvest_value=baseline_value,
        reinvestment_gain=gain,
        reinvestment_gain_pct=(gain / baseline_value * 100.0) if baseline_value else 0.0,
    )

    inputs = {
        "initial_shares": float(initial_shares),
        "price": float(price),
        "dividend_per_share_annual": float(dividend_per_share_annual),
        "payments_per_year": payments_per_year,
        "years": years,
        "price_growth_rate": float(price_growth_rate),
        "dividend_growth_rate": float(dividend_growth_rate),
        "annual_dividend_yield_pct": (dividend_per_share_annual / price) * 100.0,
        "net_dividend_yield_pct": (dividend_per_share_annual / price) * 100.0 * (1.0 - a.withholding_tax_rate),
    }
    return DripProjection(inputs=inputs, assumptions=a, periods=periods, totals=totals)


# ── Compatibility wrappers ────────────────────────────────────────────────────

@dataclass
class DRIPResult:
    """
    Per-ticker result kept for compatibility with earlier callers, now backed
    by the real engine. ``reinvest_shares`` / ``leftover_cash`` describe the
    first reinvestment of the projection (what "buy now with this dividend"
    looks like); ``projection`` holds the multi-period path.
    """

    ticker: str
    shares_held: float
    current_price: Optional[float]
    last_dividend: Optional[float]      # KES per share, per year, used
    total_dividend: float               # gross KES for the first payment
    withholding_tax: float              # KES withheld on the first payment
    net_dividend: float                 # KES actually received, first payment
    reinvest_shares: float              # whole shares bought with the first payment
    fees: float                         # KES fees on that purchase
    leftover_cash: float                # KES carried after that purchase
    annual_yield_pct: Optional[float]
    projection: Optional[DripProjection] = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in asdict(self).items() if k != "projection"}
        data["projection"] = self.projection.to_dict() if self.projection else None
        return data


def compute_drip(
    quote: QuoteData,
    shares_held: float,
    dividends: list[DividendData],
    portfolio_override_dividend: Optional[float] = None,
    *,
    years: int = 1,
    payments_per_year: int = 1,
    assumptions: Optional[DripAssumptions] = None,
) -> Optional[DRIPResult]:
    """
    Compute DRIP metrics for a single stock from scraped data.

    Dividend per share (annual) is taken from, in order:
    ``portfolio_override_dividend``, ``quote.dividend``, the annual estimate
    from ``dividends`` (de-duplicated interim + final), then the single latest
    dividend event. Returns None when there is no usable price or dividend.
    """
    price = quote.price
    if price is None or price <= 0.0 or shares_held <= 0.0:
        return None

    dps: Optional[float] = None
    if portfolio_override_dividend is not None and portfolio_override_dividend > 0:
        dps = float(portfolio_override_dividend)
    elif quote.dividend is not None and quote.dividend > 0:
        dps = float(quote.dividend)
    else:
        estimate, _ = estimate_annual_dividend_per_share(dividends)
        if estimate is None:
            latest = latest_dividend(dividends)
            estimate = latest.amount if latest else None
        dps = estimate

    if dps is None or dps <= 0.0:
        return None

    a = assumptions or DripAssumptions()
    projection = project_drip(
        initial_shares=shares_held,
        price=price,
        dividend_per_share_annual=dps,
        payments_per_year=payments_per_year,
        years=years,
        assumptions=a,
    )
    first = projection.periods[0]
    annual_yield = quote.dividend_yield
    if annual_yield is None:
        annual_yield = round((dps / price) * 100.0, 2)

    return DRIPResult(
        ticker=quote.ticker,
        shares_held=shares_held,
        current_price=price,
        last_dividend=dps,
        total_dividend=round(first.gross_dividend, 2),
        withholding_tax=round(first.withholding_tax, 2),
        net_dividend=round(first.net_dividend, 2),
        reinvest_shares=first.shares_bought,
        fees=round(first.fees, 2),
        leftover_cash=round(first.cash_carried, 2),
        annual_yield_pct=annual_yield,
        projection=projection,
        note=(
            f"Buy {first.shares_bought:g} shares @ KES {price:.2f} "
            f"(net dividend KES {first.net_dividend:.2f} after {a.withholding_tax_rate:.0%} WHT, "
            f"fees KES {first.fees:.2f}) | KES {first.cash_carried:.2f} cash carried over"
        ),
    )


def compute_portfolio_drip(
    quotes: list[QuoteData],
    dividends: list[DividendData],
    portfolio: dict[str, float],
    **kwargs: Any,
) -> list[DRIPResult]:
    """
    Run ``compute_drip`` for every ``{ticker: shares_held}`` in ``portfolio``.
    Tickers without a quote or without dividend data are skipped.
    Keyword arguments are passed through to ``compute_drip``.
    """
    quote_map = {q.ticker.upper(): q for q in quotes}
    div_map: dict[str, list[DividendData]] = {}
    for d in dividends:
        div_map.setdefault(d.ticker.upper(), []).append(d)

    results: list[DRIPResult] = []
    for ticker, shares in portfolio.items():
        quote = quote_map.get(ticker.upper())
        if quote is None:
            continue
        result = compute_drip(quote, shares, div_map.get(ticker.upper(), []), **kwargs)
        if result is not None:
            results.append(result)
    return results
