"""From a store to the two things a cycle needs: a snapshot and a history.

No look-ahead
=============

The snapshot is built as of ``at``: every record used is stamped at or before
it, and the venue's own timestamps (not the file names) decide.  A quote older
than ``quote_max_age_s`` is dropped and named — a two-minute-old top of book is
not a price, and handing it to the truth check would report "stale" on every
venue at once instead of saying that the collector stopped.  Funding is
longer-lived by nature, so it has its own window: a rate published twelve hours
ago is still the rate being paid; a rate published three days ago is a rumour.

Aligned history, or none
========================

Coherence compares oscillators row by row, so two price series of different
lengths are not a smaller reading — they are a wrong one.  ``history_window``
therefore builds **completed** bars only (a bar whose end is still in the
future is not a close), takes the last mid in each bar, and intersects the bar
sets across symbols.  A symbol that is missing more than a few bars is excluded
by name, because a series patched by interpolation would be a series the venue
never printed.  Fewer bars than the coherence window is fine here: the cycle
reports ``coherence unmeasured``, which is the honest answer.

The result is a :class:`Derivation` — snapshot, history, notes, gaps, coverage —
so the CLI can journal the provenance before the cycle starts and the operator
can read *which* data the decision saw.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from monster.contracts import FundingQuote, Quote, Snapshot
from monster.data.quality import Coverage, Gap, coverage as measure_coverage, find_gaps
from monster.data.store import Store
from monster.instruments import PAIRS

__all__ = ["DataError", "Derivation", "snapshot_from", "history_window", "derive"]


class DataError(RuntimeError):
    """The store cannot produce a decision-grade snapshot, and says why."""


@dataclass(frozen=True)
class Derivation:
    """One as-of view of the store, with the evidence about its own quality."""

    at: float
    snapshot: Snapshot
    history: Mapping[str, Tuple[float, ...]]
    notes: Tuple[str, ...] = ()
    gaps: Tuple[Gap, ...] = ()
    coverage: Tuple[Coverage, ...] = ()

    def history_lists(self) -> Dict[str, List[float]]:
        return {symbol: list(values) for symbol, values in self.history.items()}

    def to_dict(self, *, gaps_limit: int = 50) -> Dict[str, Any]:
        return {
            "at": self.at,
            "source": self.snapshot.source,
            "quotes": len(self.snapshot.quotes),
            "funding": len(self.snapshot.funding),
            "venues": list(self.snapshot.venues()),
            "symbols": list(self.snapshot.symbols()),
            "history_bars": {symbol: len(values) for symbol, values in sorted(self.history.items())},
            "gaps": [gap.to_dict() for gap in self.gaps[:gaps_limit]],
            "gaps_total": len(self.gaps),
            "coverage": [item.to_dict() for item in self.coverage],
            "notes": list(self.notes),
        }

    def describe(self) -> str:
        head = (
            f"data: {len(self.snapshot.quotes)} quote(s), {len(self.snapshot.funding)} funding quote(s) "
            f"as of {self.at:.0f} from {self.snapshot.source or 'unknown source'}"
        )
        if self.history:
            bars = {symbol: len(values) for symbol, values in self.history.items()}
            head += " | history " + ", ".join(f"{symbol} {count}" for symbol, count in sorted(bars.items()))
        if self.gaps:
            head += f" | {len(self.gaps)} gap(s)"
        for note in self.notes:
            head += f"\n  {note}"
        return head


def snapshot_from(
    quotes: Sequence[Quote],
    fundings: Sequence[FundingQuote],
    at: float,
    *,
    quote_max_age_s: float,
    funding_max_age_h: float,
    source: str = "",
) -> Tuple[Snapshot, Tuple[str, ...]]:
    """Latest fresh reading per instrument, or a named reason for its absence."""
    at = float(at)
    notes: List[str] = []
    latest: Dict[Tuple[str, str], Quote] = {}
    for quote in quotes:
        if quote.ts > at + 1e-9:
            continue
        key = (quote.venue, quote.symbol)
        if key not in latest or quote.ts > latest[key].ts:
            latest[key] = quote

    fresh: List[Quote] = []
    for key, quote in sorted(latest.items()):
        age = at - quote.ts
        if age > quote_max_age_s:
            notes.append(
                f"{quote.venue} {quote.symbol}: last quote is {age:.1f}s old, limit {quote_max_age_s:.1f}s "
                f"— dropped from the snapshot"
            )
            continue
        fresh.append(quote)

    latest_funding: Dict[Tuple[str, str], FundingQuote] = {}
    for rate in fundings:
        if rate.ts > at + 1e-9:
            continue
        key = (rate.venue, rate.symbol)
        if key not in latest_funding or rate.ts > latest_funding[key].ts:
            latest_funding[key] = rate

    funding: List[FundingQuote] = []
    for key, rate in sorted(latest_funding.items()):
        age_h = (at - rate.ts) / 3600.0
        if age_h > funding_max_age_h:
            notes.append(
                f"{rate.venue} {rate.symbol}: funding is {age_h:.1f}h old, limit {funding_max_age_h:.1f}h "
                f"— the rate is unmeasured"
            )
            continue
        funding.append(rate)

    if not fresh:
        raise DataError(
            f"no quote at or before {at:.0f} is younger than {quote_max_age_s:.1f}s; "
            "the capture stopped, or the clock moved, and the cycle has nothing to trade on"
        )

    snapshot = Snapshot(ts=at, quotes=tuple(fresh), funding=tuple(funding), source=source)
    return snapshot, tuple(notes)


def history_window(
    quotes: Sequence[Quote],
    symbols: Iterable[str],
    *,
    until: float,
    interval_s: float,
    bars: int,
    min_fraction: float = 0.9,
) -> Tuple[Dict[str, Tuple[float, ...]], Tuple[str, ...]]:
    """Aligned bar closes, complete bars only, with a note per excluded symbol."""
    if interval_s <= 0.0:
        raise ValueError("interval_s must be positive")
    if bars <= 0:
        raise ValueError("bars must be positive")
    until = float(until)
    # A bar is the half-open interval (k·Δ, (k+1)·Δ]; a tick exactly on a
    # boundary belongs to the bar it closes.  The latest bar included is the one
    # whose end is at or before ``until`` — a bar still forming has no close.
    def bar_of(stamp: float) -> int:
        return int(math.ceil(stamp / interval_s - 1e-9)) - 1

    last = bar_of(until)
    wanted = [index for index in range(last - bars + 1, last + 1) if index >= 0]
    notes: List[str] = []
    if len(wanted) < bars:
        notes.append(
            f"history: {len(wanted)} of {bars} bars fit before {until:.0f} at {interval_s:g}s "
            "(the store does not reach that far back)"
        )
    wanted_set = set(wanted)

    by_symbol: Dict[str, Dict[int, float]] = {}
    for symbol in symbols:
        closes: Dict[int, float] = {}
        for quote in quotes:
            if quote.symbol != symbol:
                continue
            index = bar_of(quote.ts)
            if index in wanted_set:
                closes[index] = quote.mid  # quotes arrive oldest first: the last mid wins
        by_symbol[symbol] = closes

    usable: Dict[str, Dict[int, float]] = {}
    excluded: List[str] = []
    for symbol, closes in sorted(by_symbol.items()):
        fraction = 0.0 if not wanted else len(closes) / len(wanted)
        if fraction < min_fraction:
            excluded.append(f"{symbol} {len(closes)}/{len(wanted)}")
            continue
        usable[symbol] = closes
    if excluded:
        notes.append(
            f"history: {len(excluded)} symbol(s) excluded for missing bars "
            f"({', '.join(excluded)}); they are not interpolated"
        )

    if not usable:
        return {}, tuple(notes)

    common = set(wanted)
    for closes in usable.values():
        common &= set(closes)
    ordered = sorted(common)
    if len(ordered) < len(wanted):
        notes.append(f"history: {len(ordered)} aligned bars across {len(usable)} symbol(s)")
    history = {symbol: tuple(usable[symbol][index] for index in ordered) for symbol in sorted(usable)}
    return history, tuple(notes)


def derive(
    store: Store,
    *,
    at: Optional[float] = None,
    interval_s: float = 60.0,
    bars: int = 128,
    quote_max_age_s: float = 5.0,
    funding_max_age_h: float = 12.0,
    history_symbols: Optional[Sequence[str]] = None,
) -> Derivation:
    """Build one as-of snapshot and its aligned history from the store.

    Only the window the answer needs is read: the history depth, or the funding
    freshness limit, whichever is longer.  A cycle that ran every minute against
    a month of ticks must not re-parse the month to decide about this minute.
    """
    if at is None:
        at = store.latest("quote")
        if at is None:
            raise DataError(f"{store.root}: the store holds no quotes; run a capture before a cycle")
    at = float(at)
    window = max(bars * float(interval_s), funding_max_age_h * 3600.0, quote_max_age_s) + float(interval_s)
    quote_batch = store.read_quotes(since=at - window, until=at)
    funding_batch = store.read_fundings(until=at)

    snapshot, notes = snapshot_from(
        quote_batch.items,
        funding_batch.items,
        at,
        quote_max_age_s=quote_max_age_s,
        funding_max_age_h=funding_max_age_h,
        source=f"store:{store.root}",
    )
    notes = list(notes)
    for conflict in quote_batch.conflicts:
        notes.append(f"conflict: {conflict.describe()}")
    for conflict in funding_batch.conflicts:
        notes.append(f"conflict: {conflict.describe()}")

    known = {symbol for pair in PAIRS.values() for symbol in pair}
    quoted = {quote.symbol for quote in quote_batch.items}
    if history_symbols:
        symbols = tuple(symbol.strip().lower() for symbol in history_symbols)
    else:
        symbols = tuple(sorted(known & quoted))
        unknown = sorted(quoted - known)
        if unknown:
            notes.append(f"history: symbol(s) outside the instrument table ignored: {', '.join(unknown)}")

    history, history_notes = history_window(
        quote_batch.items, symbols, until=at, interval_s=interval_s, bars=bars
    )
    notes.extend(history_notes)

    window_start = at - bars * float(interval_s)
    paired = tuple(quote for quote in quote_batch.items if quote.symbol in known)
    gaps = find_gaps(paired, interval_s, since=window_start, until=at)
    covs = measure_coverage(paired, interval_s, since=window_start, until=at)
    thin = [item for item in covs if not item.ok()]
    if thin:
        notes.append(
            f"coverage: {len(thin)} series below 98% of their slots "
            + "("
            + ", ".join(f"{item.venue} {item.symbol} {item.fraction:.0%}" for item in thin)
            + ")"
        )

    return Derivation(
        at=at,
        snapshot=snapshot,
        history=history,
        notes=tuple(notes),
        gaps=gaps,
        coverage=covs,
    )

