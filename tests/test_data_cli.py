"""The CLIs: replay, audit, derive, and a cycle that reads the store."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from monster.cycle import main as cycle_main
from monster.data.__main__ import main as data_main
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

    assert code == 2
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
    assert code == 2
    assert "do not pass" in capsys.readouterr().err
