"""``doctor``: one screen that says whether the data layer is healthy *now*.

Why one command and not five
============================

The nightly checks answer "was yesterday what it claimed to be": ``audit``
re-parses a closed day, ``gaps`` counts the holes in it, ``truth-report`` judges
the findings the next pass resolved.  They need a closed day file, so they run
at 00:10 and they say nothing about this minute.  The hourly cycle answers "may
I trade" and stays quiet about why it is waiting.  Between them sits the
question an operator actually has at 14:00 on day two: *is the collector
delivering, is the chain intact, and if not, which one is it?*

``doctor`` answers that in eight readings, each one a sentence with a number:

``store``      how many days, venues and records, and whether they parse;
``freshness``  the age of the newest quote against the collector's cadence;
``coverage``   delivered slots per series over the last window, finished slots
               only — the slot that started a moment ago has not been delivered
               yet, and calling it missing would make every healthy window look
               98.4 %;
``gaps``       the runs of missing slots in that window;
``journal``    the collector's own chain: does it verify, and when did it last
               write;
``readiness``  can the cycle derive a snapshot, and how many aligned bars does
               the coherence window still need;
``truth``      what the lie detector says about the newest snapshot;
``disk``       how much the store occupies and how much room is left.

Doctor reads and never writes: no journal line, no state, no lock.  A
diagnostic that changes what it inspects cannot be run twice, and this one is
meant to be run while the collector appends and the cycle decides.

Four words, and they are the whole vocabulary
=============================================

``ok``    the reading is what a healthy collector produces;
``wait``  the store is younger than the coherence window.  A calendar, not a
          fault — the same fact the cycle reports as exit code ``3`` — and the
          detail says how many minutes are left instead of making the operator
          compute it.  It stays green here too;
``warn``  a fact for the daily check, not for 3 a.m.: a rate-limited venue at
          96 % coverage, a truth finding, a capture chain nobody has written
          yet on a machine that has not started collecting;
``fail``  today's data is not a claim about the market: no quotes at all, a
          collector that stopped, a chain that does not verify, a full disk.

The exit code follows the data CLI it lives in: ``0`` when the verdict is
``ok`` or ``wait``, ``1`` when anything is ``warn`` or ``fail`` (a cron line
that ignores stdout still learns something), ``2`` when the store cannot be
read at all and the question could not be asked.
"""
from __future__ import annotations

import datetime as dt
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from monster.brain.truth import cross_venue_truth
from monster.data.derive import DataError, derive
from monster.data.quality import coverage, find_gaps
from monster.data.store import Store, StoreError
from monster.journal import Journal, JournalError

__all__ = [
    "STATUSES",
    "Check",
    "Diagnosis",
    "diagnose",
    "DEFAULT_FLOOR",
    "DEFAULT_MAX_AGE_S",
    "DEFAULT_WINDOW_MIN",
    "DEFAULT_MIN_FREE_GB",
]

#: The whole vocabulary, ordered from quiet to loud.  ``wait`` is second, not
#: last: it is greener than ``warn`` and louder than ``ok`` only because it has
#: a number to report.
STATUSES: Tuple[str, ...] = ("ok", "wait", "warn", "fail")
_SEVERITY = {status: rank for rank, status in enumerate(STATUSES)}

#: A series below this share of its slots is worth a human's eyes at the daily
#: check.  The same floor ``quality.Coverage.ok`` and the Phase 1 gate use.
DEFAULT_FLOOR = 0.98

#: How old the newest quote may be before the collector is behind.  The VPS
#: captures every 60 s, so three silent passes is a fault and one is not.
DEFAULT_MAX_AGE_S = 180.0

#: The window coverage and gaps are judged over.
DEFAULT_WINDOW_MIN = 60.0

#: Free space below which a store that grows all week is at risk.
DEFAULT_MIN_FREE_GB = 1.0


def _stamp(ts: float) -> str:
    return dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Check:
    """One reading, its verdict, and — when it is not ``ok`` — what to do."""

    name: str
    status: str
    detail: str
    hint: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in _SEVERITY:
            raise ValueError(f"unknown status {self.status!r}; expected one of {', '.join(STATUSES)}")

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "wait")

    def describe(self, *, width: int = 0) -> str:
        line = f"[{self.status:<4}] {self.name:<{width}} {self.detail}"
        if self.hint:
            line += f"\n{'':<{width + 8}}hint: {self.hint}"
        return line

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "hint": self.hint}


@dataclass(frozen=True)
class Diagnosis:
    """Every reading taken at one instant, and the worst of them."""

    ts: float
    root: str
    checks: Tuple[Check, ...] = ()

    @property
    def verdict(self) -> str:
        worst = "ok"
        for check in self.checks:
            if _SEVERITY[check.status] > _SEVERITY[worst]:
                worst = check.status
        return worst

    @property
    def ok(self) -> bool:
        """``wait`` is not an error: a young store is a calendar, not a fault."""
        return self.verdict in ("ok", "wait")

    def counts(self) -> Dict[str, int]:
        counts = {status: 0 for status in STATUSES}
        for check in self.checks:
            counts[check.status] += 1
        return {status: count for status, count in counts.items() if count}

    def check(self, name: str) -> Optional[Check]:
        for check in self.checks:
            if check.name == name:
                return check
        return None

    def _next(self) -> str:
        verdict = self.verdict
        if verdict == "fail":
            return (
                "fix the [fail] line(s) first: until then today's store is not a claim about "
                "the market, and the cycle will refuse for the right reason"
            )
        if verdict == "warn":
            return (
                "nothing is broken; the [warn] line(s) are what to look at in the daily check "
                "(`docs/VPS.md` §5)"
            )
        if verdict == "wait":
            readiness = self.check("readiness")
            return (
                "the store is young — the cycle answers 3 (wait) and the timer stays green: "
                "a calendar, not a fault"
                + (f" ({readiness.detail})" if readiness is not None else "")
            )
        return "nothing to do: the collector delivers, the chain verifies, the cycle can decide"

    def describe(self) -> str:
        counts = ", ".join(f"{count} {status}" for status, count in self.counts().items())
        width = max((len(check.name) for check in self.checks), default=0)
        lines = [f"doctor {self.root} at {_stamp(self.ts)}: {counts or 'no checks'}"]
        lines.extend(check.describe(width=width) for check in self.checks)
        lines.append(f"[next] {self._next()}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "at": _stamp(self.ts),
            "root": self.root,
            "verdict": self.verdict,
            "ok": self.ok,
            "counts": self.counts(),
            "checks": [check.to_dict() for check in self.checks],
        }

def _store_check(store: Store, days: Sequence[str]) -> Check:
    root = str(store.root)
    if not days:
        return Check(
            "store",
            "fail",
            f"{root}: no day files at all",
            hint="nothing has been captured here yet: `python -m monster.data capture --live --store var/data`",
        )
    venues = store.venues(days[-1])
    counts = store.stats(days[-1])
    raw, quote, funding = counts.get("raw", 0), counts.get("quote", 0), counts.get("funding", 0)
    span = days[0] if len(days) == 1 else f"{days[0]}..{days[-1]}"
    detail = (
        f"{root}: {len(days)} day(s) {span}, {len(venues)} venue(s) ({', '.join(venues) or 'none'}); "
        f"{days[-1]}: {raw} raw, {quote} quote, {funding} funding"
    )
    if quote == 0:
        return Check(
            "store",
            "fail",
            detail,
            hint=(
                "raw bodies without a parsed quote mean the parser refused every answer: "
                "`monster.data probe --live` names the shape"
            ),
        )
    return Check("store", "ok", detail)


def _freshness_check(newest: Optional[float], ts: float, max_age_s: float) -> Check:
    if newest is None:
        return Check(
            "freshness",
            "fail",
            "the store holds no quote at all",
            hint="`systemctl status monster-capture`: a cycle on this store would answer 3, not trade",
        )
    age = ts - newest
    if age < -1.0:
        return Check(
            "freshness",
            "fail",
            f"the newest quote is stamped {-age:.1f}s in the future (clock skew)",
            hint="fix the clock: `time-sync.target` is what makes a timestamp mean now",
        )
    if age > max_age_s:
        return Check(
            "freshness",
            "fail",
            f"newest quote {age:.1f}s old (limit {max_age_s:.0f}s): the collector is behind",
            hint="`systemctl status monster-capture`; a decision on this data would be about the past",
        )
    return Check("freshness", "ok", f"newest quote {age:.1f}s old (limit {max_age_s:.0f}s)")

def _coverage_check(
    store: Store,
    quotes: Sequence[Any],
    *,
    low: float,
    high: float,
    interval_s: float,
    floor: float,
    days: Sequence[str],
) -> Check:
    window = f"{(high - low) / 60.0:.0f} min at {interval_s:g}s"
    if not quotes:
        return Check(
            "coverage",
            "fail",
            f"no quote in the last {window}",
            hint="`systemctl status monster-capture`; freshness says how long the silence is",
        )
    series = coverage(quotes, interval_s, since=low, until=high)
    if not series:
        return Check("coverage", "fail", f"no series inside the last {window}")
    thin = [item for item in series if not item.ok(floor)]
    worst = min(series, key=lambda item: item.fraction)
    detail = (
        f"last {window}: {len(series)} series, worst {worst.venue} {worst.symbol} ({worst.kind}) "
        f"{worst.present}/{worst.expected} ({worst.fraction:.1%})"
    )
    known = set(store.venues(days[-1])) if days else set()
    silent = sorted(known - {item.venue for item in series})
    if silent:
        detail += f"; silent venue(s): {', '.join(silent)}"
    if thin:
        return Check(
            "coverage",
            "warn",
            f"{detail} — {len(thin)} of {len(series)} series below {floor:.0%}",
            hint=(
                "a rate-limited venue is a gap to record, not a price to interpolate: "
                "the store keeps the hole, and `monster.data gaps` names it"
            ),
        )
    if silent:
        return Check(
            "coverage",
            "warn",
            detail,
            hint=(
                "a venue that delivered nothing in the window is rate-limited or broken: "
                "`monster.data probe --live` asks it one question"
            ),
        )
    return Check("coverage", "ok", detail)


def _gaps_check(quotes: Sequence[Any], *, low: float, high: float, interval_s: float) -> Check:
    gaps = find_gaps(quotes, interval_s, since=low, until=high)
    if not gaps:
        return Check("gaps", "ok", f"none at {interval_s:g}s in the last {(high - low) / 60.0:.0f} min")
    worst = max(gaps, key=lambda gap: gap.missing)
    detail = (
        f"{len(gaps)} gap(s) in the last {(high - low) / 60.0:.0f} min; longest {worst.missing} "
        f"slot(s) {worst.venue} {worst.symbol} ({worst.kind})"
    )
    return Check(
        "gaps",
        "warn",
        detail,
        hint="a gap is recorded, never filled: `monster.data gaps --store var/data --interval 60` lists every one",
    )


def _journal_check(path: Path, *, ts: float, max_age_s: float) -> Check:
    target = Path(path).expanduser()
    if not target.is_file():
        return Check(
            "journal",
            "warn",
            f"{target}: no capture chain",
            hint=(
                "the collector writes its own chain with `--journal var/journal.capture.jsonl`; "
                "without it the cycle has nothing to link and `--verify` has no links to check"
            ),
        )
    try:
        journal = Journal(target, fsync=False)
        verdict = journal.verify()
        count = journal.count()
        last = journal.last()
    except (JournalError, OSError) as error:
        return Check(
            "journal",
            "fail",
            f"{target}: {error}",
            hint=(
                "a chain that cannot be read cannot be linked: "
                "`monster.cycle --verify` names the first line outside it"
            ),
        )
    if verdict != "ok":
        return Check(
            "journal",
            "fail",
            f"{target}: {verdict} ({count} event(s))",
            hint=(
                "a rewritten past gets no new lines from the cycle: "
                "restore the file from backup or move it aside, and say so"
            ),
        )
    if last is None:
        return Check("journal", "ok", f"{target}: ok, empty")
    age = ts - last.ts
    detail = f"{target}: ok, {count} event(s), last {last.kind} {age:.0f}s ago"
    if age > max_age_s * 2.0:
        return Check(
            "journal",
            "warn",
            detail,
            hint="the chain is intact but nothing has been written to it lately: is the collector still running?",
        )
    return Check("journal", "ok", detail)
def _readiness_check(derivation: Any, *, window: int, interval_s: float) -> Check:
    bars = min((len(series) for series in derivation.history.values()), default=0)
    if bars < window:
        eta_min = (window - bars) * float(interval_s) / 60.0
        return Check(
            "readiness",
            "wait",
            f"{bars}/{window} aligned bar(s) at {interval_s:g}s: "
            f"the cycle answers 3 (wait) for ~{eta_min:.0f} more min",
            hint=(
                "a young store is a calendar, not a fault: the physics reading needs a full "
                "coherence window, and the trade gate is not lowered to fill it"
            ),
        )
    return Check("readiness", "ok", f"{bars}/{window} aligned bar(s) at {interval_s:g}s: the cycle can decide")


def _truth_check(derivation: Any) -> Check:
    report = cross_venue_truth(derivation.snapshot, now=derivation.at)
    unmeasured = len(report.unmeasured)
    if not report.findings:
        return Check(
            "truth",
            "ok",
            f"{len(report.checked)} checked, 0 flagged | {unmeasured} unmeasured",
        )
    first = report.findings[0]
    more = f" (+{len(report.findings) - 1} more)" if len(report.findings) > 1 else ""
    return Check(
        "truth",
        "warn",
        f"{len(report.checked)} checked, {len(report.findings)} flagged: "
        f"{first.venue} {first.symbol} {first.reason}{more} | {unmeasured} unmeasured",
        hint=(
            "a finding is a veto, not a trade: measure how often it fires with "
            "`monster.data truth-report --max-fp-share 0.01` before moving the threshold"
        ),
    )


def _disk_check(root: Path, *, min_free_gb: float) -> Check:
    try:
        usage = shutil.disk_usage(root)
        stored = sum(path.stat().st_size for path in Path(root).rglob("*.jsonl.gz"))
    except OSError as error:
        return Check("disk", "warn", f"{root}: {error}", hint="the store is not readable as a directory")
    free_gb = usage.free / float(1 << 30)
    detail = f"{root}: {stored / 1e6:.1f} MB stored, {free_gb:.1f} GiB free (floor {min_free_gb:.1f})"
    if free_gb < min_free_gb:
        return Check(
            "disk",
            "fail",
            detail,
            hint=(
                "a full disk turns a capture into a traceback in the middle of a pass: "
                "prune old days or grow the volume"
            ),
        )
    return Check("disk", "ok", detail)


def diagnose(
    store: Store,
    *,
    now: Optional[float] = None,
    interval_s: float = 60.0,
    window_min: float = DEFAULT_WINDOW_MIN,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    floor: float = DEFAULT_FLOOR,
    coherence_window: int = 128,
    journal_path: Optional[Path] = None,
    min_free_gb: float = DEFAULT_MIN_FREE_GB,
) -> Diagnosis:
    """Take every reading once, at the same instant, and say the worst of them.

    ``wait`` is not a defect: a store younger than the coherence window is the
    same fact the cycle reports as exit code 3, and the readiness line says how
    many minutes are left instead of leaving the operator to divide.

    Coverage and gaps are judged over **finished** slots: the slot that started
    a moment ago has not been delivered yet, and counting it as missing would
    make every healthy window look like 98.4 % — an alarm that fires on a
    working collector is an alarm nobody reads.
    """
    if interval_s <= 0.0:
        raise ValueError("interval_s must be positive")
    if window_min <= 0.0:
        raise ValueError("window_min must be positive")
    if coherence_window <= 0:
        raise ValueError("coherence_window must be positive")

    ts = float(time.time() if now is None else now)
    days = store.days()
    checks: List[Check] = [_store_check(store, days)]

    newest = store.latest("quote")
    checks.append(_freshness_check(newest, ts, max_age_s))

    low = ts - float(window_min) * 60.0
    high = ts - float(interval_s)
    if high <= low:
        high = ts
    quotes = store.quotes(since=low, until=ts)
    checks.append(
        _coverage_check(
            store, quotes, low=low, high=high, interval_s=interval_s, floor=floor, days=days
        )
    )
    checks.append(_gaps_check(quotes, low=low, high=high, interval_s=interval_s))

    if journal_path is None:
        checks.append(Check("journal", "ok", "not checked: no capture chain was named"))
    else:
        checks.append(_journal_check(Path(journal_path), ts=ts, max_age_s=max_age_s))

    derivation = None
    try:
        derivation = derive(
            store,
            at=ts,
            interval_s=interval_s,
            bars=coherence_window,
            quote_max_age_s=max_age_s,
        )
    except (DataError, StoreError) as error:
        checks.append(
            Check(
                "readiness",
                "fail",
                f"the cycle cannot derive a snapshot: {error}",
                hint=(
                    "freshness and store say which half of the question failed: "
                    "no quotes, or quotes older than the limit"
                ),
            )
        )
    else:
        checks.append(_readiness_check(derivation, window=coherence_window, interval_s=interval_s))

    if derivation is None:
        checks.append(Check("truth", "ok", "not checked: the snapshot could not be derived"))
    else:
        checks.append(_truth_check(derivation))

    checks.append(_disk_check(store.root, min_free_gb=min_free_gb))
    return Diagnosis(ts=ts, root=str(store.root), checks=tuple(checks))

