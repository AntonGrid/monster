"""As-of snapshots and aligned history: no look-ahead, no interpolation, no zeros."""
from __future__ import annotations

import pytest

from monster.contracts import FundingQuote, Quote
from monster.data.derive import DataError, derive, history_window, snapshot_from
from monster.data.store import Store

BASE = 1_794_916_800.0


def _quotes(stamps, symbol: str = "btcusdt", venue: str = "htx", mid: float = 100.0):
    return [
        Quote(venue=venue, symbol=symbol, bid=mid - 0.1, ask=mid + 0.1, ts=BASE + offset)
        for offset in stamps
    ]


def _snapshot(quotes, funding=(), at=BASE, max_age=5.0, max_funding_age=12.0):
    return snapshot_from(
        quotes,
        list(funding),
        at,
        quote_max_age_s=max_age,
        funding_max_age_h=max_funding_age,
        source="unit-test",
    )


def test_the_snapshot_uses_the_latest_reading_at_or_before_the_clock():
    quotes = _quotes([0, 60, 120], mid=100.0) + _quotes([120], venue="okx", mid=100.4)
    snapshot, notes = _snapshot(quotes, at=BASE + 120)
    assert snapshot.ts == BASE + 120
    assert snapshot.quote("htx", "btcusdt").ts == BASE + 120
    assert snapshot.quote("okx", "btcusdt").mid == pytest.approx(100.4)
    assert notes == ()


def test_a_future_quote_is_not_a_reading():
    snapshot, _ = _snapshot(_quotes([0]) + _quotes([600], venue="okx"), at=BASE)
    assert snapshot.quote("okx", "btcusdt") is None


def test_a_stale_venue_is_dropped_and_named_while_a_fresh_one_stays():
    snapshot, notes = _snapshot(_quotes([0]) + _quotes([30], venue="okx"), at=BASE + 30)
    assert snapshot.quote("htx", "btcusdt") is None
    assert snapshot.quote("okx", "btcusdt") is not None
    assert any("30.0s old" in note for note in notes)


def test_nothing_fresh_is_a_DataError_not_an_empty_book():
    with pytest.raises(DataError, match="no quote"):
        _snapshot(_quotes([0]), at=BASE + 600)


def test_stale_funding_is_unmeasured_not_zero():
    old = FundingQuote(venue="htx", symbol="btc-usdt-swap", rate=0.0005, period_hours=8.0, ts=BASE - 13 * 3600)
    snapshot, notes = _snapshot(_quotes([0]), funding=[old], at=BASE)
    assert snapshot.funding == ()
    assert any("13.0h old" in note for note in notes)


def test_history_takes_the_last_mid_of_each_complete_bar():
    quotes = _quotes(list(range(0, 601, 60)))
    history, notes = history_window(quotes, ["btcusdt"], until=BASE + 600, interval_s=60, bars=5)
    values = history["btcusdt"]
    assert len(values) == 5
    # The close of the last bar is the reading stamped at its end.
    assert values[-1] == pytest.approx(100.0)
    assert notes == ()


def test_a_bar_still_forming_is_not_a_close():
    # Ten readings, the last at +540: the bar ending at +600 never closed.
    quotes = _quotes(list(range(0, 541, 60)))
    history, notes = history_window(quotes, ["btcusdt"], until=BASE + 600, interval_s=60, bars=5)
    assert history == {}
    assert any("excluded" in note for note in notes)


def test_series_are_intersected_so_lengths_always_match():
    stamps = list(range(0, 601, 60))
    gappy = [stamp for stamp in stamps if stamp != 300]
    history, notes = history_window(
        _quotes(stamps) + _quotes(gappy, symbol="ethusdt"),
        ["btcusdt", "ethusdt"],
        until=BASE + 600,
        interval_s=60,
        bars=10,
    )
    assert len(history["btcusdt"]) == len(history["ethusdt"]) == 9
    assert any("aligned bars" in note for note in notes)


def test_a_symbol_with_a_hole_is_excluded_rather_than_interpolated():
    stamps = list(range(0, 601, 60))
    history, notes = history_window(
        _quotes(stamps) + _quotes(stamps[:5], symbol="ethusdt"),
        ["btcusdt", "ethusdt"],
        until=BASE + 600,
        interval_s=60,
        bars=10,
    )
    assert set(history) == {"btcusdt"}
    assert any("ethusdt" in note and "not interpolated" in note for note in notes)


def test_derive_wires_snapshot_history_and_quality(demo_store):
    store, tape = demo_store
    derivation = derive(store, interval_s=300.0, bars=32)
    assert derivation.at == tape.last_ts
    assert len(derivation.snapshot.quotes) == 12
    assert len(derivation.snapshot.funding) == 6
    assert set(derivation.history) == {"btcusdt", "btc-usdt-swap", "ethusdt", "eth-usdt-swap"}
    assert {len(values) for values in derivation.history.values()} == {32}
    assert derivation.gaps == ()
    assert all(item.ok() for item in derivation.coverage)
    assert derivation.notes == ()


def test_derive_at_an_earlier_clock_ignores_the_future(demo_store):
    store, tape = demo_store
    at = tape.base_ts + 20 * tape.interval_s
    derivation = derive(store, at=at, interval_s=300.0, bars=16)
    assert derivation.snapshot.ts == at
    assert all(quote.ts <= at for quote in derivation.snapshot.quotes)
    assert {len(values) for values in derivation.history.values()} == {16}


def test_an_empty_store_has_no_answer_and_says_so(tmp_path):
    with pytest.raises(DataError, match="holds no quotes"):
        derive(Store(tmp_path / "empty"))
