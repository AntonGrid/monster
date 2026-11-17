"""Does the truth check fire at the right moments: replaying a day, and judging it."""
from __future__ import annotations

import json

import pytest

from monster.contracts import Quote
from monster.data.store import Store
from monster.data.truth_report import TruthDay, truth_day

BASE = 1_794_916_800.0
DAY = "2026-11-17"


def _pass(store: Store, offset: float, mids, *, symbol: str = "btcusdt", venues=("htx", "okx", "bybit")):
    """One capture pass: every venue stamped with the same receive time."""
    stamp = BASE + offset
    store.write_quotes(
        [
            Quote(venue=venue, symbol=symbol, bid=mid, ask=mid, ts=stamp)
            for venue, mid in zip(venues, mids)
        ]
    )


def _flat(store: Store, offsets) -> None:
    for offset in offsets:
        _pass(store, offset, (100.0, 100.0, 100.0))


def test_a_quiet_day_has_nothing_to_explain(tmp_path):
    store = Store(tmp_path)
    _flat(store, (0, 60, 120))
    report = truth_day(store, DAY)
    assert isinstance(report, TruthDay)
    assert (report.day, report.passes, report.sections) == (DAY, 3, 3)
    assert report.events == ()
    assert report.false_positive_share is None
    assert "no finding" in report.describe()


def test_a_lone_print_that_vanishes_is_the_false_positive_bucket(tmp_path):
    store = Store(tmp_path)
    _pass(store, 0, (102.0, 100.0, 100.0))  # htx 200 bps above a crowd that agrees exactly
    _flat(store, (60, 120))
    report = truth_day(store, DAY)
    (event,) = report.events
    assert (event.symbol, event.venue) == ("btcusdt", "htx")
    assert event.verdict == "snapped_back" and event.false_positive
    assert event.deviation_bps == pytest.approx(200.0, abs=1e-6)
    assert report.false_positive_share == 1.0
    assert report.false_positives_per_section == pytest.approx(1 / 3)
    assert "gate: under 1%" in report.describe()


def test_a_dislocation_that_survives_a_minute_is_not_noise(tmp_path):
    store = Store(tmp_path)
    for offset in (0, 60, 120):
        _pass(store, offset, (102.0, 100.0, 100.0))
    report = truth_day(store, DAY)
    verdicts = [event.verdict for event in report.events]
    assert verdicts == ["persistent", "persistent", "unknown"]
    assert report.judged == 2 and report.false_positives == 0
    assert report.false_positive_share == 0.0
    assert "the next pass cannot judge it" in report.events[-1].detail


def test_a_crowd_that_moves_to_the_flagged_price_was_late_not_the_venue(tmp_path):
    store = Store(tmp_path)
    _pass(store, 0, (101.2, 100.0, 100.0))
    _pass(store, 60, (101.2, 101.2, 101.2))
    _pass(store, 120, (101.2, 101.2, 101.2))
    report = truth_day(store, DAY)
    assert report.events[0].verdict == "others_followed"
    assert not report.events[0].false_positive
    assert report.false_positive_share == 0.0


def test_two_venues_are_unmeasured_rather_than_clean(tmp_path):
    store = Store(tmp_path)
    _pass(store, 0, (100.0, 101.0), venues=("htx", "okx"))
    _pass(store, 60, (100.0, 101.0), venues=("htx", "okx"))
    report = truth_day(store, DAY)
    assert report.sections == 0 and report.events == ()
    assert report.unmeasured == ("btcusdt",)


def test_the_day_is_reported_as_json_and_the_gate_exits_nonzero(tmp_path):
    from monster.data.__main__ import main

    store = Store(tmp_path)
    _pass(store, 0, (102.0, 100.0, 100.0))
    _flat(store, (60, 120))
    assert main(["truth-report", "--store", str(tmp_path), "--json"]) == 0
    assert main(["truth-report", "--store", str(tmp_path), "--max-fp-share", "0.5"]) == 1
    assert main(["truth-report", "--store", str(tmp_path), "--max-fp-share", "1.0"]) == 0


def test_a_store_with_no_day_is_a_question_that_cannot_be_asked(tmp_path):
    from monster.data.derive import DataError

    with pytest.raises(DataError):
        truth_day(Store(tmp_path), DAY)
