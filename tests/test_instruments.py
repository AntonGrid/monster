"""Instrument tables: one name per coin, no guessing, and refusals that name the gap."""
from __future__ import annotations

import pytest

from monster.instruments import (
    FUNDING_PERIOD_HOURS,
    InstrumentError,
    PAIRS,
    asset_for,
    canonical_pair,
    kind_of,
    kinds_for,
    native_symbol,
    venues,
)


def test_the_canonical_pair_is_the_one_the_strategies_trade():
    assert canonical_pair("BTC") == ("btcusdt", "btc-usdt-swap")
    assert kind_of("btcusdt") == "spot" and kind_of("btc-usdt-swap") == "perp"
    assert kind_of("dogeusdt") is None


def test_each_venue_speaks_its_own_id_for_the_same_coin():
    assert native_symbol("htx", "btc", "spot") == "btcusdt"
    assert native_symbol("htx", "btc", "perp") == "BTC-USDT"
    assert native_symbol("okx", "btc", "perp") == "BTC-USDT-SWAP"
    assert native_symbol("bybit", "btc", "spot") == native_symbol("bybit", "btc", "perp") == "BTCUSDT"


def test_an_unknown_venue_or_asset_is_named_not_guessed():
    with pytest.raises(InstrumentError, match="unknown venue"):
        native_symbol("kraken", "btc", "spot")
    with pytest.raises(InstrumentError, match="does not quote"):
        native_symbol("htx", "doge", "spot")
    with pytest.raises(InstrumentError, match="unknown asset"):
        canonical_pair("doge")


def test_bybit_reuses_one_spelling_so_both_kinds_are_allowed():
    assert set(kinds_for("bybit", "BTCUSDT")) == {"spot", "perp"}
    assert kinds_for("okx", "BTC-USDT") == ("spot",)
    assert asset_for("htx", "BTC-USDT") == ("btc", "perp")
    assert asset_for("htx", "nope") is None


def test_all_three_venues_are_known_and_their_funding_periods_are_positive():
    assert venues() == ("bybit", "htx", "okx")
    assert all(FUNDING_PERIOD_HOURS[venue] > 0 for venue in venues())
    assert set(PAIRS) == {"btc", "eth"}
