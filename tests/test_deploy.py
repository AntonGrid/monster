"""The units are code that runs at 3 a.m.; a typo in them costs a week."""
from __future__ import annotations

from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def test_the_collector_and_the_cycle_get_separate_chains():
    """Two writers on one chain break it, so the split is in the unit, not in a habit."""
    text = (DEPLOY / "monster-capture.service").read_text(encoding="utf-8")
    assert "--journal var/journal.capture.jsonl" in text
    assert "--fsync" in text and "Restart=always" in text


def test_the_book_audit_verifies_first_and_never_softens_a_failure():
    service = (DEPLOY / "monster-book.service").read_text(encoding="utf-8")
    start = next(line for line in service.splitlines() if line.startswith("ExecStart="))
    # Verify before reconcile: comparing a cache with a chain that does not verify
    # answers the wrong question.
    assert start.index("--verify") < start.index("--replay")
    # `SuccessExitStatus=` here would turn "the book disagrees with itself" into a
    # green unit, which is the one outcome this unit exists to prevent.
    directives = [line.strip() for line in service.splitlines() if not line.strip().startswith("#")]
    assert not any(line.startswith("SuccessExitStatus") for line in directives)
    timer = (DEPLOY / "monster-book.timer").read_text(encoding="utf-8")
    assert "OnCalendar=" in timer and "WantedBy=timers.target" in timer
