"""Parsers: the three venues' shapes, and the refusals that keep them honest."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from monster.data.parsers import EmptyBook, funding_from, quote_from
from monster.data.raw import ShapeError, decimal, pick, timestamp_s

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "raw-fixtures"
STAMP = 1_794_916_800.0
NATIVE = {
    "htx": {"spot": "btcusdt", "perp": "BTC-USDT"},
    "okx": {"spot": "BTC-USDT", "perp": "BTC-USDT-SWAP"},
    "bybit": {"spot": "BTCUSDT", "perp": "BTCUSDT"},
}


def _fixture(venue: str, name: str) -> dict:
    with open(FIXTURES / venue / f"{name}.json", encoding="utf-8") as handle:
        return json.load(handle)


def _quote(venue: str, asset: str = "btc", kind: str = "spot"):
    native = NATIVE[venue][kind].replace("BTC", asset.upper())
    symbol = f"{asset}usdt" if kind == "spot" else f"{asset}-usdt-swap"
    return quote_from(
        venue=venue, payload=_fixture(venue, f"{kind}-{asset}"), symbol=symbol, native=native, kind=kind
    )


def test_htx_spot_reads_the_nested_book_and_milliseconds_in_seconds():
    quote = _quote("htx")
    assert (quote.bid, quote.ask) == (34999.5, 35000.5)
    assert quote.ts == STAMP
    assert quote.symbol == "btcusdt" and quote.venue == "htx"


def test_htx_perp_uses_the_swap_contract_id():
    quote = _quote("htx", kind="perp")
    assert quote.symbol == "btc-usdt-swap"
    assert quote.bid == 35000.5 and quote.ask == 35001.5


def test_htx_refusal_is_a_sentence_with_the_venue_message():
    payload = _fixture("htx", "spot-btc")
    payload["status"] = "error"
    payload["err-msg"] = "invalid symbol"
    with pytest.raises(ShapeError, match="invalid symbol"):
        quote_from(venue="htx", payload=payload, symbol="btcusdt", native="btcusdt", kind="spot")


def test_htx_channel_for_another_contract_is_refused():
    payload = _fixture("htx", "spot-btc")
    payload["ch"] = "market.ethusdt.detail.merged"
    with pytest.raises(ShapeError, match="channel"):
        quote_from(venue="htx", payload=payload, symbol="btcusdt", native="btcusdt", kind="spot")


def test_okx_parses_string_decimals_and_checks_the_instrument():
    quote = _quote("okx")
    assert quote.bid == pytest.approx(34999.7) and quote.ask == pytest.approx(35000.9)
    assert quote.ts == STAMP


def test_okx_answering_about_another_instid_is_refused():
    payload = _fixture("okx", "spot-btc")
    payload["data"][0]["instId"] = "ETH-USDT"
    with pytest.raises(ShapeError, match="ETH-USDT"):
        quote_from(venue="okx", payload=payload, symbol="btcusdt", native="BTC-USDT", kind="spot")


def test_okx_empty_book_is_empty_not_zero():
    payload = _fixture("okx", "spot-btc")
    payload["data"][0]["bidPx"] = ""
    with pytest.raises(EmptyBook, match="no book"):
        quote_from(venue="okx", payload=payload, symbol="btcusdt", native="BTC-USDT", kind="spot")


def test_bybit_reads_the_ticker_and_the_envelope():
    quote = _quote("bybit")
    assert quote.bid == pytest.approx(34999.6) and quote.ask == pytest.approx(35000.6)
    assert quote.ts == STAMP


def test_bybit_retcode_must_be_zero():
    payload = _fixture("bybit", "spot-btc")
    payload["retCode"] = 10001
    payload["retMsg"] = "params error"
    with pytest.raises(ShapeError, match="params error"):
        quote_from(venue="bybit", payload=payload, symbol="btcusdt", native="BTCUSDT", kind="spot")


def test_bybit_without_a_clock_is_refused():
    payload = _fixture("bybit", "spot-btc")
    del payload["time"]
    with pytest.raises(ShapeError, match="neither"):
        quote_from(venue="bybit", payload=payload, symbol="btcusdt", native="BTCUSDT", kind="spot")


def test_a_crossed_book_never_becomes_a_quote():
    payload = _fixture("htx", "spot-btc")
    payload["tick"]["bid"] = [35_100.0, 1.0]
    with pytest.raises(ShapeError, match="crossed"):
        quote_from(venue="htx", payload=payload, symbol="btcusdt", native="btcusdt", kind="spot")


def test_an_empty_price_is_an_empty_book_on_bybit_too():
    payload = _fixture("bybit", "spot-btc")
    payload["result"]["list"][0]["ask1Price"] = ""
    with pytest.raises(EmptyBook, match="no book"):
        quote_from(venue="bybit", payload=payload, symbol="btcusdt", native="BTCUSDT", kind="spot")


def test_funding_is_stamped_with_our_receive_time_and_keeps_its_period():
    rate = funding_from(
        venue="htx",
        payload=_fixture("htx", "funding-btc"),
        symbol="btc-usdt-swap",
        native="BTC-USDT",
        period_hours=8.0,
        received_ts=STAMP,
    )
    assert rate.rate == 0.0005 and rate.period_hours == 8.0 and rate.ts == STAMP


def test_okx_derives_the_period_from_its_own_funding_clock():
    payload = _fixture("okx", "funding-btc")
    payload["data"][0]["nextFundingTime"] = str(int(STAMP * 1000) + 4 * 3_600_000)
    rate = funding_from(
        venue="okx",
        payload=payload,
        symbol="btc-usdt-swap",
        native="BTC-USDT-SWAP",
        period_hours=8.0,
        received_ts=STAMP,
    )
    assert rate.period_hours == 4.0


def test_bybit_funding_rides_in_the_ticker():
    rate = funding_from(
        venue="bybit",
        payload=_fixture("bybit", "funding-btc"),
        symbol="btc-usdt-swap",
        native="BTCUSDT",
        period_hours=8.0,
        received_ts=STAMP,
    )
    assert rate.rate == 0.0005


def test_an_empty_funding_rate_is_silence_not_zero():
    payload = _fixture("htx", "funding-btc")
    payload["data"]["funding_rate"] = ""
    with pytest.raises(ShapeError, match="empty string"):
        funding_from(
            venue="htx",
            payload=payload,
            symbol="btc-usdt-swap",
            native="BTC-USDT",
            period_hours=8.0,
            received_ts=STAMP,
        )


def test_a_funding_rate_without_a_period_is_refused():
    payload = _fixture("bybit", "funding-btc")
    with pytest.raises(ShapeError, match="settlement period"):
        funding_from(
            venue="bybit",
            payload=payload,
            symbol="btc-usdt-swap",
            native="BTCUSDT",
            period_hours=0.0,
            received_ts=STAMP,
        )


def test_decimal_accepts_a_stated_zero_and_refuses_silence():
    assert decimal("rate", "0E-18") == 0.0
    assert decimal("rate", "0.0005") == 0.0005
    for bad in ("", "   ", None, "NaN", "Infinity", "abc", True):
        with pytest.raises(ShapeError):
            decimal("rate", bad)


def test_timestamp_accepts_seconds_and_milliseconds():
    assert timestamp_s("ts", 1_700_000_000) == 1_700_000_000.0
    assert timestamp_s("ts", 1_700_000_000_123) == pytest.approx(1_700_000_000.123)
    with pytest.raises(ShapeError, match="positive"):
        timestamp_s("ts", 0)


def test_pick_names_the_step_that_was_missing():
    with pytest.raises(ShapeError, match=r"tick\.bid"):
        pick({"tick": {"ask": []}}, "tick.bid.0")


def test_an_unknown_venue_is_named_in_the_refusal():
    with pytest.raises(ShapeError, match="known: bybit, htx, okx"):
        quote_from(venue="kraken", payload={}, symbol="btcusdt", native="BTCUSDT", kind="spot")


def test_bybit_category_must_match_the_instrument():
    payload = _fixture("bybit", "spot-btc")
    payload["result"]["category"] = "linear"
    with pytest.raises(ShapeError, match="category"):
        quote_from(venue="bybit", payload=payload, symbol="btcusdt", native="BTCUSDT", kind="spot")


def test_okx_error_envelope_is_refused():
    payload = {"code": "51001", "msg": "Instrument ID does not exist", "data": []}
    with pytest.raises(ShapeError, match="51001"):
        quote_from(venue="okx", payload=payload, symbol="btcusdt", native="BTC-USDT", kind="spot")

