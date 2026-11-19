"""The doctor: one screen that says whether the data layer is healthy *now*.

Every test here builds its store by replaying a synthetic tape through the real
capturer, so what is checked is the same code the VPS runs — and nothing checks
the market.
"""
from __future__ import annotations

import json
import re

from monster.data.__main__ import main as data_main
from monster.data.store import Store

BASE = 1_794_873_600.0
INTERVAL = 300.0


def _replay(tmp_path, *, samples: int = 8, interval: float = INTERVAL, journal: bool = True):
    """Write a tape, replay ``samples`` passes of it, and return what doctor needs."""
    tape_dir, store = tmp_path / "tape", tmp_path / "data"
    journal_path = tmp_path / "journal.capture.jsonl"
    assert data_main(
        [
            "tape", "--root", str(tape_dir), "--store", str(store),
            "--samples", str(samples), "--interval", str(interval), "--base", str(BASE),
        ]
    ) == 0
    argv = [
        "capture", "--fixtures", str(tape_dir), "--store", str(store),
        "--count", str(samples), "--interval", str(interval), "--at", str(BASE),
    ]
    if journal:
        argv += ["--journal", str(journal_path)]
    assert data_main(argv) == 0
    last = BASE + (samples - 1) * interval
    return Store(store), journal_path, last


def _doctor(
    capsys,
    store,
    journal,
    *,
    at,
    interval: float = INTERVAL,
    minutes: float = 20.0,
    window: int = 4,
    max_age: float = 400.0,
):
    """Run the doctor as a cron line would, as JSON, and return ``(code, payload)``."""
    capsys.readouterr()  # the tape and capture lines are not the report
    code = data_main(
        [
            "doctor", "--store", str(store), "--journal", str(journal), "--json",
            "--at", str(at), "--interval", str(interval), "--minutes", str(minutes),
            "--window", str(window), "--max-age", str(max_age),
        ]
    )
    return code, json.loads(capsys.readouterr().out)


def _check(payload, name):
    for check in payload["checks"]:
        if check["name"] == name:
            return check
    raise AssertionError(f"no {name!r} reading in {payload}")


def test_a_healthy_store_is_green_on_every_reading(tmp_path, capsys):
    store, journal_path, last = _replay(tmp_path)
    capsys.readouterr()  # the tape and capture lines are not the report
    code = data_main(
        [
            "doctor", "--store", str(store.root), "--journal", str(journal_path),
            "--at", str(last), "--interval", "300", "--minutes", "20",
            "--window", "4", "--max-age", "400",
        ]
    )
    out = capsys.readouterr().out

    assert code == 0, "a working collector must not wake anybody"
    assert out.startswith("doctor ")
    for name in ("store", "freshness", "coverage", "gaps", "journal", "readiness", "truth", "disk"):
        assert f"[ok  ] {name}" in out, f"{name} should be green:\n{out}"
    assert "[next] nothing to do" in out


def test_a_young_store_waits_instead_of_failing(tmp_path, capsys):
    """A calendar, not a fault: the same fact the cycle reports as exit code 3."""
    store, journal_path, last = _replay(tmp_path)
    code, payload = _doctor(capsys, store.root, journal_path, at=last, window=128)

    assert code == 0, "wait is green here exactly as it is in the cycle"
    assert payload["verdict"] == "wait"
    readiness = _check(payload, "readiness")
    assert readiness["status"] == "wait"
    assert "answers 3" in readiness["detail"] and "more min" in readiness["detail"]
    assert payload["counts"]["wait"] == 1


def test_an_empty_store_fails_rather_than_waits(tmp_path, capsys):
    """Nothing captured is not a calendar: it is a collector that never ran."""
    code, payload = _doctor(capsys, tmp_path / "empty", tmp_path / "journal.jsonl", at=BASE)

    assert code == 1
    assert payload["verdict"] == "fail"
    assert _check(payload, "store")["status"] == "fail"
    assert "no day files" in _check(payload, "store")["detail"]
    assert "no capture chain" in _check(payload, "journal")["detail"]


def test_a_store_that_cannot_be_read_at_all_exits_two(tmp_path, capsys):
    """A wrong path is a question that could not be asked, not a sick store."""
    not_a_directory = tmp_path / "store.txt"
    not_a_directory.write_text("", encoding="utf-8")

    code = data_main(["doctor", "--store", str(not_a_directory), "--journal", str(tmp_path / "j.jsonl")])

    assert code == 2
    assert "data:" in capsys.readouterr().err


def test_a_stopped_collector_is_a_failure_with_the_number(tmp_path, capsys):
    """Stale is not late: the age of the newest quote is the whole sentence."""
    store, journal_path, last = _replay(tmp_path)
    code, payload = _doctor(capsys, store.root, journal_path, at=last + 900.0)

    assert code == 1
    freshness = _check(payload, "freshness")
    assert freshness["status"] == "fail"
    assert "behind" in freshness["detail"] and "limit 400s" in freshness["detail"]
    assert "monster-capture" in freshness["hint"]


def test_a_gap_is_a_warning_that_names_the_hole(tmp_path, capsys):
    """The store keeps the hole; the doctor points at it instead of filling it.

    The hole is written through the store's own API rather than through seven
    capture processes: ``FileTransport`` replays a tape from its first sample on
    every open, so seven separate invocations would all record sample zero.
    """
    from monster.contracts import Quote

    store, journal_path = Store(tmp_path / "data"), tmp_path / "journal.capture.jsonl"
    for index in (0, 1, 2, 3, 5, 6, 7):  # the pass at index 4 never happened
        stamp = BASE + index * INTERVAL
        store.write_quotes(
            [
                Quote(venue=venue, symbol="btcusdt", bid=mid - 0.5, ask=mid + 0.5, ts=stamp)
                for venue, mid in (("htx", 90_000.0), ("okx", 90_002.0), ("bybit", 89_998.0))
            ]
        )

    code, payload = _doctor(capsys, store.root, journal_path, at=BASE + 7 * INTERVAL)

    assert code == 1, "a hole is worth the daily check, not an alarm"
    assert payload["verdict"] == "warn"
    gaps = _check(payload, "gaps")
    assert gaps["status"] == "warn"
    assert "gap(s) in the last 15 min" in gaps["detail"] and "btcusdt" in gaps["detail"]
    assert _check(payload, "coverage")["status"] == "warn"


def test_a_missing_capture_chain_is_a_warning_not_a_failure(tmp_path, capsys):
    """The collector can be asked to write a chain; it cannot be assumed to have."""
    store, _, last = _replay(tmp_path, journal=False)
    code, payload = _doctor(capsys, store.root, tmp_path / "never-written.jsonl", at=last)

    assert code == 1
    assert payload["verdict"] == "warn"
    journal = _check(payload, "journal")
    assert journal["status"] == "warn" and "no capture chain" in journal["detail"]
    assert "--journal" in journal["hint"]


def test_a_rewritten_chain_fails_the_doctor(tmp_path, capsys):
    """An edited past is not a warning: the chain is the claim, and it is false."""
    store, journal_path, last = _replay(tmp_path)
    text = journal_path.read_text(encoding="utf-8")
    tampered = re.sub(r'"written":(\d+)', lambda match: f'"written":{int(match.group(1)) + 1}', text, count=1)
    assert tampered != text, "the capture event should carry a written count"
    journal_path.write_text(tampered, encoding="utf-8")

    code, payload = _doctor(capsys, store.root, journal_path, at=last)

    assert code == 1
    journal = _check(payload, "journal")
    assert journal["status"] == "fail"
    assert "journal.capture.jsonl" in journal["detail"]
    assert payload["verdict"] == "fail"



def test_the_doctor_reports_a_venue_1_7_bps_off_with_a_huge_z(tmp_path, capsys):
    """The VPS question, reproduced: a tiny gap and a tinier MAD.

    Three venues on one symbol, two of which agree to a fifth of a basis point.
    Bybit sits 1.7 bps away — a number a trader calls noise — and the robust z
    is enormous, because the scale is the disagreement of the other two.  The
    doctor is where that sentence gets read, so it says both numbers.
    """
    from monster.contracts import Quote

    centre, store = 90_000.0, Store(tmp_path / "data")
    quotes = []
    for index in range(6):
        ts = BASE + index * 60.0
        quotes += [
            Quote(venue="htx", symbol="btcusdt", bid=centre - 0.5, ask=centre + 0.5, ts=ts),
            Quote(venue="okx", symbol="btcusdt", bid=centre * 1.000002 - 0.5, ask=centre * 1.000002 + 0.5, ts=ts),
            Quote(venue="bybit", symbol="btcusdt", bid=centre * 1.00017 - 0.5, ask=centre * 1.00017 + 0.5, ts=ts),
        ]
    store.write_quotes(quotes)

    code, payload = _doctor(
        capsys, store.root, tmp_path / "journal.jsonl",
        at=BASE + 300.0, interval=60.0, minutes=5.0, window=128, max_age=10.0,
    )

    assert code == 1
    truth = _check(payload, "truth")
    assert truth["status"] == "warn"
    assert "bybit btcusdt" in truth["detail"]
    assert "+1.7 bps" in truth["detail"], "the absolute size must sit on the same line as the z"
    assert "robust z +" in truth["detail"]
    assert "veto" in truth["hint"]
    assert _check(payload, "coverage")["status"] == "ok"
