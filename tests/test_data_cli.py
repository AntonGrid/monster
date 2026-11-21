"""The CLIs: replay, audit, derive, and a cycle that reads the store."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from monster.contracts import FundingQuote, Quote
from monster.cycle import main as cycle_main
from monster.data.__main__ import main as data_main
from monster.data.raw import RawPayload
from monster.data.store import Store
from monster.journal import Journal

BASE = 1_794_873_600.0
FIXTURE_TS = 1_794_916_800.0
FIXTURES = Path(__file__).resolve().parents[1] / "data" / "raw-fixtures"


def test_tape_capture_audit_and_derive_run_in_sequence(tmp_path, capsys):
    tape_dir, store = tmp_path / "tape", tmp_path / "data"
    assert data_main(
        ["tape", "--root", str(tape_dir), "--store", str(store), "--samples", "8", "--interval", "300", "--base", str(BASE)]
    ) == 0
    assert "synthetic, not market data" in capsys.readouterr().out

    assert data_main(
        ["capture", "--fixtures", str(tape_dir), "--store", str(store), "--count", "8", "--interval", "300", "--at", str(BASE)]
    ) == 0
    assert data_main(["audit", "--store", str(store)]) == 0
    assert data_main(["gaps", "--store", str(store), "--interval", "300"]) == 0

    capsys.readouterr()  # the capture lines above are not part of the JSON
    assert data_main(["derive", "--store", str(store), "--interval", "300", "--bars", "4", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["quotes"] == 12 and payload["funding"] == 6
    assert payload["history_bars"] == {"btc-usdt-swap": 4, "btcusdt": 4, "eth-usdt-swap": 4, "ethusdt": 4}


def test_capture_journals_each_pass_as_a_chained_event(tmp_path):
    journal_path = tmp_path / "journal.jsonl"
    assert data_main(
        [
            "capture", "--fixtures", str(FIXTURES), "--store", str(tmp_path / "data"),
            "--at", str(FIXTURE_TS), "--journal", str(journal_path),
        ]
    ) == 0
    journal = Journal(journal_path)
    assert journal.verify() == "ok"
    kinds = [event.kind for event in journal.events()]
    assert kinds == ["data.captured"]
    assert journal.last().payload["written"] == 18


def test_a_broken_answer_makes_capture_exit_nonzero(tmp_path, capsys):
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixtures)
    path = fixtures / "htx" / "spot-btc.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["tick"]["bid"]
    path.write_text(json.dumps(document), encoding="utf-8")

    code = data_main(
        ["capture", "--fixtures", str(fixtures), "--store", str(tmp_path / "data"), "--at", str(FIXTURE_TS)]
    )
    assert code == 1
    assert "htx spot-btc" in capsys.readouterr().out


def test_derive_on_an_empty_store_exits_two_with_a_sentence(tmp_path, capsys):
    assert data_main(["derive", "--store", str(tmp_path / "empty")]) == 2
    assert "holds no quotes" in capsys.readouterr().err


def test_audit_with_no_days_exits_two(tmp_path, capsys):
    assert data_main(["audit", "--store", str(tmp_path / "empty")]) == 2
    assert "no day to audit" in capsys.readouterr().err


def test_audit_names_the_bodies_it_could_not_parse(tmp_path, capsys):
    store = Store(tmp_path / "data")
    store.write_raw(
        RawPayload(
            venue="htx",
            endpoint="funding-btc",
            received_ts=FIXTURE_TS,
            body={"status": "error", "err-code": "429", "err-msg": "too many requests"},
            symbol="btcusdt",
            native="BTC-USDT",
            kind="funding",
            period_hours=8.0,
        )
    )
    args = ["audit", "--store", str(store.root), "--day", "2026-11-17", "--skipped", "1"]
    # The gate refuses a day with an unparsable body, so the nightly audit has to
    # refuse it too — an alarm that rings on the 25th is not an alarm.
    assert data_main(args) == 1
    printed = capsys.readouterr().out
    assert "htx/funding-btc" in printed and "too many requests" in printed
    # The other question — can the store re-derive its day? — is still askable.
    assert data_main(["audit", "--store", str(store.root), "--day", "2026-11-17", "--allow-skipped"]) == 0


def test_gaps_only_sees_a_funding_hole_when_asked(tmp_path, capsys):
    store = Store(tmp_path / "data")
    store.write_quotes(
        [Quote(venue="htx", symbol="btcusdt", bid=1.0, ask=2.0, ts=BASE + offset) for offset in (0, 60, 120)]
    )
    store.write_fundings(
        [
            FundingQuote(venue="htx", symbol="btcusdt", rate=1e-4, period_hours=8.0, ts=BASE + offset)
            for offset in (0, 120)  # the 60 s slot never arrived
        ]
    )
    window = ["--store", str(store.root), "--interval", "60", "--since", str(BASE), "--until", str(BASE + 120)]
    assert data_main(["gaps", *window]) == 0  # quotes alone are complete
    assert data_main(["gaps", *window, "--funding"]) == 1
    printed = capsys.readouterr().out
    assert "htx btcusdt (funding): 1 missing" in printed
    assert "htx btcusdt (funding): 66.7% of 3 slots" in printed


def test_a_cycle_reads_the_store_and_journals_its_provenance(tmp_path, demo_store, monkeypatch, capsys):
    store, tape = demo_store
    monkeypatch.setenv("MONSTER_COHERENCE_WINDOW", "32")
    monkeypatch.setenv("MONSTER_SURROGATES", "40")
    journal_path, state_path = tmp_path / "journal.jsonl", tmp_path / "state.json"
    code = cycle_main(
        [
            "--store", str(store.root), "--interval", "300",
            # The synthetic tape is dated, not "now": the freshness gate is about
            # a live collector falling behind, and this run is a replay of a store.
            "--max-snapshot-age", "0",
            "--journal", str(journal_path), "--state", str(state_path),
        ]
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "coherence R=" in printed
    assert "btc htx: accepted" in printed

    journal = Journal(journal_path)
    assert journal.verify() == "ok"
    events = {event.kind: event for event in journal.events()}
    assert "data.derived" in events and "coherence.read" in events
    assert events["data.derived"].payload["quotes"] == 12
    assert events["data.derived"].payload["at"] == tape.last_ts
    assert all(event.ts <= tape.last_ts for event in journal.events())


def test_a_capture_pass_that_stopped_is_not_a_picture_of_now(tmp_path, demo_store, monkeypatch, capsys):
    store, tape = demo_store
    monkeypatch.setenv("MONSTER_COHERENCE_WINDOW", "32")
    journal_path, state_path = tmp_path / "journal.jsonl", tmp_path / "state.json"
    code = cycle_main(
        [
            "--store", str(store.root), "--interval", "300",
            "--max-snapshot-age", "180",
            "--journal", str(journal_path), "--state", str(state_path),
        ]
    )

    assert code == 4  # stale: the collector is behind, and 3 is reserved for "young"
    assert "collector is behind" in capsys.readouterr().err
    events = list(Journal(journal_path).events())
    assert [event.kind for event in events] == ["cycle.stale"]
    assert events[0].payload["at"] == tape.last_ts
    assert events[0].payload["limit_s"] == 180.0
    assert not state_path.exists()


def test_the_truth_check_uses_only_the_configured_witnesses(tmp_path, demo_store, monkeypatch, capsys):
    store, _ = demo_store
    monkeypatch.setenv("MONSTER_TRUTH_VENUES", "htx")
    monkeypatch.setenv("MONSTER_COHERENCE_WINDOW", "32")
    monkeypatch.setenv("MONSTER_SURROGATES", "40")
    journal_path = tmp_path / "journal.jsonl"
    code = cycle_main(
        [
            "--store", str(store.root), "--interval", "300",
            "--max-snapshot-age", "0",
            "--journal", str(journal_path), "--state", str(tmp_path / "state.json"),
        ]
    )
    assert code == 0
    assert "truth: 0 checked" in capsys.readouterr().out
    truth = next(event for event in Journal(journal_path).events() if event.kind == "truth.read")
    assert truth.payload["checked"] == []
    assert len(truth.payload["unmeasured"]) == 4


def test_a_cycle_refuses_to_mix_a_frozen_snapshot_with_a_store(tmp_path, demo_store, capsys):
    store, _ = demo_store
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"ts": 1.0, "quotes": []}), encoding="utf-8")
    code = cycle_main(
        [
            "--store", str(store.root), "--snapshot", str(snapshot),
            "--journal", str(tmp_path / "journal.jsonl"), "--state", str(tmp_path / "state.json"),
        ]
    )
    assert code == 64  # a wrong command, not a wrong book: nothing was attempted
    assert "do not pass" in capsys.readouterr().err


def test_a_young_store_is_waited_for_and_nothing_is_traded(tmp_path, monkeypatch, capsys):
    """The first two hours of a week are not a fault; they are a calendar."""
    from monster.data.capture import Capturer, FileTransport
    from monster.data.store import Store
    from monster.data.synthetic import write_demo_tape

    monkeypatch.setenv("MONSTER_COHERENCE_WINDOW", "32")
    tape = write_demo_tape(tmp_path / "tape", samples=5, interval_s=300.0)
    store = Store(tmp_path / "data")
    capturer = Capturer(store, FileTransport(tape.root))
    for index in range(tape.samples):
        assert capturer.capture(at=tape.base_ts + index * tape.interval_s).ok
    journal_path, state_path = tmp_path / "journal.jsonl", tmp_path / "state.json"

    code = cycle_main(
        [
            "--store", str(store.root), "--interval", "300",
            "--max-snapshot-age", "0",
            "--journal", str(journal_path), "--state", str(state_path),
        ]
    )

    assert code == 3, "3 is the one code that means 'wait': 2 and 4 mean 'look'"
    assert "young store is waited for" in capsys.readouterr().err
    events = list(Journal(journal_path).events())
    # The provenance is still recorded — the store said something true, it just said
    # too little — and the refusal names both numbers instead of a guess.
    assert [event.kind for event in events] == ["data.derived", "cycle.not_ready"]
    refusal = events[-1].payload
    # Five bars cannot cover 90% of a 32-bar window, so no series is aligned yet: the
    # honest count is zero, and the note beside it says how far the store reaches.
    assert refusal["bars"] == 0 and refusal["window"] == 32
    assert any("excluded for missing bars" in note for note in refusal["notes"])
    assert refusal["interval_s"] == 300.0
    assert not state_path.exists(), "a cycle that did not decide must not write a book"
    assert "journal.linked" not in [event.kind for event in events]


def test_a_store_with_no_quotes_yet_is_not_ready_either(tmp_path, capsys):
    """An empty directory is a collector that has not started, not a broken store."""
    journal_path = tmp_path / "journal.jsonl"
    code = cycle_main(
        [
            "--store", str(tmp_path / "empty"),
            "--journal", str(journal_path), "--state", str(tmp_path / "state.json"),
        ]
    )

    assert code == 3
    assert "holds no quotes" in capsys.readouterr().err
    assert list(Journal(journal_path).events()) == []


def test_every_exit_code_is_one_fact():
    """A number that means three things is a number nobody reads."""
    from monster.cycle import (
        EXIT_DIVERGED,
        EXIT_NOT_READY,
        EXIT_OK,
        EXIT_REFUSED,
        EXIT_STALE,
        EXIT_USAGE,
    )

    codes = (EXIT_OK, EXIT_REFUSED, EXIT_DIVERGED, EXIT_NOT_READY, EXIT_STALE, EXIT_USAGE)
    assert codes == (0, 1, 2, 3, 4, 64)
    assert len(set(codes)) == len(codes)
