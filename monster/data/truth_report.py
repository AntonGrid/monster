"""Did the truth check fire at the right moments?  Replay a day and classify it.

Why a finding is not yet a mistake
==================================
``cross_venue_truth`` answers "may I trade this symbol now", and it is built to
be wrong in one direction only: it would rather refuse a real dislocation than
accept a bad print.  That bias is correct at decision time and useless as a
measurement, because a refusal caused by a venue lying and a refusal caused by a
venue printing one bad price look identical in the journal — both are a line
with a z-score.  The Phase 1 gate asks for a *false-positive* rate, so somebody
has to say which findings were noise, and that cannot be done at the moment of
the refusal: it is only knowable from what happened next.

So the day is replayed pass by pass, and every disagreement is classified by the
pass that follows it:

* **persistent** — the same venue is still off the cross-section at the next
  pass.  A dislocation that survives a minute is either real or a dying feed;
  either way, refusing was right.
* **others_followed** — the flagged venue is back inside the crowd *because the
  crowd moved to it*.  The price was the new price, printed early: refusing cost
  an opportunity, not correctness, and calling that a false positive would
  punish the detector for being first.
* **snapped back** — the venue is back inside the crowd and the crowd did not
  move.  Nothing happened, so the finding was noise.  This is the only bucket
  that counts against the detector, and the gate wants it under 1%.
* **ambiguous** — neither sentence fits (the crowd drifted, the venue partly
  returned).  Counted and named, never quietly folded into either side.
* **unknown** — the day ended, or the venue was absent from the next pass.  An
  unjudgeable finding is reported as unjudgeable; a denominator is not inflated
  by pretending it was noise.  Stale and clock-skew findings land here as well,
  carrying their reason: they are statements about the collector, not about
  disagreement between venues, and they are never scored.

Two numbers come out, and they answer different questions
========================================================
``false_positive_share`` — of the findings we could judge, how many were noise.
This is "how often is the detector wrong when it speaks".

``false_positives_per_section`` — how many noise findings per cross-section
judged.  This is "how much of the day does the detector spoil", and it is the
number to compare with a gate written as a rate over time.

Reporting only one of them is how a detector gets tuned into silence: a rule
that never fires has a perfect share and a meaningless day.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

from monster.brain.truth import (
    DEFAULT_MAX_AGE_S,
    DEFAULT_MAX_DEVIATION_BPS,
    DEFAULT_Z,
    MIN_VENUES,
    TruthReport,
    cross_venue_truth,
)
from monster.contracts import Quote, Snapshot
from monster.data.derive import DataError
from monster.data.store import Store, day_start

__all__ = ["TruthEvent", "TruthDay", "truth_day", "VERDICTS", "FP_VERDICT"]

#: Every classification a finding can end up in.
VERDICTS = ("persistent", "others_followed", "snapped_back", "ambiguous", "unknown")

#: The one bucket that counts as a false positive: the venue returned to the
#: crowd and the crowd had not moved, so nothing had happened.
FP_VERDICT = "snapped_back"

#: A return to within this fraction of the original deviation is a return.
SNAP_FRACTION = 0.3

#: How far the other venues must travel toward the flagged price to call it
#: "the crowd followed": half the deviation, in the venue's direction.
FOLLOW_FRACTION = 0.5

#: A return to within one basis point is a return even when the deviation was
#: small; below this, price noise would masquerade as a snap-back.
SNAP_FLOOR_BPS = 1.0



@dataclass(frozen=True)
class TruthEvent:
    """One finding, and the pass that judged it."""

    ts: float
    symbol: str
    venue: str
    deviation_bps: Optional[float]
    z: Optional[float]
    verdict: str
    detail: str = ""

    @property
    def false_positive(self) -> bool:
        return self.verdict == FP_VERDICT

    def describe(self) -> str:
        edge = "" if self.deviation_bps is None else f" ({self.deviation_bps:+.1f} bps)"
        head = f"{self.symbol} {self.venue} at {self.ts:.0f}{edge}: {self.verdict.replace('_', ' ')}"
        return head + (f" — {self.detail}" if self.detail else "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "venue": self.venue,
            "deviation_bps": self.deviation_bps,
            "z": self.z,
            "verdict": self.verdict,
            "detail": self.detail,
            "false_positive": self.false_positive,
        }


@dataclass(frozen=True)
class TruthDay:
    """A day of truth findings, classified, with the rates that follow."""

    day: str
    passes: int
    sections: int
    unmeasured: Tuple[str, ...] = ()
    events: Tuple[TruthEvent, ...] = ()

    def counts(self) -> Dict[str, int]:
        found = {verdict: 0 for verdict in VERDICTS}
        for event in self.events:
            found[event.verdict] = found.get(event.verdict, 0) + 1
        return found

    @property
    def judged(self) -> int:
        """Findings that could be classified — the honest denominator."""
        return sum(1 for event in self.events if event.verdict != "unknown")

    @property
    def false_positives(self) -> int:
        return sum(1 for event in self.events if event.false_positive)

    @property
    def false_positive_share(self) -> Optional[float]:
        """Noise as a fraction of the findings that could be judged."""
        return None if self.judged == 0 else self.false_positives / self.judged

    @property
    def false_positives_per_section(self) -> Optional[float]:
        """Noise findings per cross-section judged — the gate's rate over time."""
        return None if self.sections == 0 else self.false_positives / self.sections

    def describe(self, *, limit: int = 10) -> str:
        head = f"truth {self.day}: {self.passes} pass(es), {self.sections} cross-section(s) judged"
        if self.unmeasured:
            head += f", {len(self.unmeasured)} symbol(s) unmeasured"
        if not self.events:
            return head + " · no finding: nothing to explain"
        counts = self.counts()
        spoken = ", ".join(
            f"{counts[verdict]} {verdict.replace('_', ' ')}" for verdict in VERDICTS if counts[verdict]
        )
        lines = [f"{head} · {len(self.events)} finding(s): {spoken}"]
        share = self.false_positive_share
        per_section = self.false_positives_per_section
        if share is not None:
            rate = "" if per_section is None else f", {per_section:.2e} per cross-section"
            tail = " (gate: under 1%)" if share >= 0.01 else " (inside the gate)"
            lines.append(f"false positives: {self.false_positives}/{self.judged} judged = {share:.1%}{rate}{tail}")
        for event in self.events[:limit]:
            lines.append(f"  {event.describe()}")
        if len(self.events) > limit:
            lines.append(f"  … {len(self.events) - limit} more (use --json)")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "day": self.day,
            "passes": self.passes,
            "sections": self.sections,
            "unmeasured": list(self.unmeasured),
            "events": [event.to_dict() for event in self.events],
            "counts": self.counts(),
            "judged": self.judged,
            "false_positives": self.false_positives,
            "false_positive_share": self.false_positive_share,
            "false_positives_per_section": self.false_positives_per_section,
        }


@dataclass(frozen=True)
class _Pass:
    """One moment: the quotes that arrived together, and what the check said."""

    ts: float
    mids: Mapping[str, Mapping[str, float]]
    report: TruthReport
    flagged: FrozenSet[Tuple[str, str]]


def _deviation_bps(mid: float, centre: float) -> float:
    return 0.0 if centre == 0.0 else 1e4 * (mid - centre) / centre


def _passes(
    quotes: Tuple[Quote, ...],
    *,
    min_venues: int,
    z_threshold: float,
    max_deviation_bps: float,
    max_age_s: float,
) -> List[_Pass]:
    """Group the day by timestamp: one capture pass is one moment.

    Capture stamps every reading of a pass with the same receive time, so the
    timestamp *is* the snapshot boundary.  A store written by something else
    still groups correctly as long as its rows carry a pass stamp; rows carrying
    per-request stamps make every venue its own section, and the report then says
    ``unmeasured`` rather than inventing a cross-section out of nothing.
    """
    grouped: Dict[float, List[Quote]] = {}
    for quote in quotes:
        grouped.setdefault(float(quote.ts), []).append(quote)
    passes: List[_Pass] = []
    for ts in sorted(grouped):
        items = tuple(sorted(grouped[ts], key=lambda item: (item.symbol, item.venue)))
        snapshot = Snapshot(ts=ts, quotes=items)
        report = cross_venue_truth(
            snapshot,
            now=ts,
            z_threshold=z_threshold,
            min_venues=min_venues,
            max_age_s=max_age_s,
            max_deviation_bps=max_deviation_bps,
        )
        mids: Dict[str, Dict[str, float]] = {}
        for quote in items:
            mids.setdefault(quote.symbol, {})[quote.venue] = quote.mid
        flagged = frozenset(
            (finding.symbol, finding.venue)
            for finding in report.findings
            if finding.deviation_bps is not None
        )
        passes.append(_Pass(ts=ts, mids=mids, report=report, flagged=flagged))
    return passes


def _next_judgeable(passes: List[_Pass], index: int, symbol: str, venue: str, min_venues: int) -> Optional[int]:
    """The next pass in which this (symbol, venue) can be compared with a crowd."""
    for following in range(index + 1, len(passes)):
        mids = passes[following].mids.get(symbol, {})
        if venue in mids and len(mids) >= min_venues:
            return following
    return None



def _judge(
    now: _Pass, later: _Pass, symbol: str, venue: str, deviation_now: float, still_flagged: bool
) -> Tuple[str, str]:
    """Say what the next pass made of this finding."""
    if still_flagged:
        return "persistent", f"still flagged at {later.ts:.0f}"
    mids_later = later.mids[symbol]
    centre_later = statistics.median(mids_later.values())
    deviation_later = _deviation_bps(mids_later[venue], centre_later)
    shifts = [
        _deviation_bps(mids_later[other], mid_now)
        for other, mid_now in now.mids.get(symbol, {}).items()
        if other != venue and other in mids_later
    ]
    crowd = statistics.median(shifts) if shifts else 0.0
    returned = abs(deviation_later) <= max(SNAP_FLOOR_BPS, SNAP_FRACTION * abs(deviation_now))
    if returned and abs(crowd) < FOLLOW_FRACTION * abs(deviation_now):
        return "snapped_back", f"back to {deviation_later:+.1f} bps while the crowd moved {crowd:+.1f} bps"
    if abs(crowd) >= FOLLOW_FRACTION * abs(deviation_now) and crowd * deviation_now > 0.0:
        return "others_followed", f"the crowd moved {crowd:+.1f} bps toward it: it was early, not wrong"
    return "ambiguous", f"venue {deviation_later:+.1f} bps, crowd {crowd:+.1f} bps: neither sentence fits"


def truth_day(
    store: Store,
    day: Optional[str] = None,
    *,
    min_venues: int = MIN_VENUES,
    z_threshold: float = DEFAULT_Z,
    max_deviation_bps: float = DEFAULT_MAX_DEVIATION_BPS,
    max_age_s: float = DEFAULT_MAX_AGE_S,
) -> TruthDay:
    """Replay one day of the truth check and classify every finding it made."""
    days = store.days()
    target = day or (days[-1] if days else "")
    if not target:
        raise DataError(f"{store.root} has no day to replay")
    start = day_start(target)
    quotes = store.quotes(since=start, until=start + 86_400.0)
    if not quotes:
        raise DataError(f"{store.root} holds no quote for {target}")

    passes = _passes(
        quotes,
        min_venues=min_venues,
        z_threshold=z_threshold,
        max_deviation_bps=max_deviation_bps,
        max_age_s=max_age_s,
    )
    sections = sum(len(item.report.checked) for item in passes)
    unmeasured = sorted(
        {
            symbol
            for item in passes
            for symbol, mids in item.mids.items()
            if len(mids) < min_venues
        }
    )

    events: List[TruthEvent] = []
    for index, item in enumerate(passes):
        for finding in item.report.findings:
            if finding.deviation_bps is None:
                # Staleness is a statement about the collector or the clock, not
                # a disagreement between venues: reported, never scored.
                events.append(
                    TruthEvent(
                        ts=item.ts,
                        symbol=finding.symbol,
                        venue=finding.venue,
                        deviation_bps=None,
                        z=None,
                        verdict="unknown",
                        detail=finding.reason,
                    )
                )
                continue
            following = _next_judgeable(passes, index, finding.symbol, finding.venue, min_venues)
            if following is None:
                verdict, detail = "unknown", "the next pass cannot judge it (venue absent, or the day ended)"
            else:
                verdict, detail = _judge(
                    item,
                    passes[following],
                    finding.symbol,
                    finding.venue,
                    float(finding.deviation_bps),
                    (finding.symbol, finding.venue) in passes[following].flagged,
                )
            events.append(
                TruthEvent(
                    ts=item.ts,
                    symbol=finding.symbol,
                    venue=finding.venue,
                    deviation_bps=finding.deviation_bps,
                    z=finding.z,
                    verdict=verdict,
                    detail=detail,
                )
            )

    return TruthDay(
        day=target,
        passes=len(passes),
        sections=sections,
        unmeasured=tuple(unmeasured),
        events=tuple(events),
    )

