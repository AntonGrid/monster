"""The wire refuses what it cannot mean — and says which field it refused."""
from __future__ import annotations

import math

import pytest

from monster.contracts import (
    ClosedTrade,
    ContractError,
    Fill,
    FundingQuote,
    Intent,
    Leg,
    Quote,
    Snapshot,
    canonical,
)


def test_quote_reads_a_book_and_refuses_a_crossed_one():
    quote = Quote(venue="HTX", symbol="BTCUSDT", bid=100.0, ask=100.2, ts=10.0)
    assert quote.venue == "htx" and quote.symbol == "btcusdt"
    assert quote.mid == pytest.approx(100.1)
    assert quote.spread_bps == pytest.approx(1e4 * 0.2 / 100.1)
    with pytest.raises(ContractError, match="crossed book"):
        Quote(venue="htx", symbol="btcusdt", bid=101.0, ask=100.0, ts=10.0)


def test_funding_is_a_rate_with_a_period_and_unknown_is_not_zero():
    rate = FundingQuote(venue="htx", symbol="btcusdt", rate=0.0001, period_hours=8.0, ts=1.0)
    assert rate.daily_rate == pytest.approx(0.0003)
    # A period is not optional: without it the same rate is a different business.
    with pytest.raises(ContractError):
        FundingQuote(venue="htx", symbol="btcusdt", rate=0.0001, period_hours=0.0, ts=1.0)
    with pytest.raises(ContractError, match="finite"):
        FundingQuote(venue="htx", symbol="btcusdt", rate=math.nan, period_hours=8.0, ts=1.0)


def test_snapshot_is_the_look_ahead_barrier():
    quote = Quote(venue="htx", symbol="btcusdt", bid=100.0, ask=100.1, ts=10.0)
    snapshot = Snapshot(ts=11.0, quotes=(quote,), source="fixture")
    assert snapshot.quote("HTX", "BTCUSDT") is quote
    assert snapshot.symbols() == ("btcusdt",)

    future = Quote(venue="htx", symbol="btcusdt", bid=100.0, ask=100.1, ts=12.0)
    with pytest.raises(ContractError, match="reading from the future"):
        Snapshot(ts=11.0, quotes=(future,))

    with pytest.raises(ContractError, match="duplicate quote"):
        Snapshot(ts=11.0, quotes=(quote, quote))

    restored = Snapshot.from_dict(snapshot.to_dict())
    assert restored == snapshot


def test_unknown_fields_are_refused_not_ignored():
    document = {
        "ts": 11.0,
        "quotes": [{"venue": "htx", "symbol": "btcusdt", "bid": 1.0, "ask": 1.1, "ts": 10.0, "mid": 1.05}],
    }
    with pytest.raises(ContractError, match="unknown field"):
        Snapshot.from_dict(document)


def test_canonical_refuses_nan_because_nan_never_equals_itself():
    with pytest.raises(ValueError):
        canonical({"pnl": math.nan})


def test_intent_id_is_the_content_and_a_carry_is_delta_neutral():
    legs = (
        Leg(venue="htx", symbol="btcusdt", side="buy", kind="spot", notional_usd=100.0),
        Leg(venue="htx", symbol="btcusdt", side="sell", kind="perp", notional_usd=100.0),
    )
    intent = Intent(
        strategy="carry",
        legs=legs,
        expected_edge_bps=12.0,
        horizon_hours=8.0,
        rationale="funding 0.0001 per 8h, costs 4 bps a round trip",
    )
    assert intent.net_delta_usd == 0.0
    assert intent.gross_usd == 200.0
    assert intent.symbols == ("btcusdt",)
    clone = Intent.from_dict(intent.to_dict())
    assert clone == intent and clone.id == intent.id

    with pytest.raises(ContractError, match="does not match the intent"):
        Intent(
            strategy="carry",
            legs=legs,
            expected_edge_bps=12.0,
            horizon_hours=8.0,
            rationale="same words, different id",
            id=intent.id,
        )


def test_intent_without_legs_is_not_a_proposal():
    with pytest.raises(ContractError, match="without legs"):
        Intent(strategy="empty", legs=(), expected_edge_bps=1.0, horizon_hours=1.0, rationale="nothing")


def test_fill_and_closed_trade_round_trip():
    fill = Fill(
        ts=1.0,
        strategy="carry",
        venue="htx",
        symbol="btcusdt",
        kind="perp",
        side="sell",
        qty=0.01,
        price=100.0,
        fee_usd=0.04,
        notional_usd=100.0,
    )
    assert Fill.from_dict(fill.to_dict()) == fill

    trade = ClosedTrade(
        strategy="carry",
        venue="htx",
        symbol="btcusdt",
        kind="perp",
        entry_side="sell",
        qty=0.01,
        entry_price=100.0,
        exit_price=99.0,
        entry_notional_usd=100.0,
        fees_usd=0.08,
        funding_usd=0.05,
        realized_usd=0.97,
        bps=97.0,
        opened_ts=1.0,
        closed_ts=2.0,
        reason="horizon",
    )
    assert ClosedTrade.from_dict(trade.to_dict()) == trade

    with pytest.raises(ContractError, match="precedes"):
        ClosedTrade(**{**trade.to_dict(), "opened_ts": 3.0})
