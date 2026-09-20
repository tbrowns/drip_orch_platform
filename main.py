from fastapi import FastAPI, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import Date, cast

import os, signal
import threading
import time
import logging
import schedule
from dotenv import load_dotenv

from db.models import (
    init_db, StockQuote, Announcement, User, UserKYC, 
    UserPortfolio, PortfolioHolding, PaymentMethod, CDSAccount
)
from datetime import datetime
from core.security import (
    create_access_token,
    verify_token,
    hash_password,
    verify_password,
    oauth2_scheme,
)
from nse_scraper import NSEDatabaseScraper
from scraper.drip import (
    DEFAULT_BROKERAGE_RATE,
    DEFAULT_MIN_BROKERAGE_KES,
    RESIDENT_WHT_RATE,
    project_drip,
)
from scraper.models import quote_from_row, dividends_from_rows
from core.drip_schemas import (
    MAX_YEARS,
    PaymentsPerYear,
    DripAssumptionsIn,
    DripPortfolioAggregateOut,
    DripPortfolioPositionOut,
    DripPortfolioResponse,
    DripSimulateRequest,
    DripSimulateResponse,
    InputResolutionError,
    SkippedPositionOut,
    projection_to_response,
    resolve_market_inputs,
)

app = FastAPI()

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing from environment variables")

_, session_factory = init_db(DATABASE_URL)

# ─── Logger ──────────────────────────────────────────────────────────────────

logger = logging.getLogger("stock_quote_scheduler")

# ─── Background Scheduler ────────────────────────────────────────────────────

scheduler_thread = None
scheduler_running = False
RAPID_API_KEY = os.getenv("RAPID_API_KEY")


def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean env var. Accepts 1/true/yes/on, any case."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Starting the app normally scrapes live.mystocks.co.ke immediately and then
# every SCRAPE_INTERVAL_MINUTES. That is right in production and wrong almost
# everywhere else: it blocks startup on a third party being up, hits their
# site on every local run and every test, and rewrites stock_quotes underneath
# whatever you were about to measure. SKIP_SCRAPE=true turns the whole thing
# off. Default is unchanged, so production behaves exactly as before.
SKIP_SCRAPE = _env_flag("SKIP_SCRAPE", default=False)
# NSE quotes settle once a day, so a 10-minute poll was 60x more traffic than
# the data justified - and the kind of thing that gets an IP rate-limited.
SCRAPE_INTERVAL_MINUTES = max(1, int(os.getenv("SCRAPE_INTERVAL_MINUTES", "60")))


def _run_scraper_job(scraper: NSEDatabaseScraper) -> None:
    try:
        scraper.run_once()
    except Exception:
        logger.exception("Scheduled NSEDatabaseScraper run failed")


def scheduler_worker():
    """Background worker that runs scheduled tasks."""
    global scheduler_running
    logger.info("Stock quote scheduler started")
    
    while scheduler_running:
        try:
            schedule.run_pending()
        except Exception:
            logger.exception("Error while running scheduled tasks")
        time.sleep(1)


def start_scheduler():
    """Start the background scheduler."""
    global scheduler_thread, scheduler_running

    if SKIP_SCRAPE:
        logger.info(
            "SKIP_SCRAPE is set - no startup scrape and no scheduler. "
            "The API serves whatever is already in the database."
        )
        return

    if scheduler_running:
        logger.warning("Scheduler already running")
        return
    
    scheduler_running = True
    
    scraper = NSEDatabaseScraper(session_factory=session_factory, logger=logger)
    schedule.every(SCRAPE_INTERVAL_MINUTES).minutes.do(lambda: _run_scraper_job(scraper))
    logger.info(
        "Scheduled NSEDatabaseScraper to run every %d minutes", SCRAPE_INTERVAL_MINUTES
    )

    logger.info("Running initial NSEDatabaseScraper scrape on startup")
    _run_scraper_job(scraper)
    
    # Start scheduler in background thread
    scheduler_thread = threading.Thread(target=scheduler_worker, daemon=True)
    scheduler_thread.start()


def stop_scheduler():
    """Stop the background scheduler."""
    global scheduler_running
    scheduler_running = False
    if scheduler_thread:
        scheduler_thread.join(timeout=5)
    schedule.clear()
    logger.info("Stock quote scheduler stopped")


class UserCreate(BaseModel):
    full_name: str
    username: str
    email: EmailStr
    password: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: int
    full_name: str
    username: str
    email: str
    

    class Config:
        from_attributes = True


class PortfolioHoldingResponse(BaseModel):
    id: int
    ticker: str
    shares_owned: float
    average_buy_price: float
    total_invested: float
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class UserPortfolioResponse(BaseModel):
    id: int
    user_id: int
    name: str
    cash_balance: float
    created_at: str
    holdings: list[PortfolioHoldingResponse]

    class Config:
        from_attributes = True


class PaymentMethodResponse(BaseModel):
    id: int
    method_type: str
    phone_number: str | None = None
    bank_name: str | None = None
    account_number: str | None = None
    account_name: str | None = None
    is_default: bool
    is_verified: bool
    created_at: str

    class Config:
        from_attributes = True


class CDSAccountResponse(BaseModel):
    id: int
    cds_number: str
    status: str
    created_at: str

    class Config:
        from_attributes = True

class UserKYCResponse(BaseModel):
    id: int
    user_id: int
    id_number: str
    kra_pin: str | None = None
    phone_number: str | None = None
    date_of_birth: str | None = None
    nationality: str
    county: str | None = None
    address: str | None = None
    verification_status: str
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True

class UserDetailResponse(BaseModel):
    id: int
    full_name: str
    username: str
    email: str
    created_at: str
    updated_at: str
    kyc: UserKYCResponse | None = None
    payment_methods: list[PaymentMethodResponse] = []
    cds_accounts: list[CDSAccountResponse] = []
    portfolios: list[UserPortfolioResponse] = []

    class Config:
        from_attributes = True


def get_current_user(token: str = Depends(oauth2_scheme)):
    token_data = verify_token(token)

    with session_factory() as session:
        user = (
            session.query(User)
            .filter(User.id == token_data["user_id"])
            .first()
        )

        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found"
            )
        
        portfolio = (session.query(UserPortfolio).filter(UserPortfolio.user_id == user.id).first())
        if portfolio:
            user.portfolio = portfolio

        return user

@app.get("/")
def read_root():
    return {"Hello": "World"}

@app.post("/auth/signup")
def signup(user: UserCreate):
    with session_factory() as session:
        existing_email = (
            session.query(User)
            .filter(User.email == user.email)
            .first()
        )

        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

        new_user = User(
            full_name=user.full_name,
            username=user.username,
            email=user.email,
            password_hash=hash_password(user.password),
        )

        session.add(new_user)
        session.commit()
        session.refresh(new_user)

        access_token = create_access_token(
            data={"user_id": new_user.id}
        )

        return {
            "message": "User signed up successfully",
            "access_token": access_token,
            "token_type": "bearer"
        }

@app.post("/auth/login")
def login(user: UserLogin):
    with session_factory() as session:
        existing_user = (
            session.query(User)
            .filter(User.email == user.email)
            .first()
        )

        if not existing_user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )

        password_is_valid = verify_password(
            user.password,
            existing_user.password_hash
        )

        if not password_is_valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )

        access_token = create_access_token(
            data={"user_id": existing_user.id}
        )

        return {
            "message": "User logged in successfully",
            "access_token": access_token,
            "token_type": "bearer"
        }

@app.get("/users/me", response_model=UserDetailResponse)
def read_me(current_user: User = Depends(get_current_user)):
    """
    Get the current authenticated user with all related data:
    - KYC information
    - Payment methods
    - CDS accounts
    - Portfolios with holdings
    """
    with session_factory() as session:
        user = (
            session.query(User)
            .filter(User.id == current_user.id)
            .first()
        )

        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # Get all portfolios with their holdings
        portfolios = session.query(UserPortfolio).filter(
            UserPortfolio.user_id == user.id
        ).all()

        # Serialize portfolios with holdings
        portfolio_responses = []
        for portfolio in portfolios:
            holdings = session.query(PortfolioHolding).filter(
                PortfolioHolding.portfolio_id == portfolio.id
            ).all()
            
            portfolio_responses.append(
                UserPortfolioResponse(
                    id=portfolio.id,
                    user_id=portfolio.user_id,
                    name=portfolio.name,
                    cash_balance=float(portfolio.cash_balance),
                    created_at=portfolio.created_at.isoformat(),
                    holdings=[
                        PortfolioHoldingResponse(
                            id=h.id,
                            ticker=h.ticker,
                            shares_owned=float(h.shares_owned),
                            average_buy_price=float(h.average_buy_price),
                            total_invested=float(h.total_invested),
                            created_at=h.created_at.isoformat(),
                            updated_at=h.updated_at.isoformat()
                        )
                        for h in holdings
                    ]
                )
            )

        # Get KYC information
        kyc = session.query(UserKYC).filter(UserKYC.user_id == user.id).first()
        kyc_response = None
        if kyc:
            kyc_response = UserKYCResponse(
                id=kyc.id,
                user_id=kyc.user_id,
                id_number=kyc.id_number,
                kra_pin=kyc.kra_pin,
                phone_number=kyc.phone_number,
                date_of_birth=kyc.date_of_birth.isoformat() if kyc.date_of_birth else None,
                nationality=kyc.nationality,
                county=kyc.county,
                address=kyc.address,
                verification_status=kyc.verification_status,
                created_at=kyc.created_at.isoformat(),
                updated_at=kyc.updated_at.isoformat()
            )

        # Get payment methods
        payment_methods = session.query(PaymentMethod).filter(
            PaymentMethod.user_id == user.id
        ).all()
        payment_responses = [
            PaymentMethodResponse(
                id=pm.id,
                method_type=pm.method_type,
                phone_number=pm.phone_number,
                bank_name=pm.bank_name,
                account_number=pm.account_number,
                account_name=pm.account_name,
                is_default=pm.is_default,
                is_verified=pm.is_verified,
                created_at=pm.created_at.isoformat()
            )
            for pm in payment_methods
        ]

        # Get CDS accounts
        cds_accounts = session.query(CDSAccount).filter(
            CDSAccount.user_id == user.id
        ).all()
        cds_responses = [
            CDSAccountResponse(
                id=cds.id,
                cds_number=cds.cds_number,
                status=cds.status,
                created_at=cds.created_at.isoformat()
            )
            for cds in cds_accounts
        ]

        return UserDetailResponse(
            id=user.id,
            full_name=user.full_name,
            username=user.username,
            email=user.email,
            created_at=user.created_at.isoformat(),
            updated_at=user.updated_at.isoformat(),
            kyc=kyc_response,
            payment_methods=payment_responses,
            cds_accounts=cds_responses,
            portfolios=portfolio_responses
        )

@app.get("/kyc/me")
def get_current_kyc(current_user: User = Depends(get_current_user)):
    with session_factory() as session:
        current_kyc = (
            session.query(UserKYC)
            .filter(UserKYC.user_id == current_user.id)
            .first()
        )

        if not current_kyc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="KYC information not found"
            )

        return {
            "id": current_kyc.id,
            "user_id": current_kyc.user_id,
            "id_number": current_kyc.id_number,
            "kra_pin": current_kyc.kra_pin,
            "verification_status": current_kyc.verification_status
        }

@app.get("/quotes")
def get_quotes_from_db():
    with session_factory() as session:
        quotes = session.query(
            StockQuote.ticker,
            StockQuote.name,
            StockQuote.sector,
            StockQuote.previous,
            StockQuote.open,
            StockQuote.volume,
            StockQuote.turnover,
        ).all()
        return {
            "Quotes": [
                {
                    "ticker": q[0],
                    "name": q[1],
                    "sector": q[2],
                    "previous": q[3],
                    "open": q[4],
                    "volume": q[5],
                    "turnover": q[6],
                    
                }
            for q in quotes
            ]
        }

def _serialize_stock_quote(quote: StockQuote) -> dict:
    return {
        "ticker": quote.ticker,
        "name": quote.name,
        "sector": quote.sector,
        "previous": quote.previous,
        "open": quote.open,
        "average": quote.average,
        "deals": quote.deals,
        "volume": quote.volume,
        "turnover": quote.turnover,
        "day_range": quote.day_range,
        "week_52_range": quote.week_52_range,
        "average_volume": quote.average_volume,
        "beta": quote.beta,
        "shares_issued": quote.shares_issued,
        "year_end": quote.year_end,
        "par_value": quote.par_value,
        "profile": quote.profile,
        "error": quote.error,
        "scraped_at": quote.scraped_at.isoformat() if quote.scraped_at else None,
    }

def _serialize_date(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)

@app.get("/detailed-quotes")
def get_all_quotes():
    with session_factory() as session:
        quotes = session.query(StockQuote).all()
        return {"All Quotes": [_serialize_stock_quote(q) for q in quotes]}


@app.get("/detailed-quotes/{ticker}")
def get_detailed_quote(ticker: str):
    with session_factory() as session:
        quote = session.query(StockQuote).filter(StockQuote.ticker == ticker.upper()).first()
        if not quote:
            raise HTTPException(status_code=404, detail=f"Quote for ticker '{ticker}' not found")
        return _serialize_stock_quote(quote)


@app.get("/dividends/upcoming")
def get_dividends_from_db():
    with session_factory() as session:
        announcement_date = cast(Announcement.date, Date)
        dividends = session.query(
            Announcement.ticker,
            Announcement.company,
            Announcement.dividend_type,
            announcement_date,
            Announcement.amount_kes,
            Announcement.event_type,
            Announcement.description,
        
        ).filter(announcement_date >= datetime.now().date()).all()
        return {
            "Dividends": [
                {
                    "ticker": d[0],
                    "company": d[1],
                    "dividend_type": d[2],
                    "date": _serialize_date(d[3]),
                    "amount_kes": d[4],
                    "event_type": d[5],
                    "description": d[6],
                }
                for d in dividends
            ]
        }

# ─── DRIP ────────────────────────────────────────────────────────────────────

def _load_market_data(session, ticker: str | None):
    """
    Return ``(QuoteData | None, list[DividendData])`` for ``ticker`` from the
    scraped tables (one StockQuote row per ticker; every Announcement row for
    it, newest first). Nothing is looked up when ``ticker`` is None.
    """
    if not ticker:
        return None, []
    quote_row = session.query(StockQuote).filter(StockQuote.ticker == ticker).first()
    announcement_rows = (
        session.query(Announcement)
        .filter(Announcement.ticker == ticker)
        .order_by(Announcement.date.desc())
        .all()
    )
    quote = quote_from_row(quote_row) if quote_row else None
    return quote, dividends_from_rows(announcement_rows)


def _run_projection(resolved, shares_held: float, years: int, payments_per_year: int,
                    assumptions: DripAssumptionsIn, price_growth_rate: float,
                    dividend_growth_rate: float) -> DripSimulateResponse:
    try:
        projection = project_drip(
            initial_shares=shares_held,
            price=resolved.price,
            dividend_per_share_annual=resolved.dividend_per_share_annual,
            payments_per_year=payments_per_year,
            years=years,
            assumptions=assumptions.to_engine(),
            price_growth_rate=price_growth_rate,
            dividend_growth_rate=dividend_growth_rate,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    return projection_to_response(
        resolved, projection, assumptions, shares_held, years, payments_per_year,
        price_growth_rate, dividend_growth_rate,
    )


@app.post("/drip/simulate", response_model=DripSimulateResponse)
def simulate_drip(request: DripSimulateRequest, current_user: User = Depends(get_current_user)):
    """
    Project what reinvesting dividends compounds into on the NSE.

    Give a ``ticker`` and the latest scraped quote (previous close) and the
    de-duplicated dividend announcements are used; pass ``price`` and/or
    ``dividend_per_share`` (annual, gross, KES) to override or when no ticker
    is given. The engine deducts withholding tax (5 % resident default),
    charges brokerage plus levies on each purchase (2.12 % approx., KES 100
    minimum), buys whole shares only and carries leftover cash forward.
    """
    with session_factory() as session:
        quote, dividends = _load_market_data(session, request.ticker)

    try:
        resolved = resolve_market_inputs(
            request.ticker, request.price, request.dividend_per_share, quote, dividends,
        )
    except InputResolutionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    return _run_projection(
        resolved, request.shares_held, request.years, request.payments_per_year,
        request.assumptions, request.price_growth_rate, request.dividend_growth_rate,
    )


@app.get("/drip/portfolio", response_model=DripPortfolioResponse)
def portfolio_drip(
    years: int = Query(10, ge=1, le=MAX_YEARS),
    payments_per_year: PaymentsPerYear = Query(1),
    price_growth_rate: float = Query(0.0, gt=-1, le=1),
    dividend_growth_rate: float = Query(0.0, gt=-1, le=1),
    withholding_tax_rate: float = Query(RESIDENT_WHT_RATE, ge=0, lt=1),
    brokerage_rate: float = Query(DEFAULT_BROKERAGE_RATE, ge=0, lt=1),
    min_brokerage_kes: float = Query(DEFAULT_MIN_BROKERAGE_KES, ge=0),
    include_periods: bool = Query(True, description="Set false to omit the per-period rows for each position."),
    current_user: User = Depends(get_current_user),
):
    """
    Run the DRIP projection over every holding in the current user's stored
    portfolio (``user_portfolios`` / ``portfolio_holdings``), using scraped
    prices and announcements. Holdings without a usable quote or dividend are
    listed under ``skipped`` with the reason. 404 when the user has no
    portfolio or it has no holdings.
    """
    assumptions = DripAssumptionsIn(
        withholding_tax_rate=withholding_tax_rate,
        brokerage_rate=brokerage_rate,
        min_brokerage_kes=min_brokerage_kes,
    )

    with session_factory() as session:
        portfolio = (
            session.query(UserPortfolio)
            .filter(UserPortfolio.user_id == current_user.id)
            .order_by(UserPortfolio.id)
            .first()
        )
        if not portfolio:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No portfolio found for this user. Add a portfolio with holdings first, "
                       "or use POST /drip/simulate with explicit values.",
            )

        holdings = (
            session.query(PortfolioHolding)
            .filter(PortfolioHolding.portfolio_id == portfolio.id)
            .order_by(PortfolioHolding.ticker)
            .all()
        )
        if not holdings:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Portfolio '{portfolio.name}' has no holdings to project. "
                       "Add holdings first, or use POST /drip/simulate.",
            )

        positions: list[DripPortfolioPositionOut] = []
        skipped: list[SkippedPositionOut] = []
        for holding in holdings:
            ticker = (holding.ticker or "").upper()
            shares = float(holding.shares_owned or 0)
            if shares <= 0:
                skipped.append(SkippedPositionOut(ticker=ticker, shares_held=shares, reason="shares_owned is zero"))
                continue

            quote, dividends = _load_market_data(session, ticker)
            try:
                resolved = resolve_market_inputs(ticker, None, None, quote, dividends)
            except InputResolutionError as exc:
                skipped.append(SkippedPositionOut(ticker=ticker, shares_held=shares, reason=exc.detail))
                continue

            result = _run_projection(
                resolved, shares, years, payments_per_year, assumptions,
                price_growth_rate, dividend_growth_rate,
            )
            positions.append(DripPortfolioPositionOut(
                ticker=ticker,
                name=resolved.name,
                inputs=result.inputs,
                totals=result.totals,
                periods=result.periods if include_periods else [],
            ))

        portfolio_id = portfolio.id
        portfolio_name = portfolio.name
        cash_balance = float(portfolio.cash_balance or 0)

    aggregate = DripPortfolioAggregateOut(
        positions=len(positions),
        initial_value=round(sum(p.inputs.shares_held * p.inputs.price for p in positions), 2),
        ending_value=round(sum(p.totals.ending_value for p in positions), 2),
        vs_no_reinvest_value=round(sum(p.totals.vs_no_reinvest_value for p in positions), 2),
        reinvestment_gain=round(sum(p.totals.reinvestment_gain for p in positions), 2),
        total_net_dividends=round(sum(p.totals.total_net_dividends for p in positions), 2),
        total_tax_paid=round(sum(p.totals.total_tax_paid for p in positions), 2),
        total_fees_paid=round(sum(p.totals.total_fees_paid for p in positions), 2),
    )

    return DripPortfolioResponse(
        portfolio_id=portfolio_id,
        portfolio_name=portfolio_name,
        cash_balance=cash_balance,
        years=years,
        payments_per_year=payments_per_year,
        assumptions=assumptions,
        positions=positions,
        skipped=skipped,
        aggregate=aggregate,
    )


@app.get("/shutdown")
async def shutdown():
    os.kill(os.getpid(), signal.SIGTERM)
    return {"message": "Shutting down..."}

# ─── Startup and Shutdown Events ─────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    """Start the background scheduler when the server starts."""
    start_scheduler()

@app.on_event("shutdown")
async def shutdown_event():
    """Stop the background scheduler when the server stops."""
    stop_scheduler()
