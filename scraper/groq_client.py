import json
import logging
from typing import List
from groq import Groq
from tenacity import retry, stop_after_attempt, wait_exponential

import os
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()  # Load environment variables from .env file

# Initialize Groq client (requires GROQ_API_KEY env var)
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# ---------- System Prompt for Stock Data Validation ----------
SYSTEM_PROMPT = """You are a financial data validation assistant for NSE (Nairobi Securities Exchange) stock data.
You will receive raw extracted stock quote or dividend data. Validate and clean it strictly:

For STOCK QUOTES, return a JSON object:
{
  "ticker": string,              # NSE ticker code (e.g., SCOM, EQTY) – must be uppercase, 3-6 chars
  "valid": boolean,              # true only if ticker is valid and price >= 0
  "name": string,                # company name, max 120 chars
  "sector": string,              # industry sector, max 120 chars - must be a known NSE sector or null
  "price": number,               # last traded price in KES, must be > 0 or null
  "dividend": number,            # dividend per share in KES, must be >= 0 or null
  "change_pct": number,          # percent change, can be negative or null
  "warnings": string[]           # data quality issues found
}

For DIVIDEND DATA, return a JSON object:
{
  "ticker": string,              # must match the stock ticker
  "valid": boolean,              # true only if amount > 0
  "amount": number,              # dividend amount in KES, must be > 0
  "ex_date": string,             # ISO date (YYYY-MM-DD) or null
  "warnings": string[]           # data quality issues
}

Rules:
- Set "valid" to false if: ticker is invalid, price/amount is negative, required fields are missing.
- If a field cannot be inferred, set it to null. DO NOT guess or fabricate values.
- Include warnings for suspicious values (e.g., extreme price changes, unusually high dividends).
- Round numerical values to 2 decimal places.
"""

def build_quote_validation_prompt(ticker: str, data: str) -> str:
    """Build a prompt to validate a stock quote."""
    return f"""Validate this stock quote data:
Ticker: {ticker}
Raw data: {data}

Return a single JSON object (not an array) conforming to the STOCK QUOTES schema."""

def build_dividend_validation_prompt(ticker: str, data: str) -> str:
    """Build a prompt to validate dividend data."""
    return f"""Validate this dividend event data:
Ticker: {ticker}
Raw data: {data}

Return a single JSON object (not an array) conforming to the DIVIDEND DATA schema."""

# ---------- API wrapper ----------
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def call_groq_validation(prompt: str) -> str:
    """Send validation prompt to Groq and return the response."""
    logger.debug("Calling Groq for data validation")
    response = client.chat.completions.create(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],  # Ensure we get a JSON object,
        model="openai/gpt-oss-120b",
        response_format={"type": "json_object"}
    )
    return response.choices[0].message.content

# ---------- Validation functions ----------
def validate_quote_data(ticker: str, quote_dict: dict) -> tuple[bool, dict, List[str]]:
    """
    Validate a quote using Groq.
    Returns (is_valid, cleaned_data, warnings).
    """
    try:
        data_str = json.dumps(quote_dict)
        prompt = build_quote_validation_prompt(ticker, data_str)
        response_str = call_groq_validation(prompt)
        result = json.loads(response_str)
        
        is_valid = result.get("valid", False)
        warnings = result.get("warnings", [])
        
        if warnings:
            logger.warning("Validation warnings for %s: %s", ticker, "; ".join(warnings))
        
        # Extract only the validated fields
        cleaned = {
            "ticker": result.get("ticker", ticker),
            "name": result.get("name"),
            "sector": result.get("sector"),
            "price": result.get("price"),
            "dividend": result.get("dividend"),
            "change_pct": result.get("change_pct"),
        }
        
        return is_valid, cleaned, warnings
    except Exception as exc:
        logger.error("Groq validation failed for %s: %s", ticker, exc)
        return False, {}, [str(exc)]

def validate_dividend_data(ticker: str, dividend_dict: dict) -> tuple[bool, dict, List[str]]:
    """
    Validate a dividend event using Groq.
    Returns (is_valid, cleaned_data, warnings).
    """
    try:
        data_str = json.dumps(dividend_dict)
        prompt = build_dividend_validation_prompt(ticker, data_str)
        response_str = call_groq_validation(prompt)
        result = json.loads(response_str)
        
        is_valid = result.get("valid", False)
        warnings = result.get("warnings", [])
        
        if warnings:
            logger.warning("Dividend validation warnings for %s: %s", ticker, "; ".join(warnings))
        
        cleaned = {
            "ticker": result.get("ticker", ticker),
            "amount": result.get("amount"),
            "ex_date": result.get("ex_date"),
        }
        
        return is_valid, cleaned, warnings
    except Exception as exc:
        logger.error("Groq dividend validation failed for %s: %s", ticker, exc)
        return False, {}, [str(exc)]