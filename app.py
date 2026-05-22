"""
main.py — Periodic NSE scraper for the DRIP application.

Usage:
    python main.py                    # runs on schedule (see .env)
    python main.py --once             # single run then exit
    python main.py --once --tickers SCOM,EQTY   # override watchlist
    python main.py --portfolio SCOM:500,EQTY:200 # compute DRIP for your portfolio

Setup:
    1. cp .env.example .env
    2. Edit .env (DATABASE_URL, WATCH_LIST, etc.)
    3. pip install -r requirements.txt
    4. python main.py
"""

import os
import sys
import time
import logging
import argparse
from datetime import datetime, time as dt_time

import schedule
from dotenv import load_dotenv
from sqlalchemy.exc import IntegrityError

from db.models     import init_db, StockQuote, DividendEvent, DRIPSummary
from scraper.mystocks import scrape_watchlist, QuoteData, DividendData
from scraper.drip  import compute_portfolio_drip

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scraper.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

# ─── Config ───────────────────────────────────────────────────────────────────

load_dotenv()

DATABASE_URL        = os.getenv("DATABASE_URL", "sqlite:///mystocks_drip.db")
WATCH_LIST          = [t.strip().upper() for t in
                       os.getenv("WATCH_LIST", "SCOM,EQTY,KCB,EABL").split(",") if t.strip()]
INTERVAL_MINUTES    = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "10"))
TRADING_HOURS_ONLY  = os.getenv("TRADING_HOURS_ONLY", "true").lower() == "true"
VALIDATE_WITH_GROQ  = os.getenv("VALIDATE_WITH_GROQ", "false").lower() == "true"

# NSE trading hours: Mon–Fri 09:00–15:30 EAT
NSE_OPEN  = dt_time(9, 0)
NSE_CLOSE = dt_time(15, 30)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def is_trading_hours() -> bool:
    """Return True if current EAT time is within NSE trading hours."""
    now = datetime.now()
    if now.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    return NSE_OPEN <= now.time() <= NSE_CLOSE


def parse_portfolio_arg(arg: str) -> dict[str, float]:
    """Parse 'SCOM:500,EQTY:200' → {'SCOM': 500.0, 'EQTY': 200.0}"""
    portfolio = {}
    for item in arg.split(","):
        item = item.strip()
        if ":" in item:
            ticker, _, shares = item.partition(":")
            try:
                portfolio[ticker.strip().upper()] = float(shares.strip())
            except ValueError:
                logger.warning("Invalid portfolio entry: %s", item)
    return portfolio

# ─── Persistence ─────────────────────────────────────────────────────────────

def save_quotes(session_factory, quotes: list[QuoteData]):
    """Persist scraped quotes to the database."""
    with session_factory() as session:
        for q in quotes:
            row = StockQuote(
                ticker         = q.ticker,
                name           = q.name,
                sector         = (q.sector[:120] if q.sector else None),
                price          = q.price,
                change         = q.change,
                change_pct     = q.change_pct,
                open_price     = q.open_price,
                high           = q.high,
                low            = q.low,
                volume         = q.volume,
                previous_close = q.previous_close,
                eps            = q.eps,
                pe_ratio       = q.pe_ratio,
                dividend       = q.dividend,
                dividend_yield = q.dividend_yield,
                book_value     = q.book_value,
                market_cap     = q.market_cap,
                shares_issued  = q.shares_issued,
                scraped_at     = q.scraped_at,
            )
            session.add(row)
        session.commit()
    logger.info("Saved %d quotes to database", len(quotes))


def save_dividends(session_factory, dividends: list[DividendData]):
    """Upsert dividend events (skip duplicates by ticker+ex_date)."""
    saved = 0
    with session_factory() as session:
        for d in dividends:
            row = DividendEvent(
                ticker       = d.ticker,
                dividend_amt = d.amount,
                ex_date      = d.ex_date,
                pay_date     = d.pay_date,
                announcement = d.note,
            )
            try:
                session.merge(row)
                session.commit()
                saved += 1
            except IntegrityError:
                session.rollback()   # duplicate; ignore
    if saved:
        logger.info("Saved %d new dividend events", saved)


def save_drip_summary(session_factory, drip_results):
    """Persist DRIP computation results."""
    with session_factory() as session:
        for r in drip_results:
            row = DRIPSummary(
                ticker           = r.ticker,
                shares_held      = r.shares_held,
                current_price    = r.current_price,
                last_dividend    = r.last_dividend,
                total_dividend   = r.total_dividend,
                reinvest_shares  = r.reinvest_shares,
                leftover_cash    = r.leftover_cash,
                annual_yield_pct = r.annual_yield_pct,
                computed_date    = datetime.utcnow(),
            )
            session.add(row)
        session.commit()
    logger.info("Saved %d DRIP summary rows", len(drip_results))

# ─── Main scrape job ──────────────────────────────────────────────────────────

def run_scrape_job(
    tickers: list[str],
    session_factory,
    portfolio: dict[str, float] | None = None,
    respect_trading_hours: bool = True,
    validate_with_groq: bool = False,
):
    """
    One full scrape cycle:
      1. Check trading hours (optional).
      2. Scrape watchlist from mystocks.
      3. Validate data with Groq LLM (optional).
      4. Persist quotes + dividends.
      5. Compute and persist DRIP summary if portfolio is provided.
    """
    if respect_trading_hours and not is_trading_hours():
        logger.info("Outside NSE trading hours — skipping scrape")
        return

    logger.info("═══ Scrape cycle started — %s ═══", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("Tickers: %s", ", ".join(tickers))

    quotes, dividends = scrape_watchlist(tickers, full_detail=True, validate_with_groq=validate_with_groq)

    if not quotes:
        logger.warning("No data returned from scraper")
        return

    # Print a quick summary to stdout
    print(f"\n{'─'*60}")
    print(f"  {'TICKER':<8} {'PRICE':>10} {'CHANGE':>8} {'%':>7}  {'DIV':>8}")
    print(f"{'─'*60}")
    for q in quotes:
        print(
            f"  {q.ticker:<8} "
            f"KES {q.price or 0:>7.2f} "
            f"{('+' if (q.change or 0) >= 0 else '')}{q.change or 0:>7.2f} "
            f"{('+' if (q.change_pct or 0) >= 0 else '')}{q.change_pct or 0:>6.2f}%"
            f"  KES {q.dividend or 0:>6.2f}"
        )
    print(f"{'─'*60}\n")

    save_quotes(session_factory, quotes)
    if dividends:
        save_dividends(session_factory, dividends)

    # DRIP computation
    if portfolio:
        drip_results = compute_portfolio_drip(quotes, dividends, portfolio)
        if drip_results:
            save_drip_summary(session_factory, drip_results)
            print("  DRIP REINVESTMENT SUMMARY")
            print(f"  {'TICKER':<8} {'SHARES HELD':>12} {'NEW SHARES':>12} {'LEFTOVER':>12}")
            print(f"{'─'*60}")
            for r in drip_results:
                print(
                    f"  {r.ticker:<8} "
                    f"{r.shares_held:>12,.0f} "
                    f"{r.reinvest_shares:>12,.0f} "
                    f"KES {r.leftover_cash:>8.2f}"
                )
                print(f"  {'':8} → {r.note}")
            print(f"{'─'*60}\n")

    logger.info("═══ Scrape cycle complete ═══")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="MyStocks NSE scraper for DRIP application"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single scrape then exit (default: run on schedule)"
    )
    parser.add_argument(
        "--tickers", type=str, default=None,
        help="Comma-separated tickers to scrape (overrides .env WATCH_LIST)"
    )
    parser.add_argument(
        "--portfolio", type=str, default=None,
        help="Your holdings as TICKER:SHARES pairs e.g. SCOM:500,EQTY:200"
    )
    parser.add_argument(
        "--no-trading-hours-check", action="store_true",
        help="Scrape even outside NSE trading hours"
    )
    parser.add_argument(
        "--validate-with-groq", action="store_true",
        help="Use Groq LLM to validate scraped stock data"
    )
    args = parser.parse_args()

    # Resolve config
    tickers   = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] \
                if args.tickers else WATCH_LIST
    portfolio = parse_portfolio_arg(args.portfolio) if args.portfolio else None
    respect   = TRADING_HOURS_ONLY and not args.no_trading_hours_check
    validate  = args.validate_with_groq or VALIDATE_WITH_GROQ

    logger.info("Initialising database: %s", DATABASE_URL.split("://")[0])
    _, Session = init_db(DATABASE_URL)

    def job():
        run_scrape_job(tickers, Session, portfolio=portfolio, respect_trading_hours=respect, validate_with_groq=validate)

    if args.once:
        job()
    else:
        logger.info(
            "Scheduler started — interval: %d min | trading hours only: %s",
            INTERVAL_MINUTES, respect
        )
        schedule.every(INTERVAL_MINUTES).minutes.do(job)
        job()   # run immediately on start
        while True:
            schedule.run_pending()
            time.sleep(60)


if __name__ == "__main__":
    main()