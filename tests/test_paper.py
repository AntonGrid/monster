"""The accountant must lose money on a round trip that touches nothing."""
from __future__ import annotations

import pytest

from monster.contracts import FundingQuote, Intent, Leg, Quote, Snapshot
from monster.exec.paper import PaperVenue, Unpriced

#: A realistic epoch: ``ts=0`` is "unset" in the contracts, and the paper venue
#: should be exercised with timestamps it will actually see.
BASE = 1_700_000_000.0


def _snapshot(offset: float, *, spot: float = 100.0, perp: float = 100.0, rate: float = 0.0001) -> Snapshot:
    stamp = BASE + float(offset)
    return Snapshot(
        ts=stamp,
        quotes=(
            Quote(venue="htx", symbol="btcusdt", bid=spot - 0.1, ask=spot + 0.1, ts=stamp),
            Quote(venue="htx", symbol="btc-usdt-swap", bid=perp - 0.1, ask=perp + 0.1, ts=stamp),
        ),
        funding=(FundingQuote(venue="htx", symbol="btc-usdt-swap", rate=rate, period_hours=8.0, ts=stamp),),
    )


def _carry(notional: float = 100.0, edge: float = 12.0) -> Intent:
    return Intent(
        strategy="carry",
        legs=(
            Leg(venue="htx", symbol="btcusdt", side="buy", kind="spot", notional_usd=notional),
            Leg(venue="htx", symbol="btc-usdt-swap", side="sell", kind="perp", notional_usd=notional),
        ),
        expected_edge_bps=edge,
        horizon_hours=8.0,
        rationale="funding 0.0001 per 8h",
    )


def test_a_carry_opens_both_legs_and_pays_both_fees():
    venue = PaperVenue(cash_usd=1_000.0, fee_bps=10.0, slippage_bps=2.0, leverage=2.0, ts=BASE)
    result = venue.execute(_carry(), _snapshot(0.0))
    assert result.executed and len(result.fills) == 2
    assert len(venue.positions) == 2

    spot = venue.positions[("htx", "btcusdt", "spot")]
    perp = venue.positions[("htx", "btc-usdt-swap", "perp")]
    assert spot.qty > 0 and perp.qty < 0
    # Buy at the ask, sell at the bid, plus slippage in the direction that hurts.
    assert spot.avg_price == pytest.approx(100.1 * (1 + 2e-4))
    assert perp.avg_price == pytest.approx(99.9 * (1 - 2e-4))
    # Spot posts 100 % margin, perp 1/leverage; both fees come out of cash now.
    assert spot.margin_usd == pytest.approx(100.0)
    assert perp.margin_usd == pytest.approx(50.0)
    assert venue.cash == pytest.approx(1_000.0 - 100.0 - 50.0 - 0.2)


def test_the_book_is_delta_neutral_but_not_free():
    venue = PaperVenue(cash_usd=1_000.0, ts=BASE)
    venue.execute(_carry(), _snapshot(0.0))
    exposure = venue.exposure(_snapshot(0.0))
    assert abs(exposure.net_delta_usd) < 0.5
    assert exposure.gross_usd == pytest.approx(200.0, rel=1e-3)
    assert exposure.equity_usd < 1_000.0
    assert exposure.equity_usd > 999.0


def test_a_round_trip_that_moves_nothing_loses_the_costs():
    venue = PaperVenue(cash_usd=1_000.0, ts=BASE)
    venue.execute(_carry(), _snapshot(0.0))
    spot = venue.close(("htx", "btcusdt", "spot"), _snapshot(10.0), reason="test")
    perp = venue.close(("htx", "btc-usdt-swap", "perp"), _snapshot(10.0), reason="test")
    assert spot.bps < 0 and perp.bps < 0
    assert spot.realized_usd < 0 and perp.realized_usd < 0
    assert venue.positions == {}
    assert venue.cash == pytest.approx(1_000.0 + spot.realized_usd + perp.realized_usd)
    assert venue.equity(_snapshot(10.0)) == pytest.approx(venue.cash)


def test_funding_is_paid_by_the_long_and_collected_by_the_short():
    venue = PaperVenue(cash_usd=1_000.0, ts=BASE)
    venue.execute(_carry(), _snapshot(0.0))
    before = venue.cash
    report = venue.accrue_funding(_snapshot(8 * 3600.0))
    assert report.unmeasured == ()
    assert len(report.accruals) == 1
    assert report.accruals[0].periods == 1
    assert report.total_usd > 0  # the short perp receives the positive rate
    assert venue.cash == pytest.approx(before + report.total_usd)

    # A partial period is not a settlement.
    quiet = venue.accrue_funding(_snapshot(9 * 3600.0))
    assert quiet.accruals == ()


def test_missing_funding_is_unmeasured_not_zero():
    venue = PaperVenue(cash_usd=1_000.0, ts=BASE)
    no_funding = Snapshot(ts=BASE, quotes=_snapshot(0.0).quotes)
    venue.execute(_carry(), no_funding)
    later = Snapshot(ts=BASE + 8 * 3600.0, quotes=_snapshot(8 * 3600.0).quotes)
    report = venue.accrue_funding(later)
    assert report.accruals == () and report.unmeasured
    assert "unknown" in report.unmeasured[0]


def test_all_legs_fill_or_none_do():
    venue = PaperVenue(cash_usd=1_000.0, ts=BASE)
    partial = Snapshot(ts=BASE, quotes=(_snapshot(0.0).quotes[0],), funding=_snapshot(0.0).funding)
    result = venue.execute(_carry(), partial)
    assert not result.executed and result.fills == ()
    assert any("no quote" in reason for reason in result.refused)
    assert venue.positions == {}
    assert venue.cash == pytest.approx(1_000.0)


def test_a_spot_short_is_refused_by_the_venue_itself():
    venue = PaperVenue(cash_usd=1_000.0, ts=BASE)
    intent = Intent(
        strategy="reverse-carry",
        legs=(Leg(venue="htx", symbol="btcusdt", side="sell", kind="spot", notional_usd=100.0),),
        expected_edge_bps=5.0,
        horizon_hours=8.0,
        rationale="negative funding, but there is nothing to borrow",
    )
    result = venue.execute(intent, _snapshot(0.0))
    assert not result.executed and any("spot short" in reason for reason in result.refused)


def test_an_unpriced_position_refuses_to_value_itself_at_cost():
    venue = PaperVenue(cash_usd=1_000.0, ts=BASE)
    venue.execute(_carry(), _snapshot(0.0))
    with pytest.raises(Unpriced, match="cannot mark"):
        venue.equity(Snapshot(ts=BASE + 1.0, quotes=()))


def test_state_round_trip_keeps_the_book_and_the_loss_budget():
    venue = PaperVenue(cash_usd=1_000.0, ts=BASE)
    venue.execute(_carry(), _snapshot(0.0))
    restored = PaperVenue.from_state(venue.state())
    assert restored.cash == pytest.approx(venue.cash)
    assert set(restored.positions) == set(venue.positions)
    assert restored.exposure(_snapshot(0.0)) == venue.exposure(_snapshot(0.0))


def test_roll_day_moves_the_budget_only_when_the_date_changes():
    venue = PaperVenue(cash_usd=1_000.0, ts=BASE)
    venue.execute(_carry(), _snapshot(0.0))
    assert not venue.roll_day(_snapshot(3600.0))
    assert venue.roll_day(_snapshot(86_400.0 + 10.0))
    assert venue.day_start_ts == pytest.approx(BASE + 86_400.0 + 10.0)