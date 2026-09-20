"""
tests/test_drip.py — unit tests for the pure DRIP engine (scraper/drip.py),
the DB-row adapters (scraper/models.py) and the request schemas
(core/drip_schemas.py).

No database and no network: everything here runs on plain Python objects.
"""

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from scraper.drip import (
    DEFAULT_BROKERAGE_RATE,
    DEFAULT_MIN_BROKERAGE_KES,
    NON_RESIDENT_WHT_RATE,
    RESIDENT_WHT_RATE,
    DripAssumptions,
    compute_drip,
    compute_portfolio_drip,
    compute_reinvestment,
    max_affordable_shares,
    project_drip,
)
from scraper.models import (
    DividendData,
    QuoteData,
    dividend_from_row,
    dividends_from_rows,
    estimate_annual_dividend_per_share,
    latest_dividend,
    parse_kes_amount,
    quote_from_row,
)
from core.drip_schemas import (
    DripAssumptionsIn,
    DripSimulateRequest,
    InputResolutionError,
    resolve_market_inputs,
)

NO_MIN = DripAssumptions(min_brokerage_kes=0.0)


# ── Defaults ──────────────────────────────────────────────────────────────────

def test_kenyan_defaults_are_explicit():
    a = DripAssumptions()
    assert a.withholding_tax_rate == 0.05 == RESIDENT_WHT_RATE
    assert a.brokerage_rate == 0.0212 == DEFAULT_BROKERAGE_RATE
    assert a.min_brokerage_kes == 100.0 == DEFAULT_MIN_BROKERAGE_KES
    assert a.allow_fractional_shares is False
    assert a.reinvest_leftover is True
    assert DripAssumptions.non_resident().withholding_tax_rate == 0.15 == NON_RESIDENT_WHT_RATE


@pytest.mark.parametrize("bad", [
    {"withholding_tax_rate": 1.0},
    {"withholding_tax_rate": -0.01},
    {"brokerage_rate": 1.5},
    {"min_brokerage_kes": -1},
])
def test_assumptions_reject_nonsense(bad):
    with pytest.raises(ValueError):
        DripAssumptions(**bad)


# ── The hand-worked example ───────────────────────────────────────────────────

def test_hand_worked_example_1000_shares_kes20_paying_kes1_50():
    """
    1,000 shares of a KES 20.00 stock paying KES 1.50 per share, resident
    investor, 2.12 % all-in brokerage, no minimum commission:

        gross dividend  = 1,000 x 1.50                 = 1,500.00
        withholding tax = 1,500 x 5 %                  =    75.00
        net dividend    = 1,500 - 75                   = 1,425.00
        cash available  = 1,425 + 0 carried            = 1,425.00

        affordable      = 1,425 / (20 x 1.0212)
                        = 1,425 / 20.424               =    69.77...
        whole shares    = floor(69.77)                 =    69        (not 69.77)

        cost            = 69 x 20                      = 1,380.00
        fee             = 1,380 x 2.12 %               =    29.256   (KES 29.26)
        leftover        = 1,425 - 1,380 - 29.256       =    15.744   (KES 15.74)
        shares after    = 1,000 + 69                   = 1,069

    Check that 70 shares would NOT have fit: 70 x 20 x 1.0212 = 1,429.68 > 1,425.

    With the default KES 100 minimum commission the same dividend buys fewer
    shares, because the minimum binds (69 x 20 x 2.12 % = 29.26 < 100):

        n <= (1,425 - 100) / 20 = 66.25 -> 66 shares
        cost = 66 x 20 = 1,320 ; fee = max(1,320 x 2.12 %, 100) = 100
        leftover = 1,425 - 1,320 - 100 = 5.00
    """
    r = compute_reinvestment(
        gross_dividend_per_share=1.50, shares_held=1000, price=20.0,
        cash_carried=0.0, assumptions=NO_MIN,
    )
    assert r.gross_dividend == pytest.approx(1500.00)
    assert r.withholding_tax == pytest.approx(75.00)
    assert r.net_dividend == pytest.approx(1425.00)
    assert r.cash_available == pytest.approx(1425.00)
    assert r.shares_bought == 69
    assert r.purchase_cost == pytest.approx(1380.00)
    assert r.fees == pytest.approx(29.256, abs=1e-6)
    assert round(r.fees, 2) == 29.26
    assert r.leftover_cash == pytest.approx(15.744, abs=1e-6)
    assert round(r.leftover_cash, 2) == 15.74
    assert r.cash_carried == pytest.approx(15.744, abs=1e-6)
    assert r.shares_end == 1069
    # 70 shares would have cost 1,429.68 all-in, more than the 1,425 available.
    assert 70 * 20.0 * 1.0212 > 1425.0

    with_min = compute_reinvestment(1.50, 1000, 20.0)   # default assumptions
    assert with_min.shares_bought == 66
    assert with_min.fees == pytest.approx(100.00)
    assert with_min.leftover_cash == pytest.approx(5.00)


# ── Withholding tax ───────────────────────────────────────────────────────────

def test_withholding_tax_reduces_cash_by_exactly_five_percent():
    r = compute_reinvestment(2.00, 750, 50.0, assumptions=NO_MIN)
    assert r.gross_dividend == pytest.approx(1500.0)
    assert r.withholding_tax == pytest.approx(1500.0 * 0.05)
    assert r.net_dividend == pytest.approx(1500.0 * 0.95)
    assert r.cash_available == pytest.approx(1425.0)


def test_non_resident_withholding_tax_is_fifteen_percent():
    r = compute_reinvestment(2.00, 750, 50.0, assumptions=DripAssumptions.non_resident())
    assert r.withholding_tax == pytest.approx(225.0)
    assert r.net_dividend == pytest.approx(1275.0)


# ── Brokerage ─────────────────────────────────────────────────────────────────

def test_brokerage_reduces_shares_bought():
    free = compute_reinvestment(1.50, 1000, 20.0, assumptions=DripAssumptions(brokerage_rate=0.0, min_brokerage_kes=0.0))
    paid = compute_reinvestment(1.50, 1000, 20.0, assumptions=NO_MIN)
    assert free.shares_bought == 71           # floor(1425 / 20)
    assert free.fees == 0.0
    assert paid.shares_bought == 69           # floor(1425 / 20.424)
    assert paid.fees > 0.0
    assert paid.shares_bought < free.shares_bought


def test_whole_share_flooring_net_1000_at_price_300():
    # 1,000 / (300 x 1.0212) = 3.264... -> 3 shares, never 3.33.
    assert max_affordable_shares(1000.0, 300.0, NO_MIN) == 3
    # The default minimum also lands on 3: (1000 - 100) / 300 = 3.0 exactly.
    assert max_affordable_shares(1000.0, 300.0) == 3
    r = compute_reinvestment(0.0, 0, 300.0, cash_carried=1000.0, assumptions=NO_MIN)
    assert r.shares_bought == 3
    assert r.purchase_cost == pytest.approx(900.0)
    assert r.fees == pytest.approx(900.0 * 0.0212)
    assert r.leftover_cash == pytest.approx(1000.0 - 900.0 - 19.08)


def test_fractional_shares_only_when_explicitly_allowed():
    frac = DripAssumptions(allow_fractional_shares=True, min_brokerage_kes=0.0)
    assert max_affordable_shares(1000.0, 300.0, frac) == pytest.approx(1000.0 / (300.0 * 1.0212))


def test_exactly_affordable_quantity_is_not_lost_to_float_noise():
    zero_fee = DripAssumptions(brokerage_rate=0.0, min_brokerage_kes=0.0)
    assert max_affordable_shares(3000.0, 300.0, zero_fee) == 10
    assert max_affordable_shares(0.3 * 3, 0.3, zero_fee) == 3


# ── Minimum commission ────────────────────────────────────────────────────────

def test_min_brokerage_makes_tiny_purchase_buy_zero_and_carry_cash():
    # 10 shares x 1.50 = 15 gross -> 14.25 net. Cannot even cover the KES 100 minimum.
    r = compute_reinvestment(1.50, 10, 20.0)
    assert r.shares_bought == 0
    assert r.fees == 0.0
    assert r.purchase_cost == 0.0
    assert r.cash_carried == pytest.approx(14.25)
    assert r.shares_end == 10

    # 120 KES could buy two KES-50 shares before fees, but 100 + 100 > 120: buy nothing.
    r2 = compute_reinvestment(0.0, 0, 50.0, cash_carried=120.0)
    assert r2.shares_bought == 0
    assert r2.cash_carried == pytest.approx(120.0)

    # Once the minimum can be covered, buy: 150 -> (150 - 100) / 50 = 1 share, fee 100.
    r3 = compute_reinvestment(0.0, 0, 50.0, cash_carried=150.0)
    assert r3.shares_bought == 1
    assert r3.fees == pytest.approx(100.0)
    assert r3.leftover_cash == pytest.approx(0.0)


# ── Carry-forward across periods ──────────────────────────────────────────────

def test_leftover_cash_carries_forward_and_eventually_buys_a_share():
    # 100 shares, KES 100 price, KES 1.00 annual dividend -> 95 net per year.
    # Year 1: 95 < 102.12 -> 0 shares, carry 95.
    # Year 2: 95 + 95 = 190 -> 1 share (102.12), carry 87.88.
    # Year 3: 87.88 + 101 x 0.95 = 183.83 -> 1 share, carry 81.71.
    p = project_drip(100, 100.0, 1.0, payments_per_year=1, years=3, assumptions=NO_MIN)
    y1, y2, y3 = p.periods
    assert y1.shares_bought == 0
    assert y1.cash_carried == pytest.approx(95.0)
    assert y2.cash_available == pytest.approx(190.0)
    assert y2.shares_bought == 1
    assert y2.cash_carried == pytest.approx(190.0 - 102.12)
    assert y3.shares_start == 101
    assert y3.cash_available == pytest.approx(87.88 + 101 * 0.95)
    assert y3.shares_bought == 1
    assert p.totals.shares_end == 102
    assert p.totals.cash_carried_end == pytest.approx(y3.cash_carried)


def test_reinvest_leftover_false_pays_cash_out_instead_of_carrying():
    a = DripAssumptions(min_brokerage_kes=0.0, reinvest_leftover=False)
    p = project_drip(100, 100.0, 1.0, years=2, assumptions=a)
    assert p.periods[0].cash_carried == 0.0
    assert p.periods[1].cash_available == pytest.approx(95.0)   # nothing carried in
    assert p.totals.cash_paid_out == pytest.approx(190.0)
    assert p.totals.ending_value == pytest.approx(100 * 100.0 + 190.0)


# ── Compounding projection ────────────────────────────────────────────────────

def test_ten_year_projection_grows_shares_and_beats_holding_cash():
    p = project_drip(1000, 20.0, 1.50, payments_per_year=1, years=10)
    t = p.totals
    assert len(p.periods) == 10
    assert t.shares_end > 1000
    assert t.shares_end == p.periods[-1].shares_end
    assert t.shares_end == int(t.shares_end)                    # whole shares throughout
    assert t.vs_no_reinvest_value < t.ending_value
    assert t.reinvestment_gain == pytest.approx(t.ending_value - t.vs_no_reinvest_value)
    # Baseline: 1,000 shares still worth 20,000 plus 10 x 1,425 net dividends kept as cash.
    assert t.vs_no_reinvest_value == pytest.approx(20000.0 + 10 * 1425.0)
    assert t.total_tax_paid == pytest.approx(t.total_gross_dividends * 0.05)
    assert t.total_net_dividends == pytest.approx(t.total_gross_dividends - t.total_tax_paid)
    assert t.total_fees_paid > 0
    # Dividends on reinvested shares mean the DRIP path collects more gross than the baseline.
    assert t.total_gross_dividends > 10 * 1500.0
    # Every period's shares_start equals the previous period's shares_end.
    for prev, cur in zip(p.periods, p.periods[1:]):
        assert cur.shares_start == prev.shares_end
        assert cur.year == prev.year + 1


def test_two_payments_per_year_split_the_annual_dividend():
    p = project_drip(1000, 20.0, 1.50, payments_per_year=2, years=1, assumptions=NO_MIN)
    assert len(p.periods) == 2
    assert [x.payment_in_year for x in p.periods] == [1, 2]
    assert p.periods[0].gross_dividend == pytest.approx(750.0)
    assert p.periods[0].year == p.periods[1].year == 1


def test_growth_rates_apply_per_year():
    p = project_drip(1000, 20.0, 1.00, years=3, price_growth_rate=0.10, dividend_growth_rate=0.05, assumptions=NO_MIN)
    assert [round(x.price, 4) for x in p.periods] == [20.0, 22.0, 24.2]
    # Year-2 gross dividend uses the grown DPS (1.05) on the year-2 opening shares.
    assert p.periods[1].gross_dividend == pytest.approx(p.periods[1].shares_start * 1.05)
    assert p.totals.ending_price == pytest.approx(24.2)


def test_zero_dividend_means_no_purchases_and_no_crash():
    p = project_drip(500, 10.0, 0.0, years=5)
    assert all(x.shares_bought == 0 for x in p.periods)
    assert p.totals.shares_end == 500
    assert p.totals.total_net_dividends == 0.0
    assert p.totals.total_fees_paid == 0.0
    assert p.totals.ending_value == pytest.approx(5000.0)
    assert p.totals.vs_no_reinvest_value == pytest.approx(5000.0)
    r = compute_reinvestment(0.0, 500, 10.0)
    assert r.shares_bought == 0 and r.fees == 0.0


@pytest.mark.parametrize("price", [0.0, -5.0])
def test_non_positive_price_raises(price):
    with pytest.raises(ValueError):
        compute_reinvestment(1.0, 100, price)
    with pytest.raises(ValueError):
        project_drip(100, price, 1.0)
    with pytest.raises(ValueError):
        max_affordable_shares(1000.0, price)


def test_other_invalid_inputs_raise():
    with pytest.raises(ValueError):
        compute_reinvestment(-1.0, 100, 10.0)
    with pytest.raises(ValueError):
        compute_reinvestment(1.0, -1, 10.0)
    with pytest.raises(ValueError):
        project_drip(0, 10.0, 1.0)
    with pytest.raises(ValueError):
        project_drip(100, 10.0, 1.0, years=0)
    with pytest.raises(ValueError):
        project_drip(100, 10.0, 1.0, payments_per_year=0)


def test_projection_to_dict_rounds_money_to_two_places():
    d = project_drip(1000, 20.0, 1.50, years=1, assumptions=NO_MIN).to_dict()
    assert d["periods"][0]["fees"] == 29.26
    assert d["periods"][0]["cash_carried"] == 15.74
    assert d["totals"]["shares_end"] == 1069
    assert d["assumptions"]["withholding_tax_rate"] == 0.05


# ── Adapters from DB rows ─────────────────────────────────────────────────────

class _Row:
    """Stand-in for a SQLAlchemy row: any attributes, no session needed."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.mark.parametrize("raw,expected", [
    ("1.20", 1.20), ("KES 0.35", 0.35), ("2,000.00", 2000.0), (1.5, 1.5),
    ("", None), (None, None), ("n/a", None), ("-3", None),
])
def test_parse_kes_amount(raw, expected):
    assert parse_kes_amount(raw) == expected


def test_quote_from_row_prefers_previous_close_then_average_then_open():
    scraped = datetime(2026, 9, 19, 15, 0)
    q = quote_from_row(_Row(ticker="scom", name="Safaricom Plc", sector="Telecommunication",
                            previous=17.5, average=17.8, open=17.6, scraped_at=scraped))
    assert q.ticker == "SCOM" and q.price == 17.5 and q.price_source == "previous"
    assert q.name == "Safaricom Plc" and q.scraped_at == scraped
    assert quote_from_row(_Row(ticker="X", previous=None, average=12.0, open=11.0)).price_source == "average"
    assert quote_from_row(_Row(ticker="X", previous=0, average=None, open=11.0)).price_source == "open"
    empty = quote_from_row(_Row(ticker="X", previous=None, average=None, open=None))
    assert empty.price is None and empty.price_source is None


def test_dividend_rows_dedupe_calendar_events_into_annual_dividend():
    rows = [
        _Row(ticker="SCOM", date=date(2026, 3, 20), event_type="Announced", amount_kes="0.55", dividend_type="interim dividend", company="Safaricom", description="Announced interim dividend of KES 0.55"),
        _Row(ticker="SCOM", date=date(2026, 4, 10), event_type="Book closure", amount_kes="0.55", dividend_type="interim dividend", company="Safaricom", description="Book closure interim dividend of KES 0.55"),
        _Row(ticker="SCOM", date=date(2026, 8, 15), event_type="Announced", amount_kes="0.65", dividend_type="final dividend", company="Safaricom", description="Announced final dividend of KES 0.65"),
        _Row(ticker="SCOM", date=date(2026, 9, 1), event_type="Payment", amount_kes="0.65", dividend_type="final dividend", company="Safaricom", description="Payment of final dividend of KES 0.65"),
        _Row(ticker="SCOM", date=date(2026, 9, 5), event_type="Other", amount_kes=None, dividend_type=None, company="Safaricom", description="AGM"),
    ]
    events = dividends_from_rows(rows)
    assert len(events) == 4                       # the AGM row has no amount
    assert events[0].date == date(2026, 9, 1)     # newest first
    assert isinstance(events[0], DividendData)

    latest = latest_dividend(events, as_of=date(2026, 9, 20))
    assert latest.amount == 0.65 and latest.event_type == "Payment"

    annual, used = estimate_annual_dividend_per_share(events, as_of=date(2026, 9, 20))
    assert annual == pytest.approx(1.20)          # 0.55 interim + 0.65 final, duplicates collapsed
    assert len(used) == 2

    single = dividend_from_row(rows[0])
    assert single.amount == 0.55 and single.date == date(2026, 3, 20)


def test_latest_dividend_falls_back_to_nearest_future_event():
    events = [
        DividendData("KCB", 3.0, date(2026, 11, 1), "Payment", "final dividend"),
        DividendData("KCB", 3.0, date(2026, 10, 1), "Book closure", "final dividend"),
    ]
    assert latest_dividend(events, as_of=date(2026, 9, 20)).date == date(2026, 10, 1)
    assert latest_dividend([], as_of=date(2026, 9, 20)) is None
    assert estimate_annual_dividend_per_share([]) == (None, [])


def test_annual_dividend_does_not_add_last_years_final_to_this_years():
    """
    Payment dates recur at about the same time every year, so a 365-day window
    ending on this year's final payment (2026-08-28) also contains last year's
    (2025-08-29). When the dividend grew (0.60 -> 0.65) the two finals carry
    different amounts and an (amount, type) de-dup would add both, reporting
    1.80 a year. The annual dividend is one interim plus one final:
    0.55 + 0.65 = 1.20.
    """
    events = [
        DividendData("SCOM", 0.60, date(2025, 5, 10), "Announced", "final dividend"),
        DividendData("SCOM", 0.60, date(2025, 7, 31), "Book closure", "final dividend"),
        DividendData("SCOM", 0.60, date(2025, 8, 29), "Payment", "final dividend"),
        DividendData("SCOM", 0.55, date(2025, 11, 10), "Announced", "interim dividend"),
        DividendData("SCOM", 0.55, date(2025, 12, 31), "Payment", "interim dividend"),
        DividendData("SCOM", 0.65, date(2026, 5, 8), "Announced", "final dividend"),
        DividendData("SCOM", 0.65, date(2026, 7, 31), "Book closure", "final dividend"),
        DividendData("SCOM", 0.65, date(2026, 8, 28), "Payment", "final dividend"),
    ]
    annual, used = estimate_annual_dividend_per_share(events, as_of=date(2026, 9, 20))
    assert annual == pytest.approx(1.20)
    assert [(e.dividend_type, e.amount) for e in used] == [("final dividend", 0.65), ("interim dividend", 0.55)]

    # Anchored on the announcement (payment not yet on the calendar) the window
    # reaches even further into the prior year; still one final + one interim.
    announced_only = [e for e in events if e.date <= date(2026, 6, 1)]
    annual2, used2 = estimate_annual_dividend_per_share(announced_only, as_of=date(2026, 6, 1))
    assert annual2 == pytest.approx(1.20)
    assert used2[0].event_type == "Announced" and used2[0].amount == 0.65

    # Rows without a dividend_type fall back to de-duplication by amount, and an
    # untyped row is not added on top of a typed row carrying the same amount.
    untyped = [
        DividendData("KCB", 3.0, date(2026, 8, 1), "Payment", None),
        DividendData("KCB", 3.0, date(2026, 7, 1), "Book closure", None),
        DividendData("KCB", 1.5, date(2026, 2, 1), "Payment", None),
        DividendData("KCB", 1.5, date(2026, 1, 5), "Announced", "interim dividend"),
    ]
    annual3, used3 = estimate_annual_dividend_per_share(untyped, as_of=date(2026, 9, 20))
    assert annual3 == pytest.approx(4.5)
    assert len(used3) == 2


# ── Compatibility wrappers ────────────────────────────────────────────────────

def test_compute_drip_wrapper_uses_engine_and_announcements():
    quote = QuoteData(ticker="SCOM", price=20.0, name="Safaricom Plc")
    dividends = [DividendData("SCOM", 1.50, date(2026, 8, 1), "Payment", "final dividend")]
    result = compute_drip(quote, 1000, dividends, assumptions=NO_MIN)
    assert result is not None
    assert result.last_dividend == 1.50
    assert result.total_dividend == pytest.approx(1500.0)
    assert result.withholding_tax == pytest.approx(75.0)
    assert result.net_dividend == pytest.approx(1425.0)
    assert result.reinvest_shares == 69
    assert result.fees == pytest.approx(29.26)
    assert result.leftover_cash == pytest.approx(15.74)
    assert result.annual_yield_pct == 7.5
    assert result.projection.totals.shares_end == 1069
    assert "69 shares" in result.note

    assert compute_drip(QuoteData("X", price=None), 100, dividends) is None
    assert compute_drip(quote, 100, []) is None
    assert compute_drip(quote, 100, [], portfolio_override_dividend=2.0).last_dividend == 2.0


def test_compute_portfolio_drip_skips_unknown_tickers():
    quotes = [QuoteData("SCOM", 20.0), QuoteData("EQTY", 45.0)]
    dividends = [
        DividendData("SCOM", 1.50, date(2026, 8, 1), "Payment", "final dividend"),
        DividendData("EQTY", 4.25, date(2026, 6, 1), "Payment", "final dividend"),
    ]
    results = compute_portfolio_drip(quotes, dividends, {"SCOM": 1000, "EQTY": 200, "NOPE": 50})
    assert [r.ticker for r in results] == ["SCOM", "EQTY"]


# ── Request schema validation ─────────────────────────────────────────────────

def test_simulate_request_validation_rules():
    ok = DripSimulateRequest(ticker=" scom ", shares_held=1000)
    assert ok.ticker == "SCOM" and ok.years == 10 and ok.payments_per_year == 1
    assert ok.assumptions.to_engine() == DripAssumptions()

    with pytest.raises(ValidationError):
        DripSimulateRequest(shares_held=1000)                          # no ticker, no explicit values
    with pytest.raises(ValidationError):
        DripSimulateRequest(price=20.0, shares_held=1000)              # explicit needs dividend too
    with pytest.raises(ValidationError):
        DripSimulateRequest(ticker="SCOM", shares_held=0)              # shares_held >= 1
    with pytest.raises(ValidationError):
        DripSimulateRequest(ticker="SCOM", shares_held=10, years=0)    # years 1..40
    with pytest.raises(ValidationError):
        DripSimulateRequest(ticker="SCOM", shares_held=10, years=41)
    with pytest.raises(ValidationError):
        DripSimulateRequest(ticker="SCOM", shares_held=10, payments_per_year=3)   # only 1, 2, 4
    with pytest.raises(ValidationError):
        DripSimulateRequest(ticker="SCOM", shares_held=10, assumptions={"withholding_tax_rate": 1.2})
    explicit = DripSimulateRequest(price=20.0, dividend_per_share=1.5, shares_held=1, payments_per_year=4)
    assert explicit.ticker is None and explicit.payments_per_year == 4


def test_resolve_market_inputs_prefers_request_then_db():
    quote = QuoteData("SCOM", 17.5, name="Safaricom Plc", price_source="previous", scraped_at=datetime(2026, 9, 19, 15, 0))
    dividends = [DividendData("SCOM", 1.20, date(2026, 9, 1), "Payment", "final dividend")]

    db = resolve_market_inputs("SCOM", None, None, quote, dividends)
    assert db.price == 17.5 and db.price_source == "db:stock_quotes.previous"
    assert db.dividend_per_share_annual == 1.20 and db.dividend_source == "db:dividend_announcements"
    assert db.name == "Safaricom Plc" and db.quote_scraped_at == "2026-09-19T15:00:00"
    assert len(db.dividend_events_used) == 1

    req = resolve_market_inputs("SCOM", 18.0, 1.50, quote, dividends)
    assert req.price == 18.0 and req.price_source == "request"
    assert req.dividend_per_share_annual == 1.50 and req.dividend_source == "request"
    assert req.dividend_events_used == []

    with pytest.raises(InputResolutionError) as missing_quote:
        resolve_market_inputs("NOPE", None, None, None, [])
    assert missing_quote.value.status_code == 404

    with pytest.raises(InputResolutionError) as no_price:
        resolve_market_inputs("SCOM", None, 1.0, QuoteData("SCOM", None), [])
    assert no_price.value.status_code == 422

    with pytest.raises(InputResolutionError) as no_dividend:
        resolve_market_inputs("SCOM", None, None, quote, [])
    assert no_dividend.value.status_code == 422
    assert "dividend_per_share" in no_dividend.value.detail


def test_assumptions_in_defaults_match_engine():
    assert DripAssumptionsIn().to_engine() == DripAssumptions()
    assert DripAssumptionsIn(withholding_tax_rate=0.15).to_engine().withholding_tax_rate == 0.15
