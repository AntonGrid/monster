"""The store: one gzipped file per venue per UTC day, raw bodies and readings.

Layout
======

::

    <root>/2026-11-17/htx.jsonl.gz
    <root>/2026-11-17/okx.jsonl.gz

Each line is one JSON object with a ``kind``:

* ``raw`` — the venue's body, the request it answered and our receive time;
* ``quote`` — a :class:`monster.contracts.Quote`;
* ``funding`` — a :class:`monster.contracts.FundingQuote`.

Day files are gzip streams opened in append mode, so a capture run adds one
member and a crashed run leaves at most one bad tail line — which
:meth:`Store.read` names by file and line instead of skipping.  A store is a
cache of the world, not the world: silently dropping a corrupt line is how a
backtest acquires a gap nobody remembers choosing.

Why normalized rows *and* raw bodies
====================================

The raw body is what makes a parser bug repairable: ``quality.audit_day``
re-parses yesterday's bodies with today's parser and compares.  The normalized
row is what makes a query cheap and what a cycle consumes.  Keeping both costs
bytes; keeping only one costs the ability to answer "was this number ever true".

Duplicates
==========

The same ``(venue, symbol, ts)`` may arrive twice (a retry, a replayed tape).
The first observation wins — it is the one that was known at that instant — and
a *different* value under the same key becomes a :class:`Conflict`, reported
rather than resolved.  Two prices for one instant is an event about the feed,
and the feed is what the truth check is for.

Reading a store that is still being written
===========================================

Each write appends one complete gzip member, and a reader can open the file in
the millisecond between the member's first byte and its trailer.  That is not
corruption, it is a race with a live writer, so the reader re-reads the file
instead of failing or — worse — returning a short day: the whole file is
buffered per attempt, so a re-read can never yield the same record twice.  A
file that stays incomplete after several attempts is *then* an error, and it
says both things it could mean: a writer that is still appending, or a crash
that left a partial member.  :attr:`Store.reopens` counts the races, because a
number nobody records is a number nobody can audit.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from monster.contracts import ContractError, FundingQuote, Quote
from monster.data.raw import RawPayload

__all__ = ["StoreError", "Conflict", "Store", "Batch", "DAY_FORMAT"]

DAY_FORMAT = "%Y-%m-%d"

#: How many times a reader re-opens a file whose last gzip member is still
#: growing.  The window is one ``write`` (plus ``fsync``) long, so a few short
#: retries make a live store readable without ever calling a partial member
#: complete.
READ_ATTEMPTS = 4
READ_PAUSE_S = 0.05


class StoreError(RuntimeError):
    """The store cannot be read: a bad line, a bad gzip member, a bad day."""


@dataclass(frozen=True)
class Conflict:
    """Two different values recorded for one instrument at one instant."""

    venue: str
    symbol: str
    ts: float
    kept: Mapping[str, Any]
    dropped: Mapping[str, Any]

    def describe(self) -> str:
        return (
            f"{self.venue} {self.symbol} at {self.ts:.3f}: kept {dict(self.kept)}, "
            f"also saw {dict(self.dropped)} (first observation wins)"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "ts": self.ts,
            "kept": dict(self.kept),
            "dropped": dict(self.dropped),
            "reason": "two values for one instant; the first observation was kept",
        }


def day_start(day: str) -> float:
    try:
        parsed = dt.datetime.strptime(day, DAY_FORMAT).replace(tzinfo=dt.timezone.utc)
    except ValueError as error:
        raise StoreError(f"{day!r} is not a {DAY_FORMAT} day") from error
    return parsed.timestamp()


class Store:
    """Append-only, day-partitioned, gzip-compressed history on local disk."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        fsync: bool = False,
        read_attempts: int = READ_ATTEMPTS,
        read_pause_s: float = READ_PAUSE_S,
    ) -> None:
        self.root = Path(root).expanduser()
        self.fsync = bool(fsync)
        self.read_attempts = max(1, int(read_attempts))
        self.read_pause_s = max(0.0, float(read_pause_s))
        #: How many times a read had to re-open a file whose tail was still
        #: being written.  Not an error: a fact about reading a live store.
        self.reopens = 0

    # ── layout ──────────────────────────────────────────────────────────────

    @staticmethod
    def day_of(ts: float) -> str:
        """The UTC day a timestamp belongs to (not the local one)."""
        return dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc).strftime(DAY_FORMAT)

    def path(self, day: str, venue: str) -> Path:
        return self.root / day / f"{venue.strip().lower()}.jsonl.gz"

    def days(self) -> Tuple[str, ...]:
        if not self.root.exists():
            return ()
        found = [
            child.name
            for child in self.root.iterdir()
            if child.is_dir() and len(child.name) == 10 and child.name[4] == "-" and child.name[:4].isdigit()
        ]
        return tuple(sorted(found))

    def venues(self, day: Optional[str] = None) -> Tuple[str, ...]:
        days = (day,) if day else self.days()
        found: set[str] = set()
        for name in days:
            directory = self.root / name
            if directory.is_dir():
                for child in directory.iterdir():
                    if child.name.endswith(".jsonl.gz"):
                        found.add(child.name[: -len(".jsonl.gz")])
        return tuple(sorted(found))

    # ── writing ─────────────────────────────────────────────────────────────

    def _append(self, day: str, venue: str, records: Iterable[Mapping[str, Any]]) -> Path:
        target = self.path(day, venue)
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) for record in records]
        if not lines:
            return target
        # ``at`` appends a gzip member; the member is complete only if the
        # process survives the write, and ``read`` refuses an incomplete tail.
        with gzip.open(target, "at", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())
        return target

    def write_raw(self, payload: RawPayload) -> Path:
        record = {"record": "raw", **payload.to_dict()}
        return self._append(self.day_of(payload.received_ts), payload.venue, (record,))

    def write_quotes(self, quotes: Sequence[Quote]) -> Tuple[Path, ...]:
        grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for quote in quotes:
            grouped.setdefault((self.day_of(quote.ts), quote.venue), []).append(
                {"record": "quote", **quote.to_dict()}
            )
        return tuple(self._append(day, venue, rows) for (day, venue), rows in sorted(grouped.items()))

    def write_fundings(self, rates: Sequence[FundingQuote]) -> Tuple[Path, ...]:
        grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for rate in rates:
            grouped.setdefault((self.day_of(rate.ts), rate.venue), []).append(
                {"record": "funding", **rate.to_dict()}
            )
        return tuple(self._append(day, venue, rows) for (day, venue), rows in sorted(grouped.items()))

    # ── reading ─────────────────────────────────────────────────────────────

    def _selected_days(self, since: Optional[float], until: Optional[float]) -> Tuple[str, ...]:
        available = self.days()
        if since is None and until is None:
            return available
        low = since if since is not None else float("-inf")
        high = until if until is not None else float("inf")
        return tuple(day for day in available if day_start(day) <= high and day_start(day) + 86_400.0 > low)

    def _read_file(self, path: Path) -> Iterator[Dict[str, Any]]:
        attempt = 0
        while True:
            # The whole file is buffered before anything is yielded, so a
            # re-read cannot hand the same record to a caller twice.
            records: List[Dict[str, Any]] = []
            try:
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    for number, line in enumerate(handle, start=1):
                        if not line.strip():
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError as error:
                            raise StoreError(f"{path}: line {number} is not JSON ({error})") from error
                        if not isinstance(record, dict) or "record" not in record:
                            raise StoreError(f"{path}: line {number} has no 'record' and is not a store record")
                        records.append(record)
            except EOFError as error:
                # The last member has no trailer yet: a writer is inside its
                # ``write``, or a crash left a partial member behind.  Re-read
                # before deciding which of the two it was.
                attempt += 1
                self.reopens += 1
                if attempt >= self.read_attempts:
                    raise StoreError(
                        f"{path}: the last gzip member is incomplete after {attempt} attempt(s) ({error}); "
                        "a writer may still be appending, or a crash left a partial member"
                    ) from error
                if self.read_pause_s:
                    time.sleep(self.read_pause_s)
                continue
            except (gzip.BadGzipFile, OSError) as error:
                raise StoreError(f"{path}: the gzip member is unreadable ({error})") from error
            yield from records
            return

    @staticmethod
    def _stamp(record: Mapping[str, Any]) -> float:
        raw = record.get("received_ts") if record.get("record") == "raw" else record.get("ts")
        try:
            return float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise StoreError(f"a {record.get('record')!r} record has no usable timestamp: {raw!r}") from error

    def records(
        self,
        *,
        since: Optional[float] = None,
        until: Optional[float] = None,
        kind: Optional[str] = None,
        venue: Optional[str] = None,
        day: Optional[str] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Yield records in day order, filtered; raises on a line it cannot read."""
        wanted_venue = venue.strip().lower() if venue else None
        for name in (day,) if day else self._selected_days(since, until):
            for found in self.venues(name):
                if wanted_venue is not None and found != wanted_venue:
                    continue
                for record in self._read_file(self.path(name, found)):
                    if kind is not None and record.get("record") != kind:
                        continue
                    stamp = self._stamp(record)
                    if since is not None and stamp < since:
                        continue
                    if until is not None and stamp > until:
                        continue
                    yield record

    # ── queries ─────────────────────────────────────────────────────────────

    def read_quotes(
        self,
        *,
        since: Optional[float] = None,
        until: Optional[float] = None,
        venues: Optional[Sequence[str]] = None,
        symbols: Optional[Sequence[str]] = None,
    ) -> "Batch":
        """Deduplicated quotes, oldest first, with the conflicts left over."""
        want_venues = {item.strip().lower() for item in venues} if venues else None
        want_symbols = {item.strip().lower() for item in symbols} if symbols else None
        seen: Dict[Tuple[str, str, float], Quote] = {}
        conflicts: List[Conflict] = []
        dropped = 0
        for record in self.records(kind="quote", since=since, until=until):
            try:
                quote = Quote.from_dict({name: record[name] for name in ("venue", "symbol", "bid", "ask", "ts")})
            except (KeyError, TypeError) as error:
                raise StoreError(f"a quote record is missing a field: {record!r}") from error
            except ContractError as error:
                raise StoreError(
                    f"the store holds an invalid quote for {record.get('venue')} {record.get('symbol')}: {error}"
                ) from error
            if want_venues is not None and quote.venue not in want_venues:
                continue
            if want_symbols is not None and quote.symbol not in want_symbols:
                continue
            key = (quote.venue, quote.symbol, quote.ts)
            previous = seen.get(key)
            if previous is not None:
                dropped += 1
                if (previous.bid, previous.ask) != (quote.bid, quote.ask):
                    conflicts.append(
                        Conflict(
                            venue=quote.venue,
                            symbol=quote.symbol,
                            ts=quote.ts,
                            kept={"bid": previous.bid, "ask": previous.ask},
                            dropped={"bid": quote.bid, "ask": quote.ask},
                        )
                    )
                continue
            seen[key] = quote
        ordered = tuple(sorted(seen.values(), key=lambda item: (item.ts, item.venue, item.symbol)))
        return Batch(items=ordered, conflicts=tuple(conflicts), dropped=dropped)

    def read_fundings(
        self,
        *,
        since: Optional[float] = None,
        until: Optional[float] = None,
        venues: Optional[Sequence[str]] = None,
        symbols: Optional[Sequence[str]] = None,
    ) -> "Batch":
        """Deduplicated funding quotes, oldest first, with the conflicts left over."""
        want_venues = {item.strip().lower() for item in venues} if venues else None
        want_symbols = {item.strip().lower() for item in symbols} if symbols else None
        seen: Dict[Tuple[str, str, float], FundingQuote] = {}
        conflicts: List[Conflict] = []
        dropped = 0
        for record in self.records(kind="funding", since=since, until=until):
            try:
                rate = FundingQuote.from_dict(
                    {name: record[name] for name in ("venue", "symbol", "rate", "period_hours", "ts")}
                )
            except (KeyError, TypeError) as error:
                raise StoreError(f"a funding record is missing a field: {record!r}") from error
            except ContractError as error:
                raise StoreError(
                    f"the store holds an invalid funding quote for {record.get('venue')} {record.get('symbol')}: {error}"
                ) from error
            if want_venues is not None and rate.venue not in want_venues:
                continue
            if want_symbols is not None and rate.symbol not in want_symbols:
                continue
            key = (rate.venue, rate.symbol, rate.ts)
            previous = seen.get(key)
            if previous is not None:
                dropped += 1
                if (previous.rate, previous.period_hours) != (rate.rate, rate.period_hours):
                    conflicts.append(
                        Conflict(
                            venue=rate.venue,
                            symbol=rate.symbol,
                            ts=rate.ts,
                            kept={"rate": previous.rate, "period_hours": previous.period_hours},
                            dropped={"rate": rate.rate, "period_hours": rate.period_hours},
                        )
                    )
                continue
            seen[key] = rate
        ordered = tuple(sorted(seen.values(), key=lambda item: (item.ts, item.venue, item.symbol)))
        return Batch(items=ordered, conflicts=tuple(conflicts), dropped=dropped)

    def quotes(self, **kwargs: Any) -> Tuple[Quote, ...]:
        return self.read_quotes(**kwargs).items  # type: ignore[return-value]

    def fundings(self, **kwargs: Any) -> Tuple[FundingQuote, ...]:
        return self.read_fundings(**kwargs).items  # type: ignore[return-value]

    def raws(self, day: Optional[str] = None) -> Iterator[RawPayload]:
        for record in self.records(kind="raw", day=day):
            body = dict(record)
            body.pop("record", None)
            yield RawPayload.from_dict(body)

    def stats(self, day: Optional[str] = None) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for record in self.records(day=day):
            kind = str(record.get("record"))
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    def latest(self, kind: str = "quote") -> Optional[float]:
        """The newest stamp of one record kind, scanning the newest day first.

        A cycle asks "what is the latest reading" far more often than "what
        happened"; walking backwards from today keeps that question O(today)
        instead of O(everything the store has ever seen).
        """
        for day in reversed(self.days()):
            newest: Optional[float] = None
            for venue in self.venues(day):
                for record in self._read_file(self.path(day, venue)):
                    if record.get("record") != kind:
                        continue
                    stamp = self._stamp(record)
                    if newest is None or stamp > newest:
                        newest = stamp
            if newest is not None:
                return newest
        return None


@dataclass(frozen=True)
class Batch:
    """A deduplicated query result: what was kept, and what disagreed with it."""

    items: Tuple[Any, ...] = ()
    conflicts: Tuple[Conflict, ...] = ()
    dropped: int = 0

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[Any]:
        return iter(self.items)



