from fastapi import FastAPI, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr

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
    
    if scheduler_running:
        logger.warning("Scheduler already running")
        return
    
    scheduler_running = True
    
    scraper = NSEDatabaseScraper(session_factory=session_factory, logger=logger)
    schedule.every(10).minutes.do(lambda: _run_scraper_job(scraper))
    logger.info("Scheduled NSEDatabaseScraper to run every 10 minutes")

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

@app.get("/detailed-quotes")
def get_all_quotes():
    with session_factory() as session:
        quotes = session.query(StockQuote).all()
        return {"All Quotes": [_serialize_stock_quote(q) for q in quotes]}


@app.get("/dividends/upcoming")
def get_dividends_from_db():
    with session_factory() as session:
        dividends = session.query(
            Announcement.ticker,
            Announcement.company,
            Announcement.dividend,
            Announcement.date,
            Announcement.amount_kes,
            Announcement.event_type,
            Announcement.description,
        
        ).filter(Announcement.date >= datetime.now().date()).all()
        return {
            "Dividends": [
                {
                    "ticker": d[0],
                    "company": d[1],
                    "dividend": d[2],
                    "date": d[3].isoformat() if d[3] else None,
                    "amount_kes": d[4],
                    "event_type": d[5],
                    "description": d[6],
                }
                for d in dividends
            ]
        }

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