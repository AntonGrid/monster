"""Is the data any good? Gaps, coverage, and whether the store can be re-derived.

Three questions, three answers
==============================

**Gaps.**  A capture every 60 s that misses forty minutes has not produced a
gap of one bar; it produced forty missing bars, and every study that treats the
remaining rows as a regular series will silently compress time.  ``find_gaps``
compares consecutive stamps with the *expected* interval and reports the number
of slots that never arrived — never a zero-filled price, which would be a price
the venue never quoted.

**Coverage.**  The fraction of expected slots a (venue, symbol) actually
delivered in a window.  The Phase 1 gate is ``coverage >= 0.98`` per day; a
number below it is a sentence about the collector, not about the market.

**Audit.**  The reason raw bodies are kept: re-parse yesterday's payloads with
today's parser, compare with yesterday's normalized rows, and report the keys
that exist on one side only or disagree.  A store that cannot reproduce its own
past is a store whose backtests cannot be trusted — and the audit says *which
key* broke rather than printing a checksum and a shrug.

Comparison is exact for values that were written from the same parse, with a
relative tolerance of ``1e-12`` for the float round-trip through JSON.  Two
transcriptions of one number should be equal to the last bit; if they are not,
something rewrote a row.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from monster.contracts import FundingQuote, Quote
from monster.data.parsers import funding_from, quote_from
from monster.data.raw import RawPayload, ShapeError
from monster.data.store import Store, day_start
from monster.instruments import kind_of

__all__ = [
    "Gap",
    "Coverage",
    "Audit",
    "find_gaps",
    "coverage",
    "audit_day",
    "unparsable",
    "DEFAULT_TOLERANCE",
]

#: How far past the expected interval a stamp may sit before it is a hole
#: rather than jitter.  A venue that answers at 61 s is not a missing minute.
DEFAULT_TOLERANCE = 1.5


@dataclass(frozen=True)
class Gap:
    """Missing slots between two stamps that did arrive."""

    venue: str
    symbol: str
    kind: str
    start_ts: float
    end_ts: float
    missing: int
    interval_s: float

    def describe(self) -> str:
        return (
            f"{self.venue} {self.symbol} ({self.kind}): {self.missing} missing "
            f"{self.interval_s:g}s slot(s) between {self.start_ts:.0f} and {self.end_ts:.0f}"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "kind": self.kind,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "missing": self.missing,
            "interval_s": self.interval_s,
        }


@dataclass(frozen=True)
class Coverage:
    """How much of a window one (venue, symbol) actually delivered."""

    venue: str
    symbol: str
    kind: str
    first_ts: float
    last_ts: float
    present: int
    expected: int
    interval_s: float

    @property
    def fraction(self) -> float:
        return 0.0 if self.expected <= 0 else min(1.0, self.present / self.expected)

    def ok(self, floor: float = 0.98) -> bool:
        return self.expected > 0 and self.fraction >= floor

    def to_dict(self) -> Dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "kind": self.kind,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "present": self.present,
            "expected": self.expected,
            "interval_s": self.interval_s,
            "fraction": self.fraction,
        }


def _group(items: Iterable[Any]) -> Dict[Tuple[str, str, str], List[Any]]:
    grouped: Dict[Tuple[str, str, str], List[Any]] = {}
    for item in items:
        if isinstance(item, FundingQuote):
            kind = "funding"
        elif isinstance(item, Quote):
            kind = kind_of(item.symbol) or "unknown"
        else:
            kind = "?"
        grouped.setdefault((item.venue, item.symbol, kind), []).append(item)
    return grouped


def find_gaps(
    items: Sequence[Any],
    interval_s: float,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    since: Optional[float] = None,
    until: Optional[float] = None,
) -> Tuple[Gap, ...]:
    """Every run of missing slots, per (venue, symbol, kind)."""
    if interval_s <= 0.0:
        raise ValueError("interval_s must be positive")
    gaps: List[Gap] = []
    for (venue, symbol, kind), group in sorted(_group(items).items()):
        stamps = sorted({float(item.ts) for item in group})
        if since is not None:
            stamps = [stamp for stamp in stamps if stamp >= since]
        if until is not None:
            stamps = [stamp for stamp in stamps if stamp <= until]
        for left, right in zip(stamps, stamps[1:]):
            delta = right - left
            if delta <= interval_s * tolerance:
                continue
            missing = int(round(delta / interval_s)) - 1
            if missing >= 1:
                gaps.append(
                    Gap(
                        venue=venue,
                        symbol=symbol,
                        kind=kind,
                        start_ts=left,
                        end_ts=right,
                        missing=missing,
                        interval_s=float(interval_s),
                    )
                )
    return tuple(gaps)


def coverage(
    items: Sequence[Any],
    interval_s: float,
    *,
    since: Optional[float] = None,
    until: Optional[float] = None,
) -> Tuple[Coverage, ...]:
    """Delivered slots over expected slots, per (venue, symbol, kind)."""
    if interval_s <= 0.0:
        raise ValueError("interval_s must be positive")
    result: List[Coverage] = []
    for (venue, symbol, kind), group in sorted(_group(items).items()):
        stamps = sorted({float(item.ts) for item in group})
        if not stamps:
            continue
        low = float(since) if since is not None else stamps[0]
        high = float(until) if until is not None else stamps[-1]
        if high < low:
            continue
        inside = [stamp for stamp in stamps if low <= stamp <= high]
        expected = int(math.floor((high - low) / interval_s + 1e-9)) + 1
        result.append(
            Coverage(
                venue=venue,
                symbol=symbol,
                kind=kind,
                first_ts=stamps[0],
                last_ts=stamps[-1],
                present=len(inside),
                expected=expected,
                interval_s=float(interval_s),
            )
        )
    return tuple(result)


# ── audit ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Audit:
    """Whether a day's normalized rows can be re-derived from its raw bodies."""

    day: str
    raw: int
    parsed: int
    matched: int
    skipped: Tuple[str, ...] = ()
    only_raw: Tuple[str, ...] = ()
    only_normalized: Tuple[str, ...] = ()
    mismatched: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not (self.only_raw or self.only_normalized or self.mismatched)

    def describe(self) -> str:
        head = (
            f"audit {self.day}: {self.matched}/{len(self.only_raw) + self.matched + len(self.only_normalized)} "
            f"keys agree ({self.raw} raw bodies, {self.parsed} parsed rows)"
        )
        if self.ok:
            return head + (" · " + f"{len(self.skipped)} unparsable raw body(ies) skipped" if self.skipped else "")
        parts = [head]
        for title, entries in (
            ("only in raw", self.only_raw),
            ("only normalized", self.only_normalized),
            ("mismatched", self.mismatched),
        ):
            if entries:
                parts.append(f"{title}: " + "; ".join(entries))
        return " | ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "day": self.day,
            "raw": self.raw,
            "parsed": self.parsed,
            "matched": self.matched,
            "ok": self.ok,
            "skipped": list(self.skipped),
            "only_raw": list(self.only_raw),
            "only_normalized": list(self.only_normalized),
            "mismatched": list(self.mismatched),
        }


def _reparse(payload: RawPayload) -> List[Tuple[Tuple[str, str, str, float], Tuple[float, ...]]]:
    """One raw body → the normalized rows it should have produced."""
    if payload.kind == "funding":
        rate = funding_from(
            venue=payload.venue,
            payload=payload.body,
            symbol=payload.symbol,
            native=payload.native,
            period_hours=payload.period_hours,
            received_ts=payload.received_ts,
        )
        return [((rate.venue, rate.symbol, "funding", rate.ts), (rate.rate, rate.period_hours))]
    quote = quote_from(
        venue=payload.venue, payload=payload.body, symbol=payload.symbol, native=payload.native, kind=payload.kind
    )
    return [((quote.venue, quote.symbol, payload.kind, quote.ts), (quote.bid, quote.ask))]


def _equal(left: Set[Tuple[float, ...]], right: Set[Tuple[float, ...]]) -> bool:
    if len(left) != len(right):
        return False
    for a in left:
        if not any(
            len(a) == len(b) and all(math.isclose(x, y, rel_tol=1e-12, abs_tol=1e-12) for x, y in zip(a, b))
            for b in right
        ):
            return False
    return True


def _show(key: Tuple[str, str, str, float]) -> str:
    venue, symbol, kind, ts = key
    return f"{venue} {symbol} {kind} @ {ts:.3f}"


def audit_day(store: Store, day: str) -> Audit:
    """Re-parse the day's raw bodies and compare with the normalized rows.

    Raw bodies from the previous day are read as well, because a capture at
    23:59:50 can normalise an instrument stamped 00:00:05 — the boundary is a
    property of clocks, not of the collector.  Parsed rows are attributed to the
    day their *own* timestamp falls in, which is the day the store filed them
    under.
    """
    start = day_start(day)
    end = start + 86_400.0
    raw_values: Dict[Tuple[str, str, str, float], Set[Tuple[float, ...]]] = {}
    raw_count, parsed, skipped = 0, 0, []
    for record in store.records(kind="raw", since=start - 86_400.0, until=end):
        payload = RawPayload.from_dict({name: value for name, value in record.items() if name != "record"})
        in_day = start <= payload.received_ts < end
        if in_day:
            raw_count += 1
        if not payload.parsable:
            if in_day:
                skipped.append(f"{payload.venue}/{payload.endpoint}: no request context, cannot re-parse")
            continue
        try:
            rows = _reparse(payload)
        except ShapeError as error:
            if in_day:
                skipped.append(f"{payload.venue}/{payload.endpoint}: {error}")
            continue
        for key, value in rows:
            if start <= key[3] < end:
                raw_values.setdefault(key, set()).add(value)
                parsed += 1

    normalized: Dict[Tuple[str, str, str, float], Set[Tuple[float, ...]]] = {}
    for quote in store.read_quotes(since=start, until=end).items:
        if start <= quote.ts < end:
            key = (quote.venue, quote.symbol, kind_of(quote.symbol) or "unknown", quote.ts)
            normalized.setdefault(key, set()).add((quote.bid, quote.ask))
    for rate in store.read_fundings(since=start, until=end).items:
        if start <= rate.ts < end:
            key = (rate.venue, rate.symbol, "funding", rate.ts)
            normalized.setdefault(key, set()).add((rate.rate, rate.period_hours))

    matched, only_raw, only_normalized, mismatched = 0, [], [], []
    for key in sorted(set(raw_values) | set(normalized)):
        left, right = raw_values.get(key), normalized.get(key)
        if left is not None and right is not None:
            if _equal(left, right):
                matched += 1
            else:
                mismatched.append(f"{_show(key)}: raw {sorted(left)} vs stored {sorted(right)}")
        elif left is not None:
            only_raw.append(f"{_show(key)}: parsed from raw, absent from the store")
        elif right is not None:
            only_normalized.append(f"{_show(key)}: in the store, no raw body produced it")

    return Audit(
        day=day,
        raw=raw_count,
        parsed=parsed,
        matched=matched,
        skipped=tuple(skipped),
        only_raw=tuple(only_raw),
        only_normalized=tuple(only_normalized),
        mismatched=tuple(mismatched),
    )


def unparsable(store: Store, day: str, limit: int = 5) -> Tuple[str, ...]:
    """The raw bodies the parser refused, and enough of them to name the cause.

    ``audit_day`` says *how many* bodies were skipped and why in one sentence;
    this says *what the venue actually sent*, because "the venue answered 200
    with an error document" (a rate limit, an outage on their side) and "the
    schema moved" (our parser) are different repairs with different urgency, and
    the difference lives in the bytes — which is the only reason the bytes are
    kept.  The list is capped: five bodies identify a pattern, ten thousand
    spell it out one row at a time.
    """
    if limit <= 0:
        return ()
    start = day_start(day)
    end = start + 86_400.0
    found: List[str] = []
    for record in store.records(kind="raw", since=start - 86_400.0, until=end):
        payload = RawPayload.from_dict({name: value for name, value in record.items() if name != "record"})
        if not start <= payload.received_ts < end:
            continue
        if not payload.parsable:
            reason = "no request context in the raw record: cannot be re-parsed at all"
        else:
            try:
                _reparse(payload)
                continue
            except ShapeError as error:
                reason = str(error)
        head = json.dumps(payload.body, sort_keys=True, ensure_ascii=False)
        if len(head) > 160:
            head = head[:160] + "…"
        found.append(f"{payload.venue}/{payload.endpoint} @ {payload.received_ts:.0f}: {reason} | {head}")
        if len(found) >= limit:
            break
    return tuple(found)


