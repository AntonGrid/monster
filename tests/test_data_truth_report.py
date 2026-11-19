"""Does the truth check fire at the right moments: replaying a day, and judging it."""
from __future__ import annotations

import json

import pytest

from monster.contracts import Quote
from monster.data.store import Store
from monster.data.truth_report import TruthDay, truth_day

BASE = 1_794_916_800.0
DAY = "2026-11-17"


def _pass(
    store: Store,
    offset: float,
    mids,
    *,
    symbol: str = "btcusdt",
    venues=("htx", "okx", "bybit"),
    skew: float = 0.02,
):
    """One capture pass: each venue stamps its own book time, ``skew`` apart.

    Live capture keeps the venue's timestamp (``parsers.quote_from`` never
    relabels one), so a pass is a cluster of stamps milliseconds apart, not one
    repeated number.  ``skew=0.0`` reproduces a store written by hand.
    """
    stamp = BASE + offset
    store.write_quotes(
        [
            Quote(venue=venue, symbol=symbol, bid=mid, ask=mid, ts=stamp + index * skew)
            for index, (venue, mid) in enumerate(zip(venues, mids))
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


def test_a_pass_is_a_cluster_of_venue_stamps_not_a_single_stamp(tmp_path):
    """Venues answer one request with three stamps: one pass, not three.

    The VPS replayed a 60 s day as 15 924 passes of one venue each, because the
    quotes carry the venue's own book time and the report grouped by exact
    equality.  Every symbol came out unmeasured and the gate looked like "no
    finding: nothing to explain".
    """
    store = Store(tmp_path)
    for offset in (0, 60, 120):
        _pass(store, offset, (100.0, 100.0, 100.0), skew=1.5)  # four and a half seconds wide
    report = truth_day(store, DAY)
    assert (report.passes, report.sections) == (3, 3)
    assert report.events == ()


def test_a_venue_that_missed_the_pass_is_unmeasured_not_flagged(tmp_path):
    """A quote 90 s late is its own pass, and it judges nothing."""
    store = Store(tmp_path)
    _pass(store, 0, (102.0, 100.0, 100.0))  # htx 200 bps above the crowd
    _pass(store, 60, (100.0, 100.0), venues=("htx", "okx"))  # bybit missed this pass
    store.write_quotes([Quote(venue="bybit", symbol="btcusdt", bid=100.0, ask=100.0, ts=BASE + 90.0)])
    _pass(store, 120, (100.0, 100.0, 100.0))
    report = truth_day(store, DAY)
    # The finding is judged by the next pass that can judge it, not by a lone quote.
    (event,) = report.events
    assert event.verdict == "snapped_back"
    assert "btcusdt" in report.unmeasured


def test_the_day_is_reported_as_json_and_the_gate_exits_nonzero(tmp_path):
    from monster.data.__main__ import main

    store = Store(tmp_path)
    _pass(store, 0, (102.0, 100.0, 100.0))
    _flat(store, (60, 120))
    assert main(["truth-report", "--store", str(tmp_path), "--json"]) == 0
    assert main(["truth-report", "--store", str(tmp_path), "--max-fp-share", "0.5"]) == 1
    assert main(["truth-report", "--store", str(tmp_path), "--max-fp-share", "1.0"]) == 0


def test_a_materiality_floor_stops_microstructure_being_called_a_lie(tmp_path):
    """With three venues the z measures how alone a venue is, not how big the lie is.

    Two venues agree to 0.2 bps and the third is 1.8 bps away: the MAD is set by
    the pair that agree, so z ≈ 6 and the check fires on a gap smaller than the
    spread a mid can legitimately differ by.  The VPS replayed a full day at
    0.4–3.3 bps and got 1 935 findings in 5 421 cross-sections.
    """
    store = Store(tmp_path)
    for offset in (0, 60):
        _pass(store, offset, (100.000, 100.002, 100.020))
    unfenced = truth_day(store, DAY)
    assert [event.verdict for event in unfenced.events] == ["persistent", "unknown"]
    assert unfenced.events[0].deviation_bps == pytest.approx(1.8, abs=0.05)

    floored = truth_day(store, DAY, min_deviation_bps=2.0)
    assert floored.events == ()
    assert floored.sections == 2  # the symbol is still measured, just not lied about
    assert floored.false_positive_share is None


def test_the_cli_sweeps_the_materiality_floor_in_one_pass(tmp_path, capsys):
    from monster.data.__main__ import main

    store = Store(tmp_path)
    for offset in (0, 60):
        _pass(store, offset, (100.000, 100.002, 100.020))
    assert main(["truth-report", "--store", str(tmp_path), "--min-bps", "0,2"]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 3  # a header and one row per floor
    assert "bps floor" in lines[0] and "findings" in lines[0]
    assert lines[1].startswith("        0.0") and lines[2].startswith("        2.0")
    assert lines[2].split()[1] == "0"  # the floor leaves no finding to explain
    assert main(["truth-report", "--store", str(tmp_path), "--min-bps", "lots"]) == 2


def test_a_store_with_no_day_is_a_question_that_cannot_be_asked(tmp_path):
    from monster.data.derive import DataError

    with pytest.raises(DataError):
        truth_day(Store(tmp_path), DAY)
