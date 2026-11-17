"""Is the data any good: gaps, coverage, and re-deriving a day from its raw bodies."""
from __future__ import annotations

import json
from pathlib import Path

from monster.contracts import Quote
from monster.data.capture import Capturer, FileTransport
from monster.data.quality import audit_day, coverage, find_gaps
from monster.data.raw import RawPayload
from monster.data.store import Store

BASE = 1_794_916_800.0
DAY = "2026-11-17"
FIXTURES = Path(__file__).resolve().parents[1] / "data" / "raw-fixtures"


def _series(stamps, symbol: str = "btcusdt", venue: str = "htx"):
    return [Quote(venue=venue, symbol=symbol, bid=100.0, ask=100.2, ts=BASE + offset) for offset in stamps]


def test_gaps_count_the_slots_that_never_arrived():
    quotes = _series([0, 60, 120, 300])
    gaps = find_gaps(quotes, 60)
    assert len(gaps) == 1
    assert gaps[0].missing == 2
    assert gaps[0].start_ts == BASE + 120 and gaps[0].end_ts == BASE + 300
    assert "2 missing" in gaps[0].describe()


def test_jitter_is_not_a_hole():
    assert find_gaps(_series([0, 75, 150]), 60) == ()


def test_a_window_can_hide_a_gap_outside_it():
    quotes = _series([0, 60, 600, 660])
    assert len(find_gaps(quotes, 60)) == 1
    assert find_gaps(quotes, 60, since=BASE + 500) == ()


def test_coverage_is_delivered_over_expected():
    quotes = _series(list(range(0, 600, 60)))  # 10 of 11 slots in [0, 600]
    (item,) = coverage(quotes, 60, since=BASE, until=BASE + 600)
    assert item.present == 10 and item.expected == 11
    assert item.fraction == 10 / 11 and not item.ok()


def test_coverage_names_the_perp_kind_not_spot():
    (item,) = coverage(_series([0, 60], symbol="btc-usdt-swap"), 60)
    assert item.kind == "perp"


def test_audit_reproduces_a_captured_day(tmp_path):
    store = Store(tmp_path)
    capturer = Capturer(store, FileTransport(FIXTURES))
    report = capturer.capture(at=BASE)
    assert report.ok and report.written == 18
    audit = audit_day(store, DAY)
    assert audit.ok, audit.describe()
    assert audit.matched == 18 and audit.raw == 18 and audit.parsed == 18


def test_audit_sees_a_normalized_row_that_no_raw_body_produced(tmp_path):
    store = Store(tmp_path)
    store.write_quotes([Quote(venue="htx", symbol="btcusdt", bid=100.0, ask=100.2, ts=BASE)])
    audit = audit_day(store, DAY)
    assert not audit.ok
    assert len(audit.only_normalized) == 1
    assert "no raw body" in audit.only_normalized[0]


def test_audit_sees_a_value_that_does_not_match_its_own_raw_body(tmp_path):
    store = Store(tmp_path)
    with open(FIXTURES / "htx" / "spot-btc.json", encoding="utf-8") as handle:
        body = json.load(handle)
    store.write_raw(
        RawPayload(
            venue="htx",
            endpoint="spot-btc",
            received_ts=BASE,
            body=body,
            symbol="btcusdt",
            native="btcusdt",
            kind="spot",
        )
    )
    store.write_quotes([Quote(venue="htx", symbol="btcusdt", bid=1.0, ask=2.0, ts=BASE)])
    audit = audit_day(store, DAY)
    assert not audit.ok and len(audit.mismatched) == 1
    assert "raw" in audit.mismatched[0] and "stored" in audit.mismatched[0]


def test_audit_cannot_reparse_a_body_whose_request_was_not_recorded(tmp_path):
    store = Store(tmp_path)
    store.write_raw(RawPayload(venue="htx", endpoint="mystery", received_ts=BASE, body={"a": 1}))
    audit = audit_day(store, DAY)
    assert audit.ok  # nothing to compare, and no lie was written
    assert len(audit.skipped) == 1 and "no request context" in audit.skipped[0]
