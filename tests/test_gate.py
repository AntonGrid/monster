"""Does the gate say "not yet" when there is nothing to certify — and only then?

The cases that matter are the ones a one-sided gate gets wrong: a day that ran
for an hour, a day whose bodies nobody could parse, a week with no findings at
all, and a day whose point estimate is under the gate while its upper bound is
not.  Each of those used to exit 0.
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from monster.contracts import Quote
from monster.data.quality import Audit, Coverage
from monster.data.store import Store, day_start
from monster.data.truth_report import TruthDay, TruthEvent, truth_day
from monster.gate import (
    FAIL,
    PASS,
    UNMEASURABLE,
    DayEvidence,
    Policy,
    evidence,
    judge_days,
    main,
    upper_bound,
)

DAY = "2026-11-17"
BASE = day_start(DAY)


def _coverage(
    day: str = DAY, *, slots: int = 1441, series: int = 18, fraction: float = 1.0
) -> tuple:
    """A day's coverage rows, as a complete capture would leave them."""
    return tuple(
        Coverage(
            venue=("htx", "okx", "bybit")[index % 3],
            symbol=("btcusdt", "ethusdt")[(index // 3) % 2],
            kind=("spot", "perp", "funding")[index // 6],
            first_ts=BASE,
            last_ts=BASE + 86_400.0,
            present=int(round(slots * fraction)),
            expected=slots,
            interval_s=60.0,
        )
        for index in range(series)
    )


def _audit(day: str = DAY, *, raw: int = 1000, skipped: int = 0) -> Audit:
    return Audit(
        day=day,
        raw=raw,
        parsed=raw - skipped,
        matched=12 * (raw - skipped),
        skipped=tuple(f"body-{index}" for index in range(skipped)),
    )


def _events(venue: str, *, judged: int, false_positives: int, unknown: int = 0) -> tuple:
    """``judged`` findings on one venue, of which ``false_positives`` snapped back."""
    return tuple(
        TruthEvent(
            ts=BASE + index * 60.0,
            symbol="btcusdt",
            venue=venue,
            deviation_bps=5.0,
            z=4.0,
            verdict="snapped_back" if index < false_positives else "persistent",
            detail="",
        )
        for index in range(judged)
    ) + tuple(
        TruthEvent(
            ts=BASE + index * 60.0,
            symbol="btcusdt",
            venue=venue,
            deviation_bps=None,
            z=None,
            verdict="unknown",
            detail="stale",
        )
        for index in range(unknown)
    )


def _truth(
    day: str = DAY, *, sections: int = 5400, blind: int = 0, events: tuple = ()
) -> TruthDay:
    return TruthDay(
        day=day,
        passes=1441,
        sections=sections,
        unmeasured=(),
        events=events,
        unmeasured_sections=blind,
    )


def _day(
    day: str = DAY,
    *,
    slots: int = 1441,
    series: int = 18,
    fraction: float = 1.0,
    raw: int = 1000,
    skipped: int = 0,
    sections: int = 5400,
    blind: int = 0,
    events: tuple = (),
) -> DayEvidence:
    return DayEvidence(
        day=day,
        coverages=_coverage(day, slots=slots, series=series, fraction=fraction),
        audit=_audit(day, raw=raw, skipped=skipped),
        truth=_truth(day, sections=sections, blind=blind, events=events),
    )


def _criterion(verdict, name: str):
    for item in verdict.criteria:
        if item.name == name:
            return item
    raise AssertionError(f"{name} is not in the verdict: {[c.name for c in verdict.criteria]}")


# ── the bound ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "k, n, expected",
    [
        (3, 691, 0.01118),  # 2026-11-18, floor 3 bps: under the gate, cannot certify it
        (46, 1934, 0.03031),  # 2026-11-18, no floor
        (16, 1170, 0.02070),  # 2026-11-18, floor 2 bps
        (3, 76, 0.09887),  # 2026-11-17, floor 3 bps: one hour of a day
    ],
)
def test_the_bound_is_the_clopper_pearson_bound(k, n, expected):
    assert upper_bound(k, n) == pytest.approx(expected, abs=5e-5)


def test_silence_is_not_a_rate_of_zero():
    # 0 of 100 says "under 3 %", not "under 1 %": the bound is what the gate reads.
    assert upper_bound(0, 100) == pytest.approx(0.02951, abs=5e-5)
    assert upper_bound(0, 299) == pytest.approx(0.00997, abs=5e-5)
    assert upper_bound(0, 299) < 0.01



# ── what the gate does with a day ───────────────────────────────────────────


def test_an_honest_sample_passes():
    verdict = judge_days([_day(events=_events("htx", judged=2000, false_positives=8))])
    decision = _criterion(verdict, "truth.decision")
    assert verdict.state == PASS and verdict.exit_code == 0
    assert decision.numbers["judged"] == 2000
    assert decision.numbers["upper_bound"] < 0.01
    assert "8/2000" in decision.detail


def test_the_eighteenth_at_three_bps_is_not_a_pass():
    """0.43 % is under the gate and 1.12 % is not: the day cannot certify it."""
    events = (
        _events("htx", judged=230, false_positives=1)
        + _events("okx", judged=230, false_positives=1)
        + _events("bybit", judged=231, false_positives=1)
    )
    verdict = judge_days([_day(events=events)])
    network = _criterion(verdict, "truth.network")
    assert network.numbers["share"] < 0.01
    assert network.numbers["upper_bound"] > 0.01
    assert network.state == UNMEASURABLE
    assert verdict.state == UNMEASURABLE and verdict.exit_code == 3


def test_a_rate_above_the_gate_fails():
    verdict = judge_days([_day(events=_events("htx", judged=1170, false_positives=16))])
    decision = _criterion(verdict, "truth.decision")
    assert decision.state == FAIL and "2.07%" in decision.detail
    assert verdict.state == FAIL and verdict.exit_code == 1


def test_a_day_that_ran_for_an_hour_is_a_fragment_not_a_quiet_day():
    verdict = judge_days([_day(slots=61)])
    coverage = _criterion(verdict, "coverage")
    assert coverage.state == FAIL
    assert "61 slot(s)" in coverage.detail and "1380" in coverage.detail
    assert verdict.state == FAIL


def test_bodies_nobody_can_parse_do_not_count_as_evidence():
    verdict = judge_days([_day(raw=1000, skipped=990)])
    audit = _criterion(verdict, "audit")
    assert audit.state == FAIL and "990 of 1000" in audit.detail
    assert verdict.state == FAIL


def test_a_day_the_check_mostly_could_not_see_is_a_defect():
    verdict = judge_days([_day(sections=900, blind=3000)])
    assert _criterion(verdict, "sample").state == FAIL
    assert verdict.state == FAIL


def test_quiet_and_blind_look_the_same_so_neither_passes():
    verdict = judge_days([_day(events=())])
    decision = _criterion(verdict, "truth.decision")
    assert decision.state == UNMEASURABLE
    assert "quiet week and a blind week" in decision.detail
    assert verdict.state == UNMEASURABLE and verdict.exit_code == 3


def test_the_network_cannot_fail_the_phase():
    """A share that moves with the number of witnesses is not about the system."""
    events = _events("htx", judged=2000, false_positives=8) + _events(
        "okx", judged=1000, false_positives=900
    )
    verdict = judge_days([_day(events=events)])
    assert _criterion(verdict, "truth.network").state == FAIL
    assert _criterion(verdict, "truth.decision").state == PASS
    assert verdict.state == PASS


def test_a_day_of_another_shape_is_refused():
    verdict = judge_days([_day(series=3)])
    assert "3 series" in _criterion(verdict, "coverage").detail
    assert verdict.state == FAIL


def test_a_short_coverage_gap_fails_rather_than_averaging_out():
    verdict = judge_days([_day(fraction=0.94)])
    assert _criterion(verdict, "coverage").state == FAIL
    assert "94.0" in _criterion(verdict, "coverage").detail


def test_a_short_day_names_the_worst_series_not_the_first_one():
    rows = list(_coverage())
    rows[0] = replace(rows[0], present=1300)  # 90.2 %, the first row in sort order
    rows[-1] = replace(rows[-1], present=1200)  # 83.3 %, the actual worst
    verdict = judge_days([replace(_day(), coverages=tuple(rows))])
    detail = _criterion(verdict, "coverage").detail
    assert _criterion(verdict, "coverage").state == FAIL
    assert "83.28" in detail  # the worst series is the one named
    assert "90.21" not in detail
    assert "2 series short" in detail


def test_days_are_pooled_and_named_together():
    first = _day("2026-11-20", events=_events("htx", judged=600, false_positives=2))
    second = _day("2026-11-21", events=_events("htx", judged=600, false_positives=3))
    verdict = judge_days([first, second])
    assert verdict.days == ("2026-11-20", "2026-11-21")
    assert _criterion(verdict, "truth.decision").numbers["judged"] == 1200
    assert "2026-11-20..2026-11-21" in verdict.describe()


def test_the_floor_and_the_gate_are_policy_not_environment():
    assert Policy().min_bps == 3.0
    assert Policy().max_fp_share == 0.01
    with pytest.raises(ValueError):
        Policy(max_fp_share=1.5)
    with pytest.raises(ValueError):
        Policy(subject="   ")


# ── the command ─────────────────────────────────────────────────────────────


def _store(tmp_path, *, passes: int = 3, venues=("htx", "okx", "bybit")) -> Store:
    store = Store(tmp_path / "data")
    for index in range(passes):
        stamp = BASE + index * 60.0
        store.write_quotes(
            [
                Quote(venue=venue, symbol="btcusdt", bid=100.0, ask=100.0, ts=stamp)
                for venue in venues
            ]
        )
    return store


def test_a_store_that_has_not_started_says_not_yet(tmp_path):
    store = Store(tmp_path / "data")
    assert main(["--store", str(store.root), "--day", DAY]) == 3


def test_a_three_pass_store_is_a_fragment_not_a_day(tmp_path):
    store = _store(tmp_path, passes=3)
    assert main(["--store", str(store.root), "--day", DAY]) == 1


def test_the_receipt_and_the_journal_hold_the_verdict(tmp_path):
    store = _store(tmp_path, passes=3)
    receipt = tmp_path / "gate.json"
    journal = tmp_path / "journal.jsonl"
    code = main(
        [
            "--store",
            str(store.root),
            "--day",
            DAY,
            "--receipt",
            str(receipt),
            "--journal",
            str(journal),
            "--json",
        ]
    )
    assert code == 1
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["state"] == "fail" and payload["phase"] == 1
    assert payload["policy"]["min_bps"] == 3.0
    assert "gate.phase1.evaluated" in journal.read_text(encoding="utf-8")


def test_a_floor_below_zero_is_misuse(tmp_path):
    assert main(["--store", str(tmp_path), "--day", DAY, "--min-bps=-1"]) == 2


def test_a_pass_with_two_venues_is_counted_as_unmeasured(tmp_path):
    store = _store(tmp_path, passes=3, venues=("htx", "okx"))
    report = truth_day(store, DAY)
    assert report.sections == 0
    assert report.unmeasured_sections == 3
    assert report.measured_share == 0.0
    assert "3 symbol-pass(es) unmeasured" in report.describe()


def test_evidence_anchors_coverage_to_the_day_not_to_the_data(tmp_path):
    store = _store(tmp_path, passes=3)
    day = evidence(store, DAY, 60.0)
    assert {row.expected for row in day.coverages} == {1441}
    assert {row.present for row in day.coverages} == {3}


# ── the reader is named, not assumed ────────────────────────────────────────


def test_the_readers_constants_are_policy_and_mirror_the_brain():
    """A receipt must say which reader judged, even though the values are fixed.

    ``min_venues`` is written twice — once here, once in ``brain.truth`` — because
    importing the brain would pull numpy, scipy and the ENRG-AI bridge into the
    gate.  The duplicate is checked here, so it cannot drift in silence.
    """
    from monster.brain.truth import (
        DEFAULT_MAX_AGE_S,
        DEFAULT_MAX_DEVIATION_BPS,
        DEFAULT_Z,
        MIN_VENUES,
    )

    policy = Policy()
    assert (policy.min_venues, policy.z) == (MIN_VENUES, DEFAULT_Z)
    assert (policy.max_age_s, policy.max_deviation_bps) == (
        DEFAULT_MAX_AGE_S,
        DEFAULT_MAX_DEVIATION_BPS,
    )
    assert policy.to_dict()["min_venues"] == 3
    assert policy.to_dict()["z"] == 4.0


def test_a_two_venue_reader_is_refused_not_run(tmp_path):
    """Two mids have a median that is one of them: that is a coin, not a check."""
    with pytest.raises(ValueError, match="at least 3"):
        Policy(min_venues=2)
    store = _store(tmp_path, passes=3)
    assert main(["--store", str(store.root), "--day", DAY, "--min-venues", "2"]) == 2


def _skewed_store(tmp_path) -> Store:
    """Three venues where one stands ~48 bps away with z ≈ 16: a finding, but a
    small one — small enough that a *policy* can silence it and a default cannot.
    """
    store = Store(tmp_path / "skewed")
    for index in range(3):
        stamp = BASE + index * 60.0
        store.write_quotes(
            [
                Quote(venue="htx", symbol="btcusdt", bid=100.0, ask=100.0, ts=stamp),
                Quote(venue="okx", symbol="btcusdt", bid=100.02, ask=100.02, ts=stamp),
                Quote(venue="bybit", symbol="btcusdt", bid=100.5, ask=100.5, ts=stamp),
            ]
        )
    return store


def test_evidence_runs_the_check_with_the_policys_reader(tmp_path):
    """The findings must be the ones the policy asked for, not the module defaults."""
    store = _skewed_store(tmp_path)
    default = evidence(store, DAY, 60.0)
    assert [event.venue for event in default.truth.events] == ["bybit"] * 3
    assert all(event.verdict is not None for event in default.truth.events)

    # The floor reaches the check: 48 bps of deviation is below a 50 bps floor.
    floored = evidence(store, DAY, 60.0, policy=Policy(min_bps=50.0))
    assert floored.truth.events == ()
    assert floored.truth.sections == 3  # still checked, just silent

    # So does the z: 16 is not above a threshold of 20.
    strict = evidence(store, DAY, 60.0, policy=Policy(z=20.0))
    assert strict.truth.events == ()


def test_the_receipt_names_the_classifier_that_produced_the_verdict(tmp_path):
    store = _store(tmp_path, passes=3)
    receipt = tmp_path / "gate.json"
    assert main(["--store", str(store.root), "--day", DAY, "--receipt", str(receipt)]) == 1
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    classifier = payload["classifier"]
    assert classifier["snap_fraction"] == 0.3
    assert classifier["snap_floor_bps"] == 1.0
    assert classifier["follow_fraction"] == 0.5
    assert classifier["fp_verdict"] == "snapped_back"
    assert "ambiguous" in classifier["verdicts"]
    assert payload["policy"]["min_venues"] == 3
    assert payload["policy"]["max_age_s"] == 5.0

