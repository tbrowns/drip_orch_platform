# DRIP Orchestrator

A comprehensive platform for simulating and tracking Dividend Reinvestment Plans (DRIP) on the Nairobi Stock Exchange (NSE). DRIP Orchestrator combines real-time stock data scraping, portfolio management, and financial calculations to help investors understand and optimize their dividend reinvestment strategies.

## Features

- **Real-time Stock Scraping**: Automatically fetches live stock quotes from the NSE via RapidAPI
- **Dividend Tracking**: Monitors dividend announcements and tracks dividend history
- **DRIP Simulation**: Calculates how dividends can be reinvested to purchase additional shares
- **User Authentication**: Secure JWT-based authentication system with password hashing
- **Portfolio Management**: Track multiple stock holdings and compute reinvestment scenarios
- **KYC Verification**: Built-in user verification and compliance features
- **REST API**: FastAPI-powered endpoints for all operations
- **Multi-Database Support**: Works with SQLite, PostgreSQL, and MySQL

## Project Structure

```
drip-orch/
├── main.py                 # FastAPI application & authentication endpoints
├── main.py                 # Entry into the backend application
├── rapid_stock_quote.py    # StockQuoteScraper class for NSE data fetching
├── requirements.txt        # Python dependencies
├── core/
│   └── security.py         # JWT tokens, password hashing, oauth2 schemes
├── db/
│   └── models.py          # SQLAlchemy ORM models (User, Portfolio, StockQuote, etc.)
└── scraper/
    ├── drip.py            # DRIP calculation logic
    ├── groq_client.py     # Groq AI integration
    └── mystocks.py         # Web scraping utilities
```

## Installation

### Prerequisites

- Python 3.9+
- PostgreSQL/MySQL/SQLite (configurable)
- API Keys:
  - RapidAPI key for NSE data
  - Groq API key (for AI features)

### Setup

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd drip-orch
   ```

2. **Create virtual environment**
   ```bash
   python -m venv .venv
   
   # Windows
   .\.venv\Scripts\activate
   
   # macOS/Linux
   source .venv/bin/activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**
   
   Create a `.env` file in the project root:
   ```env
   DATABASE_URL=sqlite:///mystocks_drip.db
   # Or for PostgreSQL:
   # DATABASE_URL=postgresql://user:password@localhost:5432/drip_db
   
   RAPID_API_KEY=your_rapidapi_key_here
   GROQ_API_KEY=your_groq_api_key_here
   SECRET_KEY=your_secret_key_for_jwt
   ```

## Usage

### Running the FastAPI Server

```bash
# Development server
uvicorn main:app --reload

# Production server
uvicorn main:app --host 0.0.0.0 --port 8000
```

The API will be available at `http://localhost:8000`
- API docs: `http://localhost:8000/docs`
- Alternative docs: `http://localhost:8000/redoc`

### User Authentication

**Sign Up**
```bash
POST /auth/signup
Content-Type: application/json

{
  "full_name": "John Doe",
  "email": "john@example.com",
  "password": "secure_password",
  "id_number": "123456789"
}
```

**Login**
```bash
POST /auth/login
Content-Type: application/json

{
  "email": "john@example.com",
  "password": "secure_password"
}
```

### Fetching Stock Data

Using the `StockQuoteScraper` class:

```python
from test import StockQuoteScraper
from db.models import init_db
import os
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("RAPID_API_KEY")
_, session_factory = init_db(os.getenv("DATABASE_URL"))

scraper = StockQuoteScraper(
    api_key=api_key,
    session_factory=session_factory
)

# Fetch and save quotes
quotes = scraper.run()
print(quotes)
```

### DRIP Calculation

```python
from scraper.drip import compute_drip
from scraper.mystocks import QuoteData, DividendData

# Compute DRIP for a stock
result = compute_drip(
    quote=quote_data,
    shares_held=100.0,
    dividends=dividend_history,
    portfolio_override_dividend=None
)

print(f"Shares to buy: {result.reinvest_shares}")
print(f"Leftover cash: {result.leftover_cash} KES")
```

## Database Models

### Core Models

- **User**: User accounts with authentication credentials
- **UserKYC**: Know-Your-Customer (KYC) information for compliance
- **UserPortfolio**: User's stock holdings and portfolio data
- **StockQuote**: Real-time stock prices and metrics
- **DividendEvent**: Dividend announcement and distribution data
- **DRIPSummary**: Calculated DRIP results for user portfolios
- **PaymentMethod**: Payment details for transactions
- **CDSAccount**: Central Depository System account information

## Core Modules

### Security (`core/security.py`)

Handles authentication and data protection:
- JWT token creation and verification
- Password hashing with bcrypt
- OAuth2 scheme configuration

### Scraper (`scraper/`)

- **drip.py**: Core DRIP calculation engine
- **mystocks.py**: Data structures for stock/dividend info
- **groq_client.py**: Integration with Groq AI for insights
- **scraper.py**: Web scraping utilities

### Database (`db/models.py`)

SQLAlchemy ORM models with support for:
- Multiple database backends
- Relationship mapping
- Timezone-aware timestamps
- Cascading deletes

## API Endpoints

### Authentication
- `POST /auth/signup` - Create new user account
- `POST /auth/login` - Authenticate and receive JWT token

### User Profile
- `GET /user/profile` - Get current user info (requires auth)
- `PUT /user/profile` - Update user information

### Portfolio
- `GET /portfolio` - List user portfolios
- `POST /portfolio` - Create new portfolio
- `GET /portfolio/{id}/drip` - Calculate DRIP for portfolio

### Stocks
- `GET /stocks` - List all tracked stocks
- `GET /stocks/{ticker}` - Get stock details
- `GET /stocks/{ticker}/dividends` - Dividend history

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `DATABASE_URL` | Database connection string | Yes |
| `RAPID_API_KEY` | RapidAPI key for NSE data | Yes |
| `GROQ_API_KEY` | Groq API key for AI features | No |
| `SECRET_KEY` | Secret key for JWT signing | Yes |

## Development

### Running Tests

```bash
pytest test.py
```

### Code Structure

- Follow PEP 8 style guidelines
- Use type hints for function signatures
- Add docstrings to classes and functions
- Keep database queries in models, business logic in separate modules

### Logging

The application uses Python's built-in logging configured in `test.py` and `main.py`:

```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scraper.log", encoding="utf-8"),
    ]
)
```

## Dependencies

### Core
- **fastapi**: Web framework
- **uvicorn**: ASGI server
- **sqlalchemy**: ORM
- **pydantic**: Data validation

### Data Processing
- **beautifulsoup4**: Web scraping
- **requests**: HTTP client
- **schedule**: Task scheduling

### Authentication & Security
- **python-jose**: JWT tokens
- **passlib**: Password hashing
- **bcrypt**: Cryptographic hashing

### Database Drivers
- **asyncpg**: PostgreSQL async driver
- **psycopg2-binary**: PostgreSQL sync driver

### AI/ML
- **groq**: Groq API client

See `requirements.txt` for complete list with versions.

## Troubleshooting

### Database Connection Issues

**Problem**: Connection to PostgreSQL/MySQL fails
- Ensure database server is running
- Verify `DATABASE_URL` in `.env`
- Check firewall/network settings

### API Key Issues

**Problem**: "Unauthorized" errors from RapidAPI
- Verify `RAPID_API_KEY` is set correctly
- Check API key has access to NSE endpoint
- Ensure API quota hasn't been exceeded

### Stock Data Not Updating

**Problem**: Quotes not refreshing
- Check if scraper is scheduled correctly
- Verify API connectivity
- Check logs in `scraper.log`

## Future Enhancements

- [ ] Automated scheduled scraping
- [ ] Advanced portfolio analytics dashboard
- [ ] Tax calculation integration
- [ ] Multi-currency support
- [ ] Mobile app
- [ ] WebSocket real-time updates
- [ ] Historical analysis and backtesting

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit changes (`git commit -m 'Add amazing feature'`)
4. Push to branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License - see LICENSE file for details.

## Support

For issues, questions, or suggestions:
- Open an GitHub issue
- Contact the development team

## Acknowledgments

- Nairobi Stock Exchange (NSE) for data APIs
- RapidAPI for API marketplace integration
- Groq for AI capabilities