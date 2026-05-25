"""
db/models.py — SQLAlchemy ORM models for the DRIP scraper.
Supports SQLite, PostgreSQL (Supabase), and MySQL.
"""

from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    DateTime, Date, Text, UniqueConstraint, ForeignKey, Boolean, Numeric
)
from sqlalchemy.orm import relationship, declarative_base, sessionmaker

from datetime import datetime, UTC

Base = declarative_base()

class User(Base):
    """
    Basic user account information.
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)

    username = Column(String(50), unique=True, nullable=False, index=True)
    full_name = Column(String(120), nullable=False)
    email = Column(String(120), unique=True, nullable=False, index=True)

    # Do not store plain passwords
    password_hash = Column(String(255), nullable=False)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False
    )

    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False
    )

    kyc = relationship("UserKYC", back_populates="user", uselist=False)
    payment_methods = relationship("PaymentMethod", back_populates="user")
    cds_accounts = relationship("CDSAccount", back_populates="user")
    portfolios = relationship("UserPortfolio", back_populates="user")


class UserKYC(Base):
    """
    User KYC information.
    One user should have one KYC record.
    """
    __tablename__ = "user_kyc"

    id = Column(Integer, primary_key=True, autoincrement=True)

    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False
    )

    id_number = Column(String(30), unique=True, nullable=False)
    kra_pin = Column(String(30), unique=True, nullable=True)

    phone_number = Column(String(20), unique=True, nullable=True)
    date_of_birth = Column(DateTime(timezone=True), nullable=True)

    nationality = Column(String(80), default="Kenyan")
    county = Column(String(80), nullable=True)
    address = Column(String(255), nullable=True)

    # pending, verified, rejected
    verification_status = Column(String(30), default="pending", nullable=False)

    id_front_image_url = Column(String(500), nullable=True)
    id_back_image_url = Column(String(500), nullable=True)
    selfie_image_url = Column(String(500), nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False
    )

    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False
    )

    user = relationship("User", back_populates="kyc")


class PaymentMethod(Base):
    """
    User payment methods such as M-Pesa or bank account.
    """
    __tablename__ = "payment_methods"

    id = Column(Integer, primary_key=True, autoincrement=True)

    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # mpesa, bank, card
    method_type = Column(String(30), nullable=False)

    # For M-Pesa
    phone_number = Column(String(20), nullable=True)

    # For bank
    bank_name = Column(String(120), nullable=True)
    account_number = Column(String(50), nullable=True)
    account_name = Column(String(120), nullable=True)

    is_default = Column(Boolean, default=False, nullable=False)
    is_verified = Column(Boolean, default=False, nullable=False)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False
    )

    user = relationship("User", back_populates="payment_methods")

class CDSAccount(Base):
    """
    User CDS account details.
    Can be mocked during MVP.
    """
    __tablename__ = "cds_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)

    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    cds_number = Column(String(50), unique=True, nullable=False)

    # mock, pending, active, suspended
    status = Column(String(30), default="mock", nullable=False)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False
    )

    user = relationship("User", back_populates="cds_accounts")

class UserPortfolio(Base):
    """
    User's investment portfolio.
    """
    __tablename__ = "user_portfolios"

    id = Column(Integer, primary_key=True, autoincrement=True)

    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    name = Column(String(100), default="Main Portfolio", nullable=False)

    cash_balance = Column(Numeric(18, 2), default=0, nullable=False)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False
    )

    user = relationship("User", back_populates="portfolios")
    holdings = relationship("PortfolioHolding", back_populates="portfolio")


class PortfolioHolding(Base):
    """
    Individual stock holdings inside a user's portfolio.
    Example: user owns 12.5 shares of SCOM.
    """
    __tablename__ = "portfolio_holdings"

    id = Column(Integer, primary_key=True, autoincrement=True)

    portfolio_id = Column(
        Integer,
        ForeignKey("user_portfolios.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    ticker = Column(String(20), nullable=False, index=True)

    shares_owned = Column(Numeric(18, 6), default=0, nullable=False)
    average_buy_price = Column(Numeric(18, 4), default=0, nullable=False)

    total_invested = Column(Numeric(18, 2), default=0, nullable=False)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False
    )

    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False
    )

    portfolio = relationship("UserPortfolio", back_populates="holdings")

    __table_args__ = (
        UniqueConstraint(
            "portfolio_id",
            "ticker",
            name="uq_portfolio_ticker"
        ),
    )


class StockQuote(Base):
    """
    One row per scrape per ticker.
    Stores the most recent scraped stock quote from mystocks.
    """
    __tablename__ = "stock_quotes"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    ticker         = Column(String(20), unique=True, nullable=False, index=True)
    name           = Column(String(120), nullable=True)
    sector         = Column(String(80), nullable=True)
    previous       = Column(String(64), nullable=True)
    open           = Column(String(64), nullable=True)
    average        = Column(String(64), nullable=True)
    deals          = Column(String(64), nullable=True)
    volume         = Column(String(64), nullable=True)
    turnover       = Column(String(64), nullable=True)
    day_range      = Column(String(64), nullable=True)
    week_52_range  = Column(String(64), nullable=True)
    average_volume = Column(String(64), nullable=True)
    beta           = Column(String(64), nullable=True)
    shares_issued  = Column(String(64), nullable=True)
    year_end       = Column(String(64), nullable=True)
    par_value      = Column(String(64), nullable=True)
    profile        = Column(Text, nullable=True)
    error          = Column(String(255), nullable=True)
    scraped_at     = Column(DateTime, default=lambda: datetime.now(UTC), nullable=False, index=True)

    def __repr__(self):
        return f"<StockQuote {self.ticker} scraped_at={self.scraped_at:%Y-%m-%d %H:%M}>"


class Announcement(Base):
    """
    Corporate announcements scraped from the calendar page.
    """
    __tablename__ = "announcements"
    __table_args__ = (
        UniqueConstraint("date", "ticker", "description", name="uq_announcement_unique"),
    )

    id             = Column(Integer, primary_key=True, autoincrement=True)
    date           = Column(String(50), nullable=False, index=True)
    ticker         = Column(String(20), nullable=False, index=True)
    company        = Column(String(120), nullable=True)
    event_type     = Column(String(80), nullable=True)
    amount_kes     = Column(String(50), nullable=True)
    dividend_type  = Column(String(50), nullable=True)
    description    = Column(Text, nullable=True)
    scraped_at     = Column(DateTime, default=lambda: datetime.now(UTC), nullable=False)

    def __repr__(self):
        return f"<Announcement {self.ticker} {self.date} {self.event_type}>"


class DividendEvent(Base):
    """
    Tracks dividend announcements found on the stock page.
    Used by the DRIP engine to calculate reinvestment.
    """
    __tablename__ = "dividend_events"
    __table_args__ = (
        UniqueConstraint("ticker", "ex_date", name="uq_ticker_exdate"),
    )

    id             = Column(Integer, primary_key=True, autoincrement=True)
    ticker         = Column(String(20), nullable=False, index=True)
    dividend_amt   = Column(Float, nullable=False)   # KES per share
    ex_date        = Column(Date)
    pay_date       = Column(Date)
    announcement   = Column(Text)
    scraped_at     = Column(DateTime, default=lambda: datetime.now(UTC))

    def __repr__(self):
        return f"<DividendEvent {self.ticker} KES {self.dividend_amt} ex={self.ex_date}>"


class DRIPSummary(Base):
    """
    Computed DRIP metrics per ticker, refreshed each scrape cycle.
    Answers: "if I hold X shares, how many new shares can I buy
    with this dividend?"
    """
    __tablename__ = "drip_summary"
    __table_args__ = (
        UniqueConstraint("ticker", "computed_date", name="uq_ticker_date"),
    )

    id               = Column(Integer, primary_key=True, autoincrement=True)
    ticker           = Column(String(20), nullable=False, index=True)
    shares_held      = Column(Float, default=0)          # from user portfolio
    current_price    = Column(Float)
    last_dividend    = Column(Float)                     # KES/share
    total_dividend   = Column(Float)                     # shares_held × dividend
    reinvest_shares  = Column(Float)                     # total_dividend / price
    leftover_cash    = Column(Float)                     # remainder after whole shares
    annual_yield_pct = Column(Float)
    computed_date    = Column(DateTime, default=lambda: datetime.now(UTC), index=True)

    def __repr__(self):
        return f"<DRIPSummary {self.ticker} +{self.reinvest_shares:.4f} shares>"


def init_db(database_url: str):
    """Create engine, tables, and return a Session factory."""
    engine = create_engine(
        database_url,
        pool_pre_ping=True,        # tests connection before using it
        pool_recycle=300,          # recycle connections every 5 min
        connect_args={"connect_timeout": 10},
        echo=False,
    )

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session