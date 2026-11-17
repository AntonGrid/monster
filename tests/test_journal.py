"""A journal that can be edited is a log; a chain is a memory."""
from __future__ import annotations

import json

import pytest

from monster.journal import GENESIS, Journal, JournalError


def test_the_chain_links_every_line(tmp_path):
    journal = Journal(tmp_path / "journal.jsonl", fsync=False)
    first = journal.append("cycle.started", {"cycle": 1}, ts=100.0)
    second = journal.append("coherence.read", {"R": 0.42}, ts=101.0)
    third = journal.append("intent.refused", {"why": "no edge"}, ts=102.0)

    assert (first.seq, second.seq, third.seq) == (1, 2, 3)
    assert first.prev == GENESIS
    assert second.prev == first.hash and third.prev == second.hash
    assert journal.verify() == "ok"
    assert journal.count() == 3
    assert journal.last() == third


def test_reopening_continues_the_chain_and_keeps_the_past(tmp_path):
    path = tmp_path / "journal.jsonl"
    first = Journal(path, fsync=False)
    first.append("a", {"n": 1}, ts=1.0)
    first.append("b", {"n": 2}, ts=2.0)

    reopened = Journal(path, fsync=False)
    third = reopened.append("c", {"n": 3}, ts=3.0)
    assert third.seq == 3
    assert reopened.verify() == "ok"
    assert [event.payload["n"] for event in reopened.events()] == [1, 2, 3]


def test_editing_a_line_in_the_middle_is_named_by_verify(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = Journal(path, fsync=False)
    journal.append("decision", {"take": False}, ts=1.0)
    journal.append("decision", {"take": True}, ts=2.0)
    journal.append("decision", {"take": False}, ts=3.0)

    lines = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[1])
    payload["payload"]["take"] = False  # the interesting edit: hide a bad call
    lines[1] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # The tail still verifies, so opening is allowed; auditing is what notices.
    reopened = Journal(path, fsync=False)
    verdict = reopened.verify()
    assert verdict.startswith("line 2")
    assert "does not hash" in verdict or "prev does not hash" in verdict


def test_editing_the_tail_refuses_to_open(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = Journal(path, fsync=False)
    journal.append("decision", {"take": False}, ts=1.0)

    lines = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[0])
    payload["payload"]["take"] = True
    lines[0] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(JournalError, match="fails its own hash"):
        Journal(path, fsync=False)


def test_a_nan_payload_is_refused_before_it_reaches_the_disk(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = Journal(path, fsync=False)
    with pytest.raises(JournalError, match="JSON-clean"):
        journal.append("pnl", {"usd": float("nan")})
    assert not path.exists() or path.read_text(encoding="utf-8") == ""


def test_a_payload_that_is_not_a_mapping_is_refused(tmp_path):
    journal = Journal(tmp_path / "journal.jsonl", fsync=False)
    with pytest.raises(JournalError, match="mapping"):
        journal.append("decision", ["not", "a", "mapping"])  # type: ignore[arg-type]
    with pytest.raises(JournalError, match="kind"):
        journal.append("   ", {})
