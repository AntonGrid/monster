"""The store: gzip day files, deduplication, conflicts, and a loud bad line."""
from __future__ import annotations

import gzip
import json

import pytest

from monster.contracts import FundingQuote, Quote
from monster.data.raw import RawPayload
from monster.data.store import Store, StoreError

BASE = 1_794_916_800.0
DAY = "2026-11-17"


def _quote(venue: str = "htx", symbol: str = "btcusdt", ts: float = BASE, bid: float = 100.0, ask: float = 100.2):
    return Quote(venue=venue, symbol=symbol, bid=bid, ask=ask, ts=ts)


def _funding(venue: str = "htx", symbol: str = "btc-usdt-swap", ts: float = BASE, rate: float = 0.0005):
    return FundingQuote(venue=venue, symbol=symbol, rate=rate, period_hours=8.0, ts=ts)


def _payload(ts: float = BASE, body: dict | None = None) -> RawPayload:
    return RawPayload(
        venue="htx",
        endpoint="spot-btc",
        received_ts=ts,
        body=body or {"status": "ok"},
        symbol="btcusdt",
        native="btcusdt",
        kind="spot",
        period_hours=0.0,
    )


def test_quotes_roundtrip_in_time_order(tmp_path):
    store = Store(tmp_path)
    store.write_quotes([_quote(ts=BASE + 60), _quote(ts=BASE)])
    batch = store.read_quotes()
    assert [quote.ts for quote in batch.items] == [BASE, BASE + 60]
    assert batch.items[0].bid == 100.0


def test_the_day_file_is_gzip_and_lives_under_the_utc_day(tmp_path):
    store = Store(tmp_path)
    path = store.write_quotes([_quote()])[0]
    assert path == tmp_path / DAY / "htx.jsonl.gz"
    assert path.read_bytes()[:2] == b"\x1f\x8b"
    assert store.days() == (DAY,) and store.venues() == ("htx",)


def test_an_identical_duplicate_is_dropped_without_alarm(tmp_path):
    store = Store(tmp_path)
    store.write_quotes([_quote(), _quote()])
    batch = store.read_quotes()
    assert len(batch.items) == 1 and batch.dropped == 1 and batch.conflicts == ()


def test_two_values_for_one_instant_are_a_conflict_not_a_merge(tmp_path):
    store = Store(tmp_path)
    store.write_quotes([_quote(bid=100.0, ask=100.2), _quote(bid=101.0, ask=101.2)])
    batch = store.read_quotes()
    assert len(batch.items) == 1
    assert batch.items[0].bid == 100.0
    assert len(batch.conflicts) == 1
    assert "first observation wins" in batch.conflicts[0].describe()
    assert batch.conflicts[0].dropped["bid"] == 101.0


def test_queries_can_narrow_by_venue_and_symbol(tmp_path):
    store = Store(tmp_path)
    store.write_quotes(
        [
            _quote(venue="htx", symbol="btcusdt"),
            _quote(venue="okx", symbol="btcusdt"),
            _quote(venue="htx", symbol="ethusdt"),
        ]
    )
    assert {item.venue for item in store.quotes(venues=["okx"])} == {"okx"}
    assert {item.symbol for item in store.quotes(symbols=["ethusdt"])} == {"ethusdt"}


def test_a_window_across_days_reads_both_days_only(tmp_path):
    store = Store(tmp_path)
    store.write_quotes([_quote(ts=BASE), _quote(ts=BASE + 86_400.0)])
    assert len(store.quotes()) == 2
    assert len(store.quotes(since=BASE + 1.0)) == 1
    assert len(store.quotes(until=BASE + 1.0)) == 1
    assert store.days() == (DAY, "2026-11-18")


def test_raw_bodies_keep_the_request_they_answered(tmp_path):
    store = Store(tmp_path)
    store.write_raw(_payload())
    raws = list(store.raws())
    assert len(raws) == 1
    assert raws[0].symbol == "btcusdt" and raws[0].native == "btcusdt" and raws[0].kind == "spot"
    assert raws[0].body == {"status": "ok"}


def test_stats_count_each_record_kind(tmp_path):
    store = Store(tmp_path)
    store.write_raw(_payload())
    store.write_quotes([_quote()])
    store.write_fundings([_funding()])
    assert store.stats() == {"raw": 1, "quote": 1, "funding": 1}


def test_a_bad_line_names_the_file_and_the_line(tmp_path):
    store = Store(tmp_path)
    store.write_quotes([_quote()])
    path = store.path(DAY, "htx")
    with gzip.open(path, "at", encoding="utf-8") as handle:
        handle.write("this is not json\n")
    with pytest.raises(StoreError, match=r"line 2 is not JSON"):
        store.read_quotes()


def test_a_crossed_book_in_the_store_is_refused_on_read(tmp_path):
    store = Store(tmp_path)
    path = store.path(DAY, "htx")
    path.parent.mkdir(parents=True)
    record = {"record": "quote", "venue": "htx", "symbol": "btcusdt", "bid": 101.0, "ask": 100.0, "ts": BASE}
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    with pytest.raises(StoreError, match="crossed"):
        store.read_quotes()


def test_latest_finds_the_newest_stamp_by_day(tmp_path):
    store = Store(tmp_path)
    assert store.latest("quote") is None
    store.write_quotes([_quote(ts=BASE), _quote(ts=BASE + 86_400.0 + 60), _quote(ts=BASE + 60)])
    assert store.latest("quote") == BASE + 86_460.0
    assert store.latest("funding") is None


def test_a_truncated_gzip_member_is_an_error_not_an_empty_day(tmp_path):
    store = Store(tmp_path, read_attempts=2, read_pause_s=0.0)
    path = store.write_quotes([_quote()])[0]
    path.write_bytes(path.read_bytes()[:-6])
    with pytest.raises(StoreError, match="gzip"):
        store.read_quotes()


def test_a_reader_reopens_a_file_whose_tail_is_still_being_written(tmp_path, monkeypatch):
    from monster.data import store as store_module

    store = Store(tmp_path, read_pause_s=0.0)
    store.write_quotes([_quote()])
    real_open = store_module.gzip.open
    calls = {"n": 0}

    def once_truncated(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise EOFError("Compressed file ended before the end-of-stream marker was reached")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(store_module.gzip, "open", once_truncated)
    assert len(store.read_quotes()) == 1
    assert calls["n"] == 2 and store.reopens == 1


def test_a_file_that_never_completes_is_named_with_both_of_its_causes(tmp_path, monkeypatch):
    from monster.data import store as store_module

    store = Store(tmp_path, read_attempts=2, read_pause_s=0.0)
    store.write_quotes([_quote()])

    def always_truncated(*args, **kwargs):
        raise EOFError("Compressed file ended before the end-of-stream marker was reached")

    monkeypatch.setattr(store_module.gzip, "open", always_truncated)
    with pytest.raises(StoreError, match="still be appending"):
        store.read_quotes()
    assert store.reopens == 2
