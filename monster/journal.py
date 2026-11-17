"""The append-only, hash-chained journal — the monster's memory.

Why a journal before a strategy
==============================

"Learn from your mistakes" is a sentence about the past, and software has no
past unless it is written down.  Logs are not enough: a log can be rotated,
edited, reordered or silently truncated after a refactor, and every one of
those turns an autopsy into fiction.  So the first component is a chain.

Each line is a JSON object with ``seq``, ``ts``, ``kind``, ``payload``, ``prev``
and ``hash``, where ``hash = sha256(schema, seq, ts, kind, payload, prev)`` and
``prev`` is the previous line's hash.  Editing any line breaks every line after
it; deleting one leaves a ``prev`` that nothing hashes to.  :meth:`verify`
walks the file and names the first line that does not belong.

What the journal is *not*
=========================

* not a database: it is read forward and replayed, not queried;
* not a guarantee of honesty by itself: it proves that the record was not
  changed *after* it was written, not that the writer observed the world
  correctly — that is what the venue adapters and the null tests are for;
* not multi-writer: one process appends.  A second writer would interleave
  ``prev`` and break the chain, so single-writer is a deployment rule, stated
  here rather than enforced with a lock file that NFS would ignore anyway.

The journal is written with ``flush`` + ``fsync`` because the interesting
failure is the machine dying between a fill and its record.  ``fsync=False``
exists for tests and backfills, where the caller already holds the source.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional

from monster import SCHEMA
from monster.contracts import canonical

__all__ = ["JournalError", "Event", "Journal", "GENESIS"]

#: The ``prev`` of the first line.  Not a hash of anything: it says "this is the
#: beginning", the same way a zero nonce does.
GENESIS = "0" * 64


class JournalError(RuntimeError):
    """The journal cannot be read, appended or trusted."""


@dataclass(frozen=True)
class Event:
    """One recorded fact: a decision, a refusal, a fill, a funding payment."""

    schema: int
    seq: int
    ts: float
    kind: str
    payload: Dict[str, Any]
    prev: str
    hash: str

    def body(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "payload": self.payload,
            "prev": self.prev,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {**self.body(), "hash": self.hash}

    def digest(self) -> str:
        return hashlib.sha256(canonical(self.body()).encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "Event":
        try:
            return cls(
                schema=int(document["schema"]),
                seq=int(document["seq"]),
                ts=float(document["ts"]),
                kind=str(document["kind"]),
                payload=dict(document["payload"]),
                prev=str(document["prev"]),
                hash=str(document["hash"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise JournalError(f"not a journal event: {error}") from error


def _hash(schema: int, seq: int, ts: float, kind: str, payload: Dict[str, Any], prev: str) -> str:
    body = {"schema": schema, "seq": seq, "ts": ts, "kind": kind, "payload": payload, "prev": prev}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


class Journal:
    """One file, one writer, one chain."""

    def __init__(self, path: str | os.PathLike[str], *, fsync: bool = True) -> None:
        self.path = Path(path)
        self.fsync = fsync
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last: Optional[Event] = self._tail()

    # ── reading ─────────────────────────────────────────────────────────────

    def _tail(self) -> Optional[Event]:
        """Read the last line so the next append can continue the chain.

        Only the tail is checked here; the whole file is checked by
        :meth:`verify`, which the cycle calls before it acts.  Opening a journal
        is a cheap operation; auditing it is a deliberate one.
        """
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        last_line = ""
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last_line = line
        if not last_line:
            return None
        try:
            event = Event.from_dict(json.loads(last_line))
        except (json.JSONDecodeError, JournalError) as error:
            raise JournalError(f"{self.path}: the last line is not an event ({error})") from error
        if event.digest() != event.hash:
            raise JournalError(
                f"{self.path}: the last line (seq {event.seq}) fails its own hash — "
                "the file was edited; a journal that lies about the tail is not a journal"
            )
        return event

    def events(self) -> Iterator[Event]:
        """Yield every event, refusing a line that is not one."""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    yield Event.from_dict(json.loads(line))
                except (json.JSONDecodeError, JournalError) as error:
                    raise JournalError(f"{self.path}: line {number} is not an event ({error})") from error

    def last(self) -> Optional[Event]:
        return self._last

    def count(self) -> int:
        return sum(1 for _ in self.events())

    # ── writing ─────────────────────────────────────────────────────────────

    def append(self, kind: str, payload: Mapping[str, Any], *, ts: Optional[float] = None) -> Event:
        """Record one fact and return the line that now holds it."""
        if not isinstance(kind, str) or not kind.strip():
            raise JournalError("kind must be a non-empty string")
        if not isinstance(payload, Mapping):
            raise JournalError(f"payload must be a mapping, got {type(payload).__name__}")
        seq = 1 if self._last is None else self._last.seq + 1
        prev = GENESIS if self._last is None else self._last.hash
        stamp = float(time.time() if ts is None else ts)
        try:
            clean = json.loads(canonical(dict(payload)))
        except (TypeError, ValueError) as error:
            raise JournalError(f"payload is not JSON-clean: {error}") from error
        kind = kind.strip()
        event = Event(
            schema=SCHEMA,
            seq=seq,
            ts=stamp,
            kind=kind,
            payload=clean,
            prev=prev,
            hash=_hash(SCHEMA, seq, stamp, kind, clean, prev),
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(canonical(event.to_dict()) + "\n")
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())
        self._last = event
        return event

    # ── verification ────────────────────────────────────────────────────────

    def verify(self) -> str:
        """Walk the chain; return ``"ok"`` or the first thing that is wrong.

        The wording is a sentence rather than a boolean because the caller is
        usually a human reading a cycle report at 3 a.m., and "False" answers
        none of their questions.
        """
        expected_seq, expected_prev = 1, GENESIS
        for number, event in enumerate(self.events(), start=1):
            if event.seq != expected_seq:
                return f"line {number}: seq is {event.seq}, expected {expected_seq} (a line was removed or reordered)"
            if event.prev != expected_prev:
                return f"line {number}: prev does not hash to the previous line (a line was edited or inserted)"
            if event.schema != SCHEMA:
                return f"line {number}: schema {event.schema} is not understood by this reader"
            if event.digest() != event.hash:
                return f"line {number}: content does not hash to its own hash (the line was edited)"
            expected_seq += 1
            expected_prev = event.hash
        return "ok"

