"""The units are code that runs at 3 a.m.; a typo in them costs a week."""
from __future__ import annotations

from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def test_the_collector_and_the_cycle_get_separate_chains():
    """Two writers on one chain break it, so the split is in the unit, not in a habit."""
    text = (DEPLOY / "monster-capture.service").read_text(encoding="utf-8")
    assert "--journal var/journal.capture.jsonl" in text
    assert "--fsync" in text and "Restart=always" in text


def test_the_nightly_audit_keeps_the_gates_bar_and_prints_the_evidence():
    service = (DEPLOY / "monster-audit.service").read_text(encoding="utf-8")
    start = next(line for line in service.splitlines() if line.startswith("ExecStart="))
    # The exit code answers the gate's question — was every body parsed — so the
    # unit may not declare that outcome green, and the log must carry the bodies
    # themselves: "64 unparsable" with no bytes is a fact nobody can act on.
    assert "--skipped 5" in start
    assert "--allow-skipped" not in start
    directives = [line.strip() for line in service.splitlines() if not line.strip().startswith("#")]
    assert not any(line.startswith("SuccessExitStatus") for line in directives)


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


def test_the_cycle_timer_treats_a_young_store_as_waiting_not_as_failure():
    """Exactly one code may stay green: the one that says "not yet"."""
    service = (DEPLOY / "monster-cycle.service").read_text(encoding="utf-8")
    softened = [
        line.strip()
        for line in service.splitlines()
        if line.strip().startswith("SuccessExitStatus")
    ]
    assert softened == ["SuccessExitStatus=3"], (
        "3 is 'the store is younger than the coherence window'; a divergence (2), a stale "
        "collector (4) or a tripped kill switch (1) must still turn the unit red"
    )
    # The cycle must link the collector's chain, or the book audit has nothing to check.
    assert "MONSTER_DATA_JOURNAL=var/journal.capture.jsonl" in service
    assert "--store var/data" in service
    timer = (DEPLOY / "monster-cycle.timer").read_text(encoding="utf-8")
    assert "OnCalendar=" in timer and "WantedBy=timers.target" in timer


def test_the_units_name_one_directory_and_the_operator_fills_it_in():
    """One placeholder, not a hard-coded checkout path: `sed` is the install step."""
    for unit in sorted(DEPLOY.glob("monster-*")):
        text = unit.read_text(encoding="utf-8")
        assert "entrain-core-workspace" not in text, f"{unit.name} hard-codes a checkout path"
        assert "<HOME>" not in text, f"{unit.name} uses a placeholder with no directory in it"
    service = (DEPLOY / "monster-cycle.service").read_text(encoding="utf-8")
    assert "WorkingDirectory=<INSTALL_DIR>" in service
    assert "User=<USER>" in service
