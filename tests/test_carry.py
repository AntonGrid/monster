"""Carry is only an edge if the funding beats four fills and the basis."""
from __future__ import annotations

import pytest

from monster.contracts import FundingQuote, Quote, Snapshot
from monster.strategies.carry import scan_carry

BASE = 1_700_000_000.0


def _snapshot(*, rate: float = 0.0005, period: float = 8.0, spot: float = 100.0, perp: float = 100.0) -> Snapshot:
    return Snapshot(
        ts=BASE,
        quotes=(
            Quote(venue="htx", symbol="btcusdt", bid=spot - 0.1, ask=spot + 0.1, ts=BASE),
            Quote(venue="htx", symbol="btc-usdt-swap", bid=perp - 0.1, ask=perp + 0.1, ts=BASE),
        ),
        funding=(FundingQuote(venue="htx", symbol="btc-usdt-swap", rate=rate, period_hours=period, ts=BASE),),
    )


def test_a_fat_funding_rate_beats_the_costs_and_becomes_a_neutral_intent():
    scan = scan_carry(_snapshot(rate=0.0005), pairs={"btc": ("btcusdt", "btc-usdt-swap")})
    assert len(scan.candidates) == 1
    candidate = scan.candidates[0]
    assert candidate.accepted and candidate.net_edge_bps is not None
    # 0.0005 per 8 h over 168 h = 21 settlements = 105 bps; costs are 4*(5+2)
    # plus a 20 bp basis (spot ask 100.1 vs perp bid 99.9).
    assert candidate.funding_bps == pytest.approx(105.0)
    # Fills cost 4*(5+2) = 28 bps; the basis is a 20 bp loss (spot ask 100.1
    # against perp bid 99.9).
    assert candidate.cost_bps == pytest.approx(28.0 + 20.0, rel=1e-6)
    assert candidate.net_edge_bps == pytest.approx(57.0, rel=1e-6)
    assert candidate.annualized_pct == pytest.approx(0.0005 * 3 * 365 * 100)

    assert len(scan.intents) == 1
    intent = scan.intents[0]
    assert intent.net_delta_usd == 0.0
    assert intent.expected_edge_bps == pytest.approx(candidate.net_edge_bps)


def test_thin_funding_is_refused_with_the_arithmetic_that_refused_it():
    scan = scan_carry(_snapshot(rate=0.0001), pairs={"btc": ("btcusdt", "btc-usdt-swap")})
    candidate = scan.candidates[0]
    assert not candidate.accepted and not scan.intents
    assert candidate.net_edge_bps is not None and candidate.net_edge_bps < 10.0
    assert "refused" in candidate.reason and "48.00" in candidate.reason


def test_negative_funding_is_not_a_carry_for_a_venue_without_spot_borrow():
    scan = scan_carry(_snapshot(rate=-0.0002), pairs={"btc": ("btcusdt", "btc-usdt-swap")})
    candidate = scan.candidates[0]
    assert not candidate.accepted
    assert "not paid to a short" in candidate.reason


def test_a_missing_leg_is_unmeasured_not_zero():
    snapshot = Snapshot(ts=BASE, quotes=(_snapshot().quotes[0],), funding=_snapshot().funding)
    scan = scan_carry(snapshot, pairs={"btc": ("btcusdt", "btc-usdt-swap")})
    candidate = scan.candidates[0]
    assert not candidate.accepted and candidate.net_edge_bps is None
    assert "unmeasured" in candidate.reason and "perp" in candidate.reason


def test_a_pair_on_another_venue_is_unmeasured_rather_than_transferred():
    snapshot = Snapshot(
        ts=BASE,
        quotes=(
            Quote(venue="htx", symbol="btcusdt", bid=99.9, ask=100.1, ts=BASE),
            Quote(venue="okx", symbol="btc-usdt-swap", bid=99.9, ask=100.1, ts=BASE),
        ),
        funding=(FundingQuote(venue="okx", symbol="btc-usdt-swap", rate=0.0005, period_hours=8.0, ts=BASE),),
    )
    scan = scan_carry(snapshot, pairs={"btc": ("btcusdt", "btc-usdt-swap")}, venue="htx")
    candidate = scan.candidates[0]
    assert not candidate.accepted and "unmeasured" in candidate.reason


def test_the_scan_refuses_arguments_that_are_not_costs():
    with pytest.raises(ValueError, match="notional"):
        scan_carry(_snapshot(), notional_usd=0.0)
    with pytest.raises(ValueError, match="horizon"):
        scan_carry(_snapshot(), horizon_hours=0.0)
    with pytest.raises(ValueError, match="negative"):
        scan_carry(_snapshot(), fee_bps=-1.0)
    with pytest.raises(ValueError, match="min_edge"):
        scan_carry(_snapshot(), min_edge_bps=-1.0)


def test_every_pair_is_reported_even_when_only_one_is_tradeable():
    snapshot = Snapshot(
        ts=BASE,
        quotes=_snapshot().quotes
        + (Quote(venue="htx", symbol="ethusdt", bid=1999.9, ask=2000.1, ts=BASE),),
        funding=_snapshot().funding,
    )
    scan = scan_carry(snapshot)  # default table: btc and eth
    assert [candidate.pair for candidate in scan.candidates] == ["btc", "eth"]
    assert [candidate.accepted for candidate in scan.candidates] == [True, False]
    assert "unmeasured" in scan.candidates[1].reason
