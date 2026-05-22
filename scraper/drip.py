"""
scraper/drip.py — Computes DRIP (Dividend Reinvestment) metrics.

Given scraped price + dividend data, this module calculates how many
new shares a holder can accumulate through dividend reinvestment —
simulating what an automated DRIP would do on the NSE (manually,
since Kenya has no broker-level DRIP infrastructure yet).
"""

import logging
from dataclasses import dataclass
from typing import Optional

from scraper.mystocks import QuoteData, DividendData

logger = logging.getLogger(__name__)


@dataclass
class DRIPResult:
    ticker:          str
    shares_held:     float
    current_price:   Optional[float]
    last_dividend:   Optional[float]    # KES per share
    total_dividend:  float              # total KES received
    reinvest_shares: float              # whole shares purchasable
    leftover_cash:   float              # KES remaining after purchase
    annual_yield_pct: Optional[float]
    note:            str = ""


def compute_drip(
    quote: QuoteData,
    shares_held: float,
    dividends: list[DividendData],
    portfolio_override_dividend: Optional[float] = None,
) -> Optional[DRIPResult]:
    """
    Compute DRIP metrics for a single stock.

    Args:
        quote:                      Latest scraped QuoteData.
        shares_held:                Number of shares in the portfolio.
        dividends:                  DividendData list from scraper.
        portfolio_override_dividend: Override the dividend per share
                                     (e.g. from user's portfolio record).

    Returns DRIPResult or None if data is insufficient.
    """
    price = quote.price
    if not price or price <= 0:
        logger.warning("No valid price for %s — skipping DRIP calc", quote.ticker)
        return None

    # Determine dividend per share
    dividend_per_share = portfolio_override_dividend or quote.dividend
    if not dividend_per_share and dividends:
        # Use the most recent dividend event
        dividend_per_share = dividends[0].amount

    if not dividend_per_share or dividend_per_share <= 0:
        logger.debug("No dividend data for %s — skipping", quote.ticker)
        return None

    total_dividend  = shares_held * dividend_per_share
    reinvest_shares = total_dividend // price          # whole shares only
    leftover_cash   = total_dividend - (reinvest_shares * price)

    # Annual yield based on scraped yield or computed
    annual_yield = quote.dividend_yield
    if not annual_yield and price > 0:
        annual_yield = round((dividend_per_share / price) * 100, 2)

    return DRIPResult(
        ticker          = quote.ticker,
        shares_held     = shares_held,
        current_price   = price,
        last_dividend   = dividend_per_share,
        total_dividend  = round(total_dividend, 2),
        reinvest_shares = reinvest_shares,
        leftover_cash   = round(leftover_cash, 2),
        annual_yield_pct = annual_yield,
        note = (
            f"Buy {int(reinvest_shares)} shares @ KES {price:.2f} "
            f"| KES {leftover_cash:.2f} cash carried over"
        ),
    )


def compute_portfolio_drip(
    quotes: list[QuoteData],
    dividends: list[DividendData],
    portfolio: dict[str, float],     # {ticker: shares_held}
) -> list[DRIPResult]:
    """
    Run DRIP calculation for an entire portfolio.

    Args:
        quotes:    Scraped QuoteData list.
        dividends: Scraped DividendData list.
        portfolio: Dict mapping ticker → number of shares held.

    Returns a list of DRIPResult, one per portfolio position.
    """
    quote_map = {q.ticker: q for q in quotes}
    div_map: dict[str, list[DividendData]] = {}
    for d in dividends:
        div_map.setdefault(d.ticker, []).append(d)

    results: list[DRIPResult] = []
    for ticker, shares in portfolio.items():
        quote = quote_map.get(ticker)
        if not quote:
            logger.warning("No scraped data for portfolio ticker %s", ticker)
            continue
        result = compute_drip(quote, shares, div_map.get(ticker, []))
        if result:
            results.append(result)

    return results