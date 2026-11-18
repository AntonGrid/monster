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


def test_a_second_writer_on_one_chain_is_refused(tmp_path):
    path = tmp_path / "journal.jsonl"
    first = Journal(path, fsync=False)
    second = Journal(path, fsync=False)  # both opened before either appends

    first.append("a", {"n": 1}, ts=1.0)
    with pytest.raises(JournalError) as refused:
        second.append("b", {"n": 2}, ts=2.0)
    assert "another process is writing this chain" in str(refused.value)

    # The refusal left no line behind, so the chain is still exactly the first
    # writer's and the next process can continue it.
    reopened = Journal(path, fsync=False)
    assert reopened.verify() == "ok"
    assert reopened.append("b", {"n": 2}, ts=2.0).seq == 2


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


# ── links: a claim about another chain is a claim somebody can check ─────────


def _linked_pair(tmp_path):
    """A capture chain and a book that links its head, the way the cycle does."""
    capture = Journal(tmp_path / "journal.capture.jsonl", fsync=False)
    capture.append("data.captured", {"pass": 1}, ts=100.0)
    capture.append("data.captured", {"pass": 2}, ts=160.0)
    head = capture.last()
    book = Journal(tmp_path / "journal.jsonl", fsync=False)
    book.append(
        "journal.linked",
        {
            "chain": "capture",
            "path": str(capture.path),
            "verdict": "ok",
            "head": head.hash,
            "seq": head.seq,
            "events": capture.count(),
        },
        ts=200.0,
    )
    return capture, book


def test_a_link_that_still_holds_is_ok(tmp_path):
    _, book = _linked_pair(tmp_path)

    reports = book.verify_links()
    assert len(reports) == 1
    assert reports[0].status == "ok" and reports[0].ok
    assert reports[0].actual_events == 2
    assert "ok (line 2 hash" in reports[0].describe()


def test_a_capture_chain_that_kept_growing_still_holds_its_link(tmp_path):
    """A link is about a moment; a collector that keeps collecting is normal."""
    capture, book = _linked_pair(tmp_path)
    capture.append("data.captured", {"pass": 3}, ts=220.0)

    reports = book.verify_links()
    assert reports[0].status == "ok"
    assert reports[0].actual_events == 3


def test_a_capture_chain_rebuilt_with_other_facts_is_edited(tmp_path):
    """A consistent chain that says something else is exactly what a link is for."""
    capture, book = _linked_pair(tmp_path)
    forged = tmp_path / "forged.jsonl"
    other = Journal(forged, fsync=False)
    other.append("data.captured", {"pass": 1}, ts=100.0)
    other.append("data.captured", {"pass": 2, "failed": []}, ts=160.0)
    capture.path.write_bytes(forged.read_bytes())

    report = book.verify_links()[0]
    assert report.status == "edited" and not report.ok
    assert "rewritten" in report.describe()
    assert report.actual_head == other.last().hash


def test_a_capture_chain_edited_in_the_middle_is_broken(tmp_path):
    capture, book = _linked_pair(tmp_path)
    lines = capture.path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[0])
    payload["payload"]["pass"] = 99
    lines[0] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    capture.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = book.verify_links()[0]
    assert report.status == "broken"
    assert "does not verify" in report.describe()


def test_a_truncated_capture_chain_is_named(tmp_path):
    capture, book = _linked_pair(tmp_path)
    lines = capture.path.read_text(encoding="utf-8").splitlines()
    capture.path.write_text(lines[0] + "\n", encoding="utf-8")

    report = book.verify_links()[0]
    assert report.status == "truncated"
    assert "ends at line 1" in report.describe()


def test_a_deleted_capture_chain_cannot_be_checked(tmp_path):
    capture, book = _linked_pair(tmp_path)
    capture.path.unlink()

    report = book.verify_links()[0]
    assert report.status == "missing" and not report.ok
    assert "gone" in report.describe()


def test_a_link_that_recorded_no_head_makes_no_claim(tmp_path):
    book = Journal(tmp_path / "journal.jsonl", fsync=False)
    book.append(
        "journal.linked",
        {
            "chain": "capture",
            "path": str(tmp_path / "absent.jsonl"),
            "verdict": "unreadable",
            "head": None,
            "seq": None,
            "events": None,
        },
        ts=1.0,
    )

    report = book.verify_links()[0]
    assert report.status == "unlinked" and report.ok
    assert "nothing to check" in report.describe()

