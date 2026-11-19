"""Phase 1's gate: a number that can say "not yet" as well as "not good enough".

Why this file exists
====================

Every Phase 1 gate in ``docs/MONSTER.md`` could be passed by *silence*:

* ``gaps`` measures coverage between the first and last quote of a series, so a
  collector that ran for one hour of a day reports 100 % and exits 0;
* ``audit`` compares re-parsed bodies with stored rows and calls the day clean
  even when 990 of 1000 bodies could not be parsed at all;
* ``truth-report --max-fp-share`` returns 0 when the day holds no finding,
  because a share over an empty denominator is ``None`` and ``None`` is not a
  failure;
* and ``truth_venues=("htx",)`` is a valid configuration that leaves every
  symbol unmeasured — the check disappears, and so does its veto.

A gate that passes when nothing was measured is not a gate; it is a receipt for
having run.  This module makes the missing side explicit: every criterion now
has three states, not two.

* ``pass`` — there was enough data, and it was good enough;
* ``fail`` — there was data, and it was not good enough (a defect: wake a human);
* ``unmeasurable`` — there was not enough data to say either way (youth, not a
  defect: wait, exactly as the cycle's exit code 3 means wait).

The third state is the whole point.  On 2026-11-18 the truth check at a 3 bps
floor judged 691 findings and called 3 of them false positives: 0.43 %, which
is under the 1 % gate and is *not* evidence that the rate is under 1 %.  The
one-sided 95 % upper bound of 3/691 is 1.12 % — the day cannot certify the gate
it appears to pass.  A point estimate is not a verdict; a bound is.  So the
truth criterion reads the exact Clopper–Pearson upper bound (computed here, in
``math``, so the gate does not grow a scipy dependency for one number) and
refuses to certify while that bound is above the gate.

What the gate stands on
=======================

The false-positive share of *every* flagged venue is a statement about the
observation network: add a fourth witness and the gate gets harder without a
single decision changing.  The carry trades one venue (``Config.venue``, HTX by
default) with both legs on it, and the cycle vetoes a leg only on its own
venue — ``truth.flagged(leg.venue, leg.symbol)``.  So the gate is judged on the
**subject** venue, and the whole network is printed beside it as an advisory
line: useful to watch, never allowed to fail the phase, because a number that
moves when you change the number of witnesses is not measuring the system.

What this module does not do
============================

It does not choose the materiality floor.  The floor is an economic statement —
the quote error that can move the carry's decision by more than its own cost
barrier — and it is frozen in ``docs/prereg/2026-11-19-truth-materiality-floor.md``
before the hold-out days are read.  A floor chosen by minimising the number this
module prints would be a threshold fitted to the week it is then judged on.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from monster.data.derive import DataError
from monster.data.quality import Audit, Coverage, audit_day, coverage
from monster.data.store import Store, StoreError, day_start
from monster.data.truth_report import (
    FP_VERDICT,
    FOLLOW_FRACTION,
    SNAP_FLOOR_BPS,
    SNAP_FRACTION,
    VERDICTS,
    TruthDay,
    truth_day,
)

__all__ = [
    "FAIL",
    "PASS",
    "UNMEASURABLE",
    "Criterion",
    "DayEvidence",
    "Policy",
    "Verdict",
    "classifier",
    "evidence",
    "judge_days",
    "main",
    "upper_bound",
]

PASS = "pass"
FAIL = "fail"
UNMEASURABLE = "unmeasurable"

#: Process exit codes.  ``3`` matches ``monster.cycle``'s ``EXIT_NOT_READY`` on
#: purpose: both mean "the world has not produced enough yet", and both are the
#: code a timer should treat as "run me again tomorrow", not as an alarm.
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_NOT_READY = 3


def _log_comb(n: int, k: int) -> float:
    return math.lgamma(n + 1.0) - math.lgamma(k + 1.0) - math.lgamma(n - k + 1.0)


def _cdf_at_most(k: int, n: int, p: float) -> float:
    """``P(X <= k)`` for ``X ~ Binomial(n, p)``, summed in log space."""
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 0.0
    logs = [
        _log_comb(n, i) + i * math.log(p) + (n - i) * math.log1p(-p) for i in range(k + 1)
    ]
    top = max(logs)
    return min(1.0, math.exp(top) * sum(math.exp(value - top) for value in logs))


def upper_bound(k: int, n: int, *, alpha: float = 0.05) -> float:
    """Exact one-sided Clopper–Pearson upper bound for ``k`` successes in ``n``.

    The smallest ``p`` for which ``P(X <= k; n, p) = alpha``; found by bisection
    because the binomial CDF is monotone in ``p``.  ``k = 0`` is the case that
    matters most: silence is not a rate of zero, it is ``1 - alpha**(1/n)``.
    """
    if n <= 0:
        return 1.0
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    low, high = 0.0, 1.0
    for _ in range(80):
        middle = 0.5 * (low + high)
        if _cdf_at_most(k, n, middle) > alpha:
            low = middle
        else:
            high = middle
    return high


# ── what the receipt must say ───────────────────────────────────────────────


def classifier() -> Dict[str, Any]:
    """The constants that decide what a finding *was*, recorded for the receipt.

    ``Policy`` covers what the gate asks; these cover how the day's findings were
    read.  They are module constants in :mod:`monster.data.truth_report` because
    the classifier is not a knob — moving one changes every historic verdict, so
    it belongs in a prereg, not in a flag.  Recording them verbatim means a
    verdict opened in a month can be reproduced without trusting a commit message.
    """
    return {
        "snap_fraction": SNAP_FRACTION,
        "snap_floor_bps": SNAP_FLOOR_BPS,
        "follow_fraction": FOLLOW_FRACTION,
        "fp_verdict": FP_VERDICT,
        "verdicts": list(VERDICTS),
    }


# ── the numbers ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Policy:
    """Every number the Phase 1 gate is allowed to use, in one place.

    Frozen by ``docs/prereg/2026-11-19-truth-materiality-floor.md``.  Changing a
    field is changing what the gate asks, so it belongs in a preregistration and
    a commit, not in an environment variable: a veto threshold that an operator
    (or a prompt) can set is a veto threshold that will be set to whatever makes
    the run green.
    """

    #: The materiality floor, in bps.  Derived in the prereg from the carry's
    #: cost barrier (``4 * (fee + slippage)`` = 28 bps): a quote error of one
    #: tenth of the barrier cannot move a decision that the barrier has not
    #: already dominated, and 3 bps is that tenth, rounded to the grid the venue
    #: data actually resolves.
    min_bps: float = 3.0
    #: The gate itself: the false-positive share of judged findings.
    max_fp_share: float = 0.01
    #: One-sided risk for the upper bound that decides the gate.
    alpha: float = 0.05
    #: The sample the prereg aims for, printed beside the verdict.  Not a second
    #: gate: the bound already refuses a small sample (0 of 10 has an upper
    #: bound of 26 %), and two rules that can disagree are two rules.
    target_judged: int = 1000
    #: A day on which the check could see less than this share of its
    #: symbol-passes is a defect, not a quiet day.
    min_measured_share: float = 0.90
    #: Coverage per (venue, symbol, kind), and the slots a full day must hold.
    min_coverage: float = 0.98
    min_slots: int = 1380
    #: 3 venues x 2 symbols x (spot, perp, funding) = 18 series: the shape this
    #: gate was written for.  A day with fewer series is not a smaller day, it is
    #: another day, and the prereg has to be rewritten before it can be judged.
    min_series: int = 18
    #: Unparsable raw bodies per day the audit may skip.  Zero: the raw body is
    #: the only thing that makes a parser bug repairable, and a body nobody can
    #: parse is a body whose rows nobody can check.
    max_skipped_share: float = 0.0
    #: The reader the gate judged with: the same four numbers the cycle's truth
    #: check runs with.  They are fields here, not defaults read from
    #: :mod:`monster.brain.truth` at call time, so that a receipt can say which
    #: reader produced the verdict.  Values are frozen by the prereg; passing them
    #: explicitly changes nothing today and makes the artifact self-describing.
    #: ``min_venues`` mirrors ``monster.brain.truth.MIN_VENUES`` — importing that
    #: module would pull numpy, scipy and the ENRG-AI bridge into a gate that
    #: otherwise needs only ``math`` — and ``tests/test_gate.py`` asserts the two
    #: agree, so the mirror cannot drift silently.
    min_venues: int = 3
    z: float = 4.0
    max_age_s: float = 5.0
    max_deviation_bps: float = 100.0
    #: The venue the gate stands on: both carry legs are on ``Config.venue`` and
    #: the cycle vetoes a leg only on its own venue.
    subject: str = "htx"

    def __post_init__(self) -> None:
        if self.min_bps < 0.0:
            raise ValueError("min_bps must be non-negative")
        if self.min_venues < 3:
            # Two venues cannot tell a liar from the truth: with two mids the
            # median *is* one of them, so the check decides by accident.
            raise ValueError(f"min_venues must be at least 3, got {self.min_venues}")
        if self.z <= 0.0 or self.max_age_s <= 0.0 or self.max_deviation_bps <= 0.0:
            raise ValueError("z, max_age_s and max_deviation_bps must be positive")
        for name in ("max_fp_share", "min_measured_share", "min_coverage", "max_skipped_share"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be a share in [0, 1], got {value!r}")
        if not 0.0 < self.alpha < 1.0:
            raise ValueError(f"alpha must be in (0, 1), got {self.alpha!r}")
        if self.min_slots < 1 or self.min_series < 1:
            raise ValueError("min_slots and min_series must be positive")
        if not self.subject.strip():
            raise ValueError("subject must name the venue the gate stands on")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "min_bps": self.min_bps,
            "max_fp_share": self.max_fp_share,
            "alpha": self.alpha,
            "target_judged": self.target_judged,
            "min_measured_share": self.min_measured_share,
            "min_coverage": self.min_coverage,
            "min_slots": self.min_slots,
            "min_series": self.min_series,
            "max_skipped_share": self.max_skipped_share,
            "min_venues": self.min_venues,
            "z": self.z,
            "max_age_s": self.max_age_s,
            "max_deviation_bps": self.max_deviation_bps,
            "subject": self.subject,
        }


@dataclass(frozen=True)
class Criterion:
    """One question the gate asks, its state, and the numbers behind it."""

    name: str
    state: str
    detail: str
    numbers: Mapping[str, Any] = field(default_factory=dict)
    #: Advisory criteria are printed and never decide the verdict: a number that
    #: moves when the number of witnesses changes is not measuring the system.
    advisory: bool = False

    def describe(self) -> str:
        mark = " (advisory)" if self.advisory else ""
        return f"  {self.name}{mark}: {self.state} — {self.detail}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "detail": self.detail,
            "numbers": dict(self.numbers),
            "advisory": self.advisory,
        }


@dataclass(frozen=True)
class DayEvidence:
    """One day's three measurements, already taken."""

    day: str
    coverages: Tuple[Coverage, ...]
    audit: Audit
    truth: TruthDay


@dataclass(frozen=True)
class Verdict:
    """A phase's answer: pass, fail, or a refusal to certify either."""

    phase: int
    days: Tuple[str, ...]
    policy: Policy
    criteria: Tuple[Criterion, ...]

    @property
    def state(self) -> str:
        graded = [item for item in self.criteria if not item.advisory]
        if any(item.state == FAIL for item in graded):
            return FAIL
        if any(item.state == UNMEASURABLE for item in graded):
            return UNMEASURABLE
        return PASS

    @property
    def exit_code(self) -> int:
        return {PASS: EXIT_OK, FAIL: EXIT_FAIL, UNMEASURABLE: EXIT_NOT_READY}[self.state]

    def describe(self) -> str:
        span = self.days[0] if len(self.days) == 1 else f"{self.days[0]}..{self.days[-1]}"
        lines = [f"gate phase {self.phase} on {span}: {self.state}"]
        lines.extend(item.describe() for item in self.criteria)
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "days": list(self.days),
            "state": self.state,
            "exit_code": self.exit_code,
            "policy": self.policy.to_dict(),
            "classifier": classifier(),
            "criteria": [item.to_dict() for item in self.criteria],
        }


# ── the three questions ─────────────────────────────────────────────────────


def _judge_coverage(days: Sequence[DayEvidence], policy: Policy) -> Criterion:
    """Was a full day collected, from every series the gate was written for?"""
    if not days:
        return Criterion("coverage", UNMEASURABLE, "no day was given")
    rows = [(day.day, item) for day in days for item in day.coverages]
    if not rows:
        return Criterion("coverage", UNMEASURABLE, "the store holds no series for these days")
    thin = [(name, item) for name, item in rows if item.expected < policy.min_slots]
    if thin:
        name, item = thin[0]
        return Criterion(
            "coverage",
            FAIL,
            f"{name} {item.venue} {item.symbol} ({item.kind}): {item.expected} slot(s), "
            f"{policy.min_slots} needed for a day — a fragment, not a day",
            {"rows": len(rows), "thin": len(thin), "worst_expected": min(i.expected for _, i in rows)},
        )
    sparse = [day.day for day in days if len(day.coverages) < policy.min_series]
    if sparse:
        return Criterion(
            "coverage",
            FAIL,
            f"{sparse[0]}: {len(days[0].coverages)} series, {policy.min_series} needed "
            "(3 venues x 2 symbols x spot/perp/funding)",
            {"rows": len(rows), "sparse_days": sparse},
        )
    short = [(name, item) for name, item in rows if item.fraction < policy.min_coverage]
    if short:
        name, item = short[0]
        return Criterion(
            "coverage",
            FAIL,
            f"{name} {item.venue} {item.symbol} ({item.kind}): {item.fraction:.2%} of "
            f"{item.expected} slots < {policy.min_coverage:.0%}",
            {"rows": len(rows), "short": len(short), "worst_fraction": min(i.fraction for _, i in rows)},
        )
    worst = min(item.fraction for _, item in rows)
    return Criterion(
        "coverage",
        PASS,
        f"{len(days)} day(s), {len(rows)} series, worst {worst:.2%} of "
        f"{min(item.expected for _, item in rows)} slots",
        {"rows": len(rows), "worst_fraction": worst},
    )


def _judge_audit(days: Sequence[DayEvidence], policy: Policy) -> Criterion:
    """Does every normalized row still follow from its raw body?"""
    if not days:
        return Criterion("audit", UNMEASURABLE, "no day was given")
    raw = sum(day.audit.raw for day in days)
    skipped = sum(len(day.audit.skipped) for day in days)
    if raw == 0:
        return Criterion("audit", UNMEASURABLE, "no raw body was written: nothing to re-parse")
    broken = [day.audit for day in days if not day.audit.ok]
    if broken:
        return Criterion(
            "audit",
            FAIL,
            broken[0].describe(),
            {"raw": raw, "skipped": skipped, "days_with_diffs": len(broken)},
        )
    share = skipped / raw
    if share > policy.max_skipped_share:
        return Criterion(
            "audit",
            FAIL,
            f"{skipped} of {raw} raw bodies could not be parsed ({share:.2%}) — the "
            "key comparison only saw the rest",
            {"raw": raw, "skipped": skipped, "skipped_share": share},
        )
    return Criterion(
        "audit",
        PASS,
        f"{raw} raw bodies re-parsed, {skipped} unparsable",
        {"raw": raw, "skipped": skipped, "skipped_share": share},
    )


def _judge_sample(days: Sequence[DayEvidence], policy: Policy) -> Criterion:
    """Could the truth check see the day, or only the part that happened to arrive?"""
    if not days:
        return Criterion("sample", UNMEASURABLE, "no day was given")
    sections = sum(day.truth.sections for day in days)
    blind = sum(day.truth.unmeasured_sections for day in days)
    total = sections + blind
    if total == 0:
        return Criterion("sample", UNMEASURABLE, "no cross-section at all: nothing was checked")
    share = sections / total
    numbers = {"sections": sections, "unmeasured_sections": blind, "measured_share": share}
    if share < policy.min_measured_share:
        return Criterion(
            "sample",
            FAIL,
            f"{sections}/{total} symbol-passes were measurable ({share:.1%}) < "
            f"{policy.min_measured_share:.0%}: a share over the measurable part is not a share over the day",
            numbers,
        )
    return Criterion("sample", PASS, f"{share:.1%} of {total} symbol-passes were measurable", numbers)


def _judge_truth(
    name: str,
    days: Sequence[DayEvidence],
    policy: Policy,
    *,
    subject: Optional[str],
    advisory: bool,
) -> Criterion:
    """Is the false-positive rate under the gate — and does this sample know?

    The verdict is read off the *upper* bound, not the point estimate.  3 of 691
    is 0.43 % and 1.12 % at 95 %: a day that prints a number below the gate and
    cannot certify it is ``unmeasurable``, not a pass.  The bound is one-sided on
    purpose — the only question asked is "could the rate be above 1 %".
    """
    events = [event for day in days for event in day.truth.events]
    if subject is not None:
        events = [event for event in events if event.venue == subject.strip().lower()]
    judged = [event for event in events if event.verdict != "unknown"]
    false_positives = sum(1 for event in judged if event.false_positive)
    sample = len(judged)
    label = subject or "network"
    if sample == 0:
        return Criterion(
            name,
            UNMEASURABLE,
            f"no judged finding on {label}: a quiet week and a blind week look the same",
            {"judged": 0},
            advisory,
        )
    share = false_positives / sample
    bound = upper_bound(false_positives, sample, alpha=policy.alpha)
    numbers = {
        "judged": sample,
        "false_positives": false_positives,
        "share": share,
        "upper_bound": bound,
        "gate": policy.max_fp_share,
        "target_judged": policy.target_judged,
    }
    head = f"{false_positives}/{sample} judged = {share:.2%}, 95% upper {bound:.2%}"
    if bound > policy.max_fp_share:
        return Criterion(
            name,
            FAIL if sample >= policy.target_judged else UNMEASURABLE,
            head
            + (
                f" > {policy.max_fp_share:.2%}: this sample cannot certify the gate"
                if sample < policy.target_judged
                else f" > {policy.max_fp_share:.2%}"
            ),
            numbers,
            advisory,
        )
    return Criterion(name, PASS, head + f" <= {policy.max_fp_share:.2%}", numbers, advisory)


def judge_days(days: Sequence[DayEvidence], policy: Optional[Policy] = None) -> Verdict:
    """Take the phase's measurements and answer pass, fail, or not yet."""
    chosen = policy or Policy()
    ordered = tuple(days)
    if not ordered:
        raise ValueError("judge_days needs at least one day")
    criteria = (
        _judge_coverage(ordered, chosen),
        _judge_audit(ordered, chosen),
        _judge_sample(ordered, chosen),
        _judge_truth("truth.decision", ordered, chosen, subject=chosen.subject, advisory=False),
        _judge_truth("truth.network", ordered, chosen, subject=None, advisory=True),
    )
    return Verdict(phase=1, days=tuple(day.day for day in ordered), policy=chosen, criteria=criteria)


def evidence(store: Store, day: str, interval_s: float, *, policy: Optional[Policy] = None) -> DayEvidence:
    """Measure one day the way the gate asks: coverage anchored to the day, audit, truth."""
    chosen = policy or Policy()
    start = day_start(day)
    until = start + 86_400.0
    items = tuple(store.quotes(since=start, until=until)) + tuple(
        store.fundings(since=start, until=until)
    )
    return DayEvidence(
        day=day,
        coverages=coverage(items, interval_s, since=start, until=until),
        audit=audit_day(store, day),
        truth=truth_day(
            store,
            day,
            min_venues=chosen.min_venues,
            z_threshold=chosen.z,
            max_age_s=chosen.max_age_s,
            max_deviation_bps=chosen.max_deviation_bps,
            min_deviation_bps=chosen.min_bps,
        ),
    )


# ── command line ────────────────────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m monster.gate",
        description=(
            "Judge Phase 1 from the store.  Exit 0 pass, 1 fail, 3 not measurable "
            "(wait and run again), 2 misuse."
        ),
    )
    parser.add_argument("--store", default="var/data", help="recorded venue data")
    parser.add_argument(
        "--day",
        nargs="+",
        required=True,
        metavar="YYYY-MM-DD",
        help="one or more days, judged as one pooled sample",
    )
    parser.add_argument("--interval", type=float, default=60.0, help="capture interval, seconds")
    parser.add_argument("--min-bps", type=float, default=None, dest="min_bps")
    parser.add_argument("--max-fp-share", type=float, default=None, dest="max_fp_share")
    parser.add_argument("--alpha", type=float, default=None)
    parser.add_argument("--target-judged", type=int, default=None, dest="target_judged")
    parser.add_argument("--min-slots", type=int, default=None, dest="min_slots")
    parser.add_argument("--min-coverage", type=float, default=None, dest="min_coverage")
    parser.add_argument("--min-venues", type=int, default=None, dest="min_venues")
    parser.add_argument("--z", type=float, default=None)
    parser.add_argument("--max-age-s", type=float, default=None, dest="max_age_s")
    parser.add_argument(
        "--max-deviation-bps", type=float, default=None, dest="max_deviation_bps"
    )
    parser.add_argument("--subject", default=None, help="venue the gate stands on")
    parser.add_argument("--json", action="store_true", help="print the verdict as JSON")
    parser.add_argument("--receipt", type=Path, default=None, help="write the verdict here")
    parser.add_argument("--journal", type=Path, default=None, help="append gate.phase1.evaluated")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    overrides = {
        "min_bps": args.min_bps,
        "max_fp_share": args.max_fp_share,
        "alpha": args.alpha,
        "target_judged": args.target_judged,
        "min_slots": args.min_slots,
        "min_coverage": args.min_coverage,
        "min_venues": args.min_venues,
        "z": args.z,
        "max_age_s": args.max_age_s,
        "max_deviation_bps": args.max_deviation_bps,
        "subject": args.subject,
    }
    try:
        policy = replace(
            Policy(), **{key: value for key, value in overrides.items() if value is not None}
        )
    except (TypeError, ValueError) as error:
        print(f"monster.gate: {error}", file=sys.stderr)
        return EXIT_USAGE

    store = Store(args.store)
    try:
        days = [evidence(store, day, args.interval, policy=policy) for day in args.day]
    except (DataError, StoreError) as error:
        # A day the store does not hold has not happened yet: the same answer as
        # an empty sample, and the same exit code.
        print(f"monster.gate: {error}", file=sys.stderr)
        return EXIT_NOT_READY

    verdict = judge_days(days, policy)
    print(verdict.describe())
    payload = verdict.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    if args.receipt is not None:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.journal is not None:
        from monster.journal import Journal

        Journal(args.journal).append("gate.phase1.evaluated", payload)
    return verdict.exit_code


if __name__ == "__main__":  # pragma: no cover - the tests call main() directly
    raise SystemExit(main())
