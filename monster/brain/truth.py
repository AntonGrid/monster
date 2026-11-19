"""The lie detector: a venue that disagrees with the crowd, or a quote that is old.

Why this is the second module and not the tenth
===============================================

Every strategy in this workspace is a claim about prices, and a price is only
as good as the book that printed it.  A stale quote is not a slow market — it
is a market that is not there, and a fill against it is a fill that will be
rejected, or worse, accepted and then repriced.  A venue whose mid sits 400 bps
away from three others is either an arbitrage or a lie, and the monster has no
way to tell which until it can withdraw — so it must assume the lie.

``entrain`` frames this as a medium whose oscillators either agree or do not;
here the oscillators are venues quoting the same symbol, and the reading is
robust rather than parametric:

* the centre is the **median** mid, not the mean, so one liar cannot move it;
* the scale is the **MAD**, not the standard deviation, for the same reason;
* a symbol quoted by fewer than :data:`MIN_VENUES` venues is *unmeasured*, not
  clean — "I could not check" is a different sentence from "I checked";
* a quote older than ``max_age_s`` is flagged whether or not its price agrees,
  because agreement about a price that no longer exists is not agreement.

A finding is not a trade.  It is a veto: the cycle refuses any leg that touches
a flagged venue/symbol, and records the refusal with its reason.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from monster.contracts import Quote, Snapshot

__all__ = ["Finding", "TruthReport", "cross_venue_truth", "MIN_VENUES"]

MIN_VENUES = 3

#: Robust z above which a venue is "not quoting the same asset as the others".
DEFAULT_Z = 4.0

#: How old a quote may be before it is a memory rather than a price.
DEFAULT_MAX_AGE_S = 5.0

#: Fallback when every honest venue prints the same mid and the MAD is zero:
#: a deviation this large is a finding even without a scale to divide by.
DEFAULT_MAX_DEVIATION_BPS = 100.0

#: A deviation below this is not a lie, whatever the robust z says.  With three
#: venues the scale is set by the two that agree most closely, so a venue one
#: basis point away from two venues that agree to a tenth of one scores z ≈ 10:
#: the z measures how alone a venue is, not how far the lie reaches.  The cycle
#: keeps the floor at ``0.0`` until the false-positive instrument has measured a
#: better one — ``truth-report --min-bps 0,1,2,3`` is that measurement.
DEFAULT_MIN_DEVIATION_BPS = 0.0


@dataclass(frozen=True)
class Finding:
    """One reason a venue/symbol must not be traded on this snapshot."""

    symbol: str
    venue: str
    reason: str
    mid: Optional[float]
    deviation_bps: Optional[float]
    z: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "venue": self.venue,
            "reason": self.reason,
            "mid": self.mid,
            "deviation_bps": self.deviation_bps,
            "z": self.z,
        }


@dataclass(frozen=True)
class TruthReport:
    """What could and could not be checked, and what was refused."""

    ts: float
    checked: Tuple[str, ...]
    findings: Tuple[Finding, ...] = ()
    unmeasured: Tuple[str, ...] = ()

    def flagged(self, venue: str, symbol: str) -> Optional[Finding]:
        key = (venue.strip().lower(), symbol.strip().lower())
        for finding in self.findings:
            if (finding.venue, finding.symbol) == key:
                return finding
        return None

    @property
    def clean(self) -> bool:
        return not self.findings

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "checked": list(self.checked),
            "findings": [finding.to_dict() for finding in self.findings],
            "unmeasured": list(self.unmeasured),
            "clean": self.clean,
        }

    def describe(self) -> str:
        if not self.findings and not self.unmeasured:
            return f"truth: {len(self.checked)} symbol(s) agree across venues"
        parts = [f"truth: {len(self.checked)} checked, {len(self.findings)} flagged"]
        if self.unmeasured:
            parts.append(f"{len(self.unmeasured)} unmeasured")
        for finding in self.findings:
            parts.append(f"{finding.venue} {finding.symbol}: {finding.reason}")
        for reason in self.unmeasured:
            parts.append(f"unmeasured: {reason}")
        return " | ".join(parts)


def _quotes_by_symbol(quotes: Iterable[Quote]) -> Dict[str, List[Quote]]:
    grouped: Dict[str, List[Quote]] = {}
    for quote in quotes:
        grouped.setdefault(quote.symbol, []).append(quote)
    return grouped


def cross_venue_truth(
    snapshot: Snapshot,
    *,
    now: Optional[float] = None,
    z_threshold: float = DEFAULT_Z,
    min_venues: int = MIN_VENUES,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    max_deviation_bps: float = DEFAULT_MAX_DEVIATION_BPS,
    min_deviation_bps: float = DEFAULT_MIN_DEVIATION_BPS,
) -> TruthReport:
    """Flag stale quotes and venues that disagree with the robust cross-section.

    The report is per **snapshot**, not per venue: it answers "may I trade this
    symbol now", and the answer is allowed to be "I cannot tell".

    ``min_deviation_bps`` is a floor on how far the venue must be from the median
    before the disagreement is a finding at all: below it, the venues are quoting
    the same asset and the difference is microstructure, however small the MAD is.

    ``min_venues`` below :data:`MIN_VENUES` is refused rather than honoured: with
    two venues the median *is* one of the two mids, so the "disagreement" is
    whichever venue the median did not land on — the check would decide by
    accident and report it as a measurement.  A caller that wants a weaker check
    has to change :data:`MIN_VENUES` in a commit, where it can be seen.
    """
    if min_venues < MIN_VENUES:
        raise ValueError(
            f"min_venues={min_venues}: {min_venues} venue(s) cannot tell a liar from the "
            f"truth, because the median of two mids is one of them; the floor is "
            f"MIN_VENUES={MIN_VENUES}"
        )
    stamp = float(snapshot.ts if now is None else now)
    findings: List[Finding] = []
    unmeasured: List[str] = []
    checked: List[str] = []

    for symbol, quotes in sorted(_quotes_by_symbol(snapshot.quotes).items()):
        mids = {quote.venue: quote.mid for quote in quotes}
        for quote in quotes:
            age = stamp - quote.ts
            if age < -1.0:
                findings.append(
                    Finding(
                        symbol,
                        quote.venue,
                        f"stamped {-age:.1f}s in the future (clock skew)",
                        quote.mid,
                        None,
                        None,
                    )
                )
            elif age > max_age_s:
                findings.append(
                    Finding(
                        symbol,
                        quote.venue,
                        f"stale: {age:.1f}s old, limit {max_age_s:.1f}s",
                        quote.mid,
                        None,
                        None,
                    )
                )

        venues = sorted(mids)
        if len(venues) < min_venues:
            unmeasured.append(
                f"{symbol}: {len(venues)} venue(s) < {min_venues}, so disagreement cannot be told from noise"
            )
            continue
        checked.append(symbol)

        centre = statistics.median(mids.values())
        deviations = {venue: mid - centre for venue, mid in mids.items()}
        mad = statistics.median(abs(value) for value in deviations.values())
        scale = 1.4826 * mad
        for venue, deviation in deviations.items():
            deviation_bps = 0.0 if centre == 0.0 else 1e4 * deviation / centre
            # Silence, not a finding: below the floor the venues are quoting the
            # same asset and the gap is microstructure, however small the MAD.
            if abs(deviation_bps) < min_deviation_bps:
                continue
            z = None if scale == 0.0 else deviation / scale
            if z is not None and abs(z) > z_threshold:
                findings.append(
                    Finding(
                        symbol,
                        venue,
                        f"mid deviates {deviation_bps:+.1f} bps from the median "
                        f"(robust z {z:+.1f} > {z_threshold:.1f})",
                        mids[venue],
                        deviation_bps,
                        z,
                    )
                )
            elif z is None and abs(deviation_bps) > max_deviation_bps:
                findings.append(
                    Finding(
                        symbol,
                        venue,
                        f"mid deviates {deviation_bps:+.1f} bps while the others agree exactly "
                        f"(no scale; limit {max_deviation_bps:.1f} bps)",
                        mids[venue],
                        deviation_bps,
                        None,
                    )
                )

    return TruthReport(ts=stamp, checked=tuple(checked), findings=tuple(findings), unmeasured=tuple(unmeasured))

