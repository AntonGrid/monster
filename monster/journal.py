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
  ``prev`` and break the chain, so every writer gets its own file: the data
  collector writes ``var/journal.capture.jsonl``, the cycle writes
  ``var/journal.jsonl``, and the cycle records the other chain's head as a
  ``journal.linked`` event so the two histories can be tied together after the
  fact.  The rule is a deployment rule, but it is not left to discipline:
  :meth:`Journal.append` notices when the file grew behind its back and refuses,
  because a silent fork is discovered months later by ``verify`` while a refusal
  is discovered the same minute.

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
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from monster import SCHEMA
from monster.contracts import canonical

__all__ = ["JournalError", "Event", "Journal", "LinkReport", "LINK_KIND", "GENESIS"]

#: The event kind whose payload is a claim about *another* chain:
#: ``{"chain": "capture", "path": ..., "head": ..., "seq": ..., "events": ...}``.
#: One writer per chain means two files, and a link is the only thing that keeps
#: "these two files belong together" from being a matter of trust.
LINK_KIND = "journal.linked"


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


def _short(digest: Optional[str]) -> str:
    return "none" if digest is None else str(digest)[:12]


@dataclass(frozen=True)
class LinkReport:
    """One chain's claim about another chain, checked against the file.

    ``status`` is one of:

    ``ok``          the linked chain still has the recorded line with the recorded hash;
    ``unlinked``    the cycle recorded that it could not read the chain — no claim, nothing to check;
    ``missing``     the linked file is gone, so the claim cannot be checked at all;
    ``edited``      the recorded line is there but hashes to something else;
    ``truncated``   the linked chain ends before the recorded line;
    ``broken``      the linked chain fails its own verification (edited line, interleaved writer);
    ``unreadable``  the linked file is not a journal this reader can parse.

    Only ``ok`` and ``unlinked`` hold.  The distinction matters because the fix
    is different: ``edited`` means somebody rewrote history, ``missing`` means
    somebody deleted it, and both are answers, not accidents.
    """

    seq: int
    path: Optional[Path]
    expected_seq: Optional[int]
    expected_head: Optional[str]
    actual_head: Optional[str]
    actual_events: Optional[int]
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "unlinked"}

    def describe(self) -> str:
        head = f"line {self.expected_seq} hash {_short(self.expected_head)}" if self.expected_head else "no head"
        target = "<no path recorded>" if self.path is None else str(self.path)
        where = f"link at seq {self.seq} -> {target}"
        if self.status == "ok":
            return f"{where}: ok ({head}, {self.actual_events} events)"
        if self.status == "unlinked":
            return f"{where}: no head recorded, nothing to check"
        if self.status == "missing":
            return f"{where}: the linked chain is gone (no file at that path)"
        if self.status == "edited":
            return (
                f"{where}: {head} is now {_short(self.actual_head)} — the linked chain was rewritten "
                "after this book read it"
            )
        if self.status == "truncated":
            return f"{where}: the chain ends at line {self.actual_events}, this book read {head}"
        if self.status == "broken":
            return f"{where}: the linked chain does not verify ({self.detail})"
        return f"{where}: not readable ({self.detail})"


class Journal:
    """One file, one writer, one chain."""

    def __init__(self, path: str | os.PathLike[str], *, fsync: bool = True) -> None:
        self.path = Path(path)
        self.fsync = fsync
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last: Optional[Event] = self._tail()
        #: Size of the file as this writer last left it.  A difference at the
        #: next append means somebody else is appending, and a forked chain.
        self._size = self._size_on_disk()

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

    def _size_on_disk(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def _refuse_a_second_writer(self) -> None:
        """Refuse to append to a file that changed since this writer left it.

        Two processes on one chain do not raise an error — they write a journal
        whose ``verify`` fails at the first interleaved line, which is found
        after the history has already been used.  One ``stat`` per append turns
        that into a refusal the writer can report the same minute, and the
        refusal names the fix (a chain per writer, linked by hash).
        """
        size = self._size_on_disk()
        if size != self._size:
            raise JournalError(
                f"{self.path}: the file grew from {self._size} to {size} bytes since this writer "
                "last appended — another process is writing this chain; give each writer its own "
                "journal and link them by hash (a journal.linked event carries the head)"
            )

    def append(self, kind: str, payload: Mapping[str, Any], *, ts: Optional[float] = None) -> Event:
        """Record one fact and return the line that now holds it."""
        if not isinstance(kind, str) or not kind.strip():
            raise JournalError("kind must be a non-empty string")
        if not isinstance(payload, Mapping):
            raise JournalError(f"payload must be a mapping, got {type(payload).__name__}")
        self._refuse_a_second_writer()
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
        self._size = self._size_on_disk()
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

    # ── links ───────────────────────────────────────────────────────────────

    def links(self) -> Iterator[Event]:
        """Yield the events that make a claim about another chain."""
        for event in self.events():
            if event.kind == LINK_KIND:
                yield event

    def verify_links(self) -> Tuple[LinkReport, ...]:
        """Check every link this chain makes about another chain.

        A link is a claim about a *moment*: "when this cycle ran, the capture
        chain had N lines and line N hashed to H".  Later appends are normal and
        are not a mismatch — so the check is the line at the recorded ``seq``,
        not the file's current tail.  The linked chain is also verified as a
        chain, because an edit after the recorded line would otherwise pass.
        """
        return tuple(self._check_link(event) for event in self.links())

    def _resolve_link_path(self, recorded: str) -> Path:
        """Find the linked file, from this process' cwd or the journal's own directory.

        The cycle records the path it was configured with, which is relative to
        the working directory it ran in.  ``--verify`` may be run from anywhere,
        so a relative path that does not exist here is retried beside this
        journal — the same checkout, a different cwd.
        """
        path = Path(recorded)
        if path.is_absolute() or path.exists():
            return path
        sibling = self.path.parent / path
        return sibling if sibling.exists() else path

    def _check_link(self, event: Event) -> LinkReport:
        payload = event.payload
        recorded = str(payload.get("path") or "")
        expected_head = payload.get("head")
        expected_seq = payload.get("seq")
        expected_seq = None if expected_seq is None else int(expected_seq)
        path = self._resolve_link_path(recorded) if recorded else None

        def report(
            status: str,
            *,
            actual_head: Optional[str] = None,
            actual_events: Optional[int] = None,
            detail: str = "",
        ) -> LinkReport:
            return LinkReport(
                seq=event.seq,
                path=path,
                expected_seq=expected_seq,
                expected_head=expected_head,
                actual_head=actual_head,
                actual_events=actual_events,
                status=status,
                detail=detail,
            )

        if path is None:
            return report("unreadable", detail="the link names no path")
        if expected_head is None:
            return report("unlinked")
        if not path.exists():
            return report("missing")
        try:
            linked = Journal(path, fsync=False)
        except JournalError as error:
            return report("broken", detail=str(error))
        actual_head: Optional[str] = None
        actual_events = 0
        try:
            for item in linked.events():
                actual_events += 1
                if expected_seq is None or item.seq == expected_seq:
                    actual_head = item.hash
            verdict = linked.verify()
        except JournalError as error:
            return report("unreadable", actual_events=actual_events, detail=str(error))
        if verdict != "ok":
            return report("broken", actual_head=actual_head, actual_events=actual_events, detail=verdict)
        if actual_head is None:
            return report("truncated", actual_events=actual_events)
        if actual_head != expected_head:
            return report("edited", actual_head=actual_head, actual_events=actual_events)
        return report("ok", actual_head=actual_head, actual_events=actual_events)

