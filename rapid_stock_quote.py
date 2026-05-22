import requests
import os
import sys
import time
import logging
import argparse
from datetime import datetime, UTC

import schedule
from dotenv import load_dotenv
from sqlalchemy.exc import IntegrityError

from db.models     import init_db, StockQuote, DividendEvent, DRIPSummary
from scraper.drip  import compute_portfolio_drip

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///mystocks_drip.db")
RAPID_API_KEY = os.getenv("RAPID_API_KEY")

_,session = init_db(DATABASE_URL)

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

def get_quotes():
    url = "https://nairobi-stock-exchange-nse.p.rapidapi.com/stocks"

    headers = {
        "x-rapidapi-key": RAPID_API_KEY,
        "x-rapidapi-host": "nairobi-stock-exchange-nse.p.rapidapi.com",
        "Content-Type": "application/json"
    }

    response = requests.get(url, headers=headers)

    return response.json()["data"]


class APIStockQuote:
    """Scraper for Nairobi Stock Exchange (NSE) stock quotes."""
    
    API_URL = "https://nairobi-stock-exchange-nse.p.rapidapi.com/stocks"
    API_HOST = "nairobi-stock-exchange-nse.p.rapidapi.com"
    
    def __init__(self, api_key, session_factory, logger=None):
        """
        Initialize the scraper.
        
        Args:
            api_key: RapidAPI key for NSE API access
            session_factory: SQLAlchemy session factory
            logger: Logger instance (optional)
        """
        self.api_key = api_key
        self.session_factory = session_factory
        self.logger = logger or logging.getLogger(self.__class__.__name__)
    
    def get_quotes(self):
        """Fetch stock quotes from NSE API."""
        headers = {
            "x-rapidapi-key": self.api_key,
            "x-rapidapi-host": self.API_HOST,
            "Content-Type": "application/json"
        }
        
        response = requests.get(self.API_URL, headers=headers)
        response.raise_for_status()
        
        return response.json()["data"]
    
    @staticmethod
    def to_float(value):
        """Convert string value to float, handling None and formatted numbers."""
        if value is None:
            return None
        
        value = str(value).strip()
        value = value.replace(",", "")
        value = value.replace("%", "")
        value = value.replace("+", "")
        
        if value == "":
            return None
        
        return float(value)
    
    @staticmethod
    def to_int(value):
        """Convert string value to int, handling None and formatted numbers."""
        if value is None:
            return None
        return int(float(str(value).replace(",", "")))
    
    def save_quotes(self, quotes):
        """Save or update stock quotes in the database."""
        with self.session_factory() as session:
            for q in quotes:
                ticker = q["ticker"]
                
                existing = (
                    session.query(StockQuote)
                    .filter(StockQuote.ticker == ticker)
                    .first()
                )
                
                if existing:
                    existing.name = q.get("name")
                    existing.price = self.to_float(q.get("price"))
                    existing.change_pct = q.get("change")
                    existing.volume = self.to_int(q.get("volume"))
                    existing.scraped_at = datetime.utcnow()
                
                else:
                    row = StockQuote(
                        ticker=ticker,
                        name=q.get("name"),
                        sector=None,
                        price=self.to_float(q.get("price")),
                        change=None,
                        change_pct=self.to_float(q.get("change")),
                        open_price=None,
                        high=None,
                        low=None,
                        volume=self.to_int(q.get("volume")),
                        previous_close=None,
                        eps=None,
                        pe_ratio=None,
                        dividend=None,
                        dividend_yield=None,
                        book_value=None,
                        market_cap=None,
                        shares_issued=None,
                        scraped_at=datetime.now(UTC),
                    )
                    
                    session.add(row)
            
            session.commit()
        self.logger.info("Saved %d quotes to database", len(quotes))
    
    def run(self):
        """Fetch and save stock quotes."""
        quotes = self.get_quotes()
        self.save_quotes(quotes)
        return quotes


if __name__ == "__main__":
    scraper = APIStockQuote(api_key=RAPID_API_KEY, session_factory=session, logger=logger)
    quotes = scraper.run()
    print(quotes)

