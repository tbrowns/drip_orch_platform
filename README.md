# DRIP Orchestrator

A FastAPI backend that models what reinvested dividends compound into on the
Nairobi Securities Exchange (NSE). It scrapes NSE quotes and the corporate
dividend calendar from live.mystocks.co.ke on a schedule, stores them, and runs
a dividend-reinvestment (DRIP) engine over them that is honest about the
frictions a Kenyan investor actually faces: withholding tax, brokerage and
statutory levies, whole shares only, and cash left over between payments.

Kenya has no broker-level DRIP, so "reinvesting" means receiving the net cash
and placing an ordinary buy order. That is exactly what the engine simulates.

## Features

- **Scheduled NSE scraping** — `NSEDatabaseScraper` pulls every ticker's quote
  page and the dividend calendar from mystocks every 10 minutes and upserts
  them into `stock_quotes` and `dividend_announcements`.
- **DRIP engine (`scraper/drip.py`)** — pure, dependency-free math:
  withholding tax, brokerage + levies with a minimum commission, whole-share
  flooring, leftover-cash carry-forward, multi-year compounding with one, two
  or four payments a year, optional price/dividend growth, and a comparison
  against simply keeping the dividends as cash.
- **DRIP API** — `POST /drip/simulate` for any ticker or explicit numbers, and
  `GET /drip/portfolio` over the user's stored holdings. Both require a JWT.
- **JWT authentication** with bcrypt password hashing.
- **User, KYC, payment-method, CDS-account and portfolio tables** (SQLAlchemy).

## Project structure

```
drip-orch/
├── main.py                 # FastAPI app: auth, quotes, dividends, DRIP endpoints, scheduler
├── nse_scraper.py          # NSEScraper / NSEDatabaseScraper (live.mystocks.co.ke)
├── rapid_stock_quote.py    # Legacy RapidAPI quote fetcher (references models that no longer exist; unused)
├── requirements.txt
├── pyproject.toml
├── core/
│   ├── security.py         # JWT create/verify, password hashing, OAuth2 bearer scheme
│   └── drip_schemas.py     # Pydantic request/response models + request -> engine-input resolution
├── db/
│   └── models.py           # SQLAlchemy models: User, UserKYC, PaymentMethod, CDSAccount,
│                           #   UserPortfolio, PortfolioHolding, StockQuote, Announcement, DRIPSummary
├── scraper/
│   ├── drip.py             # The DRIP engine (pure functions + dataclasses)
│   ├── models.py           # QuoteData / DividendData dataclasses + adapters from DB rows
│   └── groq_client.py      # Groq-based data validation helpers (optional, unused by the API)
├── tests/
│   └── test_drip.py        # Engine, adapter and schema tests (no DB needed)
└── misc/
    └── app.py              # Legacy CLI scraper (references a removed scraper.mystocks module; unused)
```

## Setup

Requires Python 3.11+ (the code uses `datetime.UTC`).

```bash
git clone <repository-url>
cd drip-orch
python -m venv .venv
# Windows: .\.venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` in the project root:

```env
DATABASE_URL=postgresql://user:password@host:5432/drip_db   # any SQLAlchemy URL
SECRET_KEY=change-me                                         # JWT signing key (required)
ALGORITHM=HS256                                              # optional, default HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60                               # optional, default 60
GROQ_API_KEY=...                                             # optional, only for scraper/groq_client.py
RAPID_API_KEY=...                                            # optional, legacy
SKIP_SCRAPE=false                                            # optional, default false
SCRAPE_INTERVAL_MINUTES=60                                   # optional, default 60
```

`main.py` connects to `DATABASE_URL` and creates the tables at import time,
and starts the scraper scheduler on startup.

```bash
uvicorn main:app --reload        # http://localhost:8000/docs
```

### Running without scraping

By default, starting the app immediately scrapes `live.mystocks.co.ke` and then
repeats every `SCRAPE_INTERVAL_MINUTES`. That is what you want in production and
rarely what you want anywhere else: it blocks startup on a third party being
reachable, hits their site on every local run and every test, and rewrites
`stock_quotes` underneath whatever you were about to measure.

```bash
SKIP_SCRAPE=true uvicorn main:app --reload
```

No startup scrape, no scheduler thread; the API serves whatever is already in
the database. Accepts `1`, `true`, `yes` or `on` in any case. Leave it unset in
production.

To refresh the data deliberately instead, run one scrape and exit:

```python
from main import session_factory
from nse_scraper import NSEDatabaseScraper
NSEDatabaseScraper(session_factory=session_factory).run_once()
```

## The DRIP engine

`scraper/drip.py` is pure: no database, no I/O. Every parameter is explicit in
`DripAssumptions`, with Kenyan defaults:

| Assumption | Default | Meaning |
|---|---|---|
| `withholding_tax_rate` | `0.05` | Final withholding tax on dividends for a resident individual. Use `0.15` for non-residents (`DripAssumptions.non_resident()`). |
| `brokerage_rate` | `0.0212` | All-in purchase cost as a fraction of the consideration. **Approximate** — see breakdown below; override with your broker's schedule. |
| `min_brokerage_kes` | `100` | Minimum commission per trade. Set `0` to disable. |
| `allow_fractional_shares` | `false` | The NSE trades whole shares only; `true` is for what-ifs. |
| `reinvest_leftover` | `true` | Carry cash that could not buy a whole share into the next payment. `false` treats it as paid out. |

The 2.12 % default is built from the CMA-capped broker commission for small
orders plus the statutory levies, and is deliberately a little conservative:

```
broker commission (orders up to KES 100,000) ... 1.50 %
16 % VAT on that commission ..................... 0.24 %
NSE transaction levy ............................ 0.12 %
CMA transaction levy ............................ 0.12 %
CDSC transaction levy ........................... 0.08 %
Investor Compensation Fund levy ................. 0.01 %
                                                  -------
                                                ≈ 2.07 %  -> rounded up to 2.12 %
```

Brokers differ, larger orders are cheaper and levies get revised, so treat the
rate as a default to override, not a quote.

### One reinvestment, worked through

1,000 shares of a KES 20.00 stock paying KES 1.50, resident investor, no
minimum commission (`min_brokerage_kes = 0`):

```
gross dividend   1,000 x 1.50                 = 1,500.00
withholding tax  1,500 x 5 %                  =    75.00
net dividend     1,500 - 75                   = 1,425.00
affordable       1,425 / (20 x 1.0212)        =    69.77  -> 69 whole shares
cost             69 x 20                      = 1,380.00
fee              1,380 x 2.12 %               =    29.26
leftover         1,425 - 1,380 - 29.26        =    15.74  -> carried to the next payment
```

With the default KES 100 minimum the same dividend buys only 66 shares
(`(1,425 - 100) / 20 = 66.25`), pays a KES 100 fee and carries KES 5.00. Small
reinvestments are expensive on the NSE; the engine shows that instead of
hiding it.

### Projection

`project_drip(initial_shares, price, dividend_per_share_annual, payments_per_year, years, assumptions, price_growth_rate, dividend_growth_rate)`
runs that step for every payment, splitting the annual dividend evenly across
1, 2 or 4 payments and growing price and dividend once per year. Shares bought
in one period earn dividends in the next. The result is a row per period plus
totals, including `vs_no_reinvest_value`: the same initial shares with every
net dividend kept as cash. `reinvestment_gain` is the difference.

With a flat price the gain is negative at first (fees are pure drag) and turns
positive once the dividends earned by the reinvested shares outweigh them —
for the example above it is about -133 KES after 2 years and about +4,158 KES
after 10 (1,920 shares vs 1,000). That is the honest shape of a DRIP.

```python
from scraper.drip import DripAssumptions, project_drip

p = project_drip(1000, 20.0, 1.50, payments_per_year=1, years=10)
print(p.totals.shares_end, round(p.totals.ending_value, 2), round(p.totals.vs_no_reinvest_value, 2))
# 1920.0 38408.5 34250.0
```

`compute_drip` / `compute_portfolio_drip` remain as thin wrappers that take
`QuoteData` / `DividendData` (from `scraper/models.py`) and return the richer
result.

## API

Interactive docs at `/docs`. All `/drip/*` endpoints need
`Authorization: Bearer <token>` from `/auth/signup` or `/auth/login`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/auth/signup` | – | Create a user, returns a JWT |
| POST | `/auth/login` | – | JSON `{email, password}`, returns a JWT |
| GET | `/users/me` | JWT | Current user with KYC, payment methods, CDS accounts, portfolios |
| GET | `/kyc/me` | JWT | Current user's KYC record |
| GET | `/quotes` | – | Compact list of scraped quotes |
| GET | `/detailed-quotes` | – | Every scraped quote field |
| GET | `/detailed-quotes/{ticker}` | – | One ticker's scraped quote |
| GET | `/dividends/upcoming` | – | Calendar announcements dated today or later |
| POST | `/drip/simulate` | JWT | DRIP projection for a ticker or explicit numbers |
| GET | `/drip/portfolio` | JWT | DRIP projection over the user's stored holdings |

### Get a token

```bash
curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "jane@example.com", "password": "secret"}'
# {"message": "...", "access_token": "eyJ...", "token_type": "bearer"}
export TOKEN=eyJ...
```

### POST /drip/simulate

Body fields: `ticker` *(optional)*, `price` *(optional, KES)*,
`dividend_per_share` *(optional, annual gross KES)*, `shares_held` *(>= 1)*,
`years` *(1–40, default 10)*, `payments_per_year` *(1, 2 or 4; default 1)*,
`price_growth_rate` and `dividend_growth_rate` *(annual, default 0)*, and an
`assumptions` object with any of the keys in the table above. Give a `ticker`
(the latest scraped previous close and the de-duplicated dividend
announcements are used; explicit values win), or give both `price` and
`dividend_per_share`.

```bash
curl -s -X POST http://localhost:8000/drip/simulate \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ticker": "SCOM", "shares_held": 1000, "years": 2}'
```

```bash
# Explicit numbers — the worked example above
curl -s -X POST http://localhost:8000/drip/simulate \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"price": 20, "dividend_per_share": 1.50, "shares_held": 1000, "years": 1,
       "assumptions": {"min_brokerage_kes": 0}}'
```

Response (explicit example, abridged):

```json
{
  "inputs": {
    "ticker": null, "name": null,
    "price": 20.0, "price_source": "request",
    "dividend_per_share_annual": 1.5, "dividend_source": "request",
    "shares_held": 1000.0, "years": 1, "payments_per_year": 1,
    "price_growth_rate": 0.0, "dividend_growth_rate": 0.0,
    "annual_dividend_yield_pct": 7.5, "net_dividend_yield_pct": 7.125,
    "quote_scraped_at": null, "dividend_events_used": []
  },
  "assumptions": {
    "withholding_tax_rate": 0.05, "brokerage_rate": 0.0212, "min_brokerage_kes": 0.0,
    "allow_fractional_shares": false, "reinvest_leftover": true
  },
  "periods": [
    {
      "period": 1, "year": 1, "payment_in_year": 1, "price": 20.0,
      "shares_start": 1000.0, "gross_dividend": 1500.0, "withholding_tax": 75.0,
      "net_dividend": 1425.0, "cash_available": 1425.0, "shares_bought": 69.0,
      "fees": 29.26, "cash_carried": 15.74, "shares_end": 1069.0, "portfolio_value": 21395.74
    }
  ],
  "totals": {
    "initial_shares": 1000.0, "shares_end": 1069.0, "shares_bought": 69.0,
    "total_gross_dividends": 1500.0, "total_tax_paid": 75.0, "total_net_dividends": 1425.0,
    "total_fees_paid": 29.26, "cash_carried_end": 15.74, "cash_paid_out": 0.0,
    "ending_price": 20.0, "ending_value": 21395.74, "vs_no_reinvest_value": 21425.0,
    "reinvestment_gain": -29.26, "reinvestment_gain_pct": -0.14
  }
}
```

When a `ticker` is used, `price_source` is `db:stock_quotes.previous` (or
`.average` / `.open` as fallbacks), `dividend_source` is
`db:dividend_announcements`, and `dividend_events_used` lists the calendar
rows that were summed into the annual dividend (an interim and a final are
added; the announced / book-closure / payment rows of one dividend are
collapsed; only the most recent dividend of each type counts, so last year's
final is never added to this year's). Errors: `404` unknown ticker with no `price`; `422` when the
scraped data lacks a price or a dividend amount and none was supplied, or when
validation fails.

### GET /drip/portfolio

Runs the projection over every holding in the user's first stored portfolio.
Query parameters: `years` (1–40, default 10), `payments_per_year` (1, 2, 4),
`price_growth_rate`, `dividend_growth_rate`, `withholding_tax_rate`,
`brokerage_rate`, `min_brokerage_kes`, `include_periods` (default `true`).

```bash
curl -s "http://localhost:8000/drip/portfolio?years=10&payments_per_year=1&include_periods=false" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "portfolio_id": 1, "portfolio_name": "Main Portfolio", "cash_balance": 0.0,
  "years": 10, "payments_per_year": 1,
  "assumptions": {"withholding_tax_rate": 0.05, "brokerage_rate": 0.0212, "min_brokerage_kes": 100.0,
                  "allow_fractional_shares": false, "reinvest_leftover": true},
  "positions": [
    {"ticker": "SCOM", "name": "Safaricom Plc", "inputs": {"...": "as in /drip/simulate"},
     "totals": {"...": "as in /drip/simulate"}, "periods": []}
  ],
  "skipped": [
    {"ticker": "KQ", "shares_held": 500.0,
     "reason": "No dividend announcement with an amount found for KQ. Pass 'dividend_per_share' (annual, gross, KES) explicitly."}
  ],
  "aggregate": {"positions": 1, "initial_value": 17500.0, "ending_value": 30123.45,
                "vs_no_reinvest_value": 28900.0, "reinvestment_gain": 1223.45,
                "total_net_dividends": 12345.67, "total_tax_paid": 649.77, "total_fees_paid": 1000.0}
}
```

Returns `404` with a clear message when the user has no portfolio or the
portfolio has no holdings. Holdings whose ticker has no usable scraped price
or dividend are reported under `skipped` rather than failing the request.

## Tests

```bash
python -m pytest -q
```

`tests/test_drip.py` covers the engine (tax, brokerage, whole-share flooring,
minimum-commission edge cases, carry-forward, compounding, growth, zero
dividend, invalid inputs), the DB-row adapters and the request schemas. No
database or network is needed.

## Notes

- `Announcement.date` is a real `Date` column; `nse_scraper.parse_announcement_date`
  parses the calendar's `"29 May 2026"` strings and skips rows it cannot parse.
- `misc/app.py` and `rapid_stock_quote.py` are legacy scripts kept for
  reference; they import modules/models that no longer exist and are not used
  by the API.
- The Swagger "Authorize" button posts a form to `/auth/login`, which expects
  JSON; obtain a token with curl (above) and paste it instead.
