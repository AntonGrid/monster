"""Replaying the journal: the cache is a convenience, the chain is the book.

Why a replay and not a backup
=============================

``var/state.json`` exists because rebuilding a book from a journal of fills is
slower than reading a JSON file, not because the book lives there.  The moment
the two disagree, one of them is wrong and the process cannot tell which: a
state file that says *two positions* beside a journal that opened *one* is how
a book ends up short a leg nobody sold — the failure class that only surfaces as
a PnL that does not reconcile, weeks later, as a phantom position.

So the replay is the definition of the book.  Only four kinds move it —
``order.filled``, ``trade.closed``, ``funding.accrued`` and ``day.rolled`` — and
each one is applied through the same :class:`~monster.exec.paper.PaperVenue`
method the trading path used.  :func:`compare` then names every field where the
cache disagrees.  A cycle that finds a disagreement refuses to trade and writes
``state.diverged``: it does not average the two books, and it does not "repair"
the cache from a position it cannot prove it held.

What a replay cannot prove
==========================

That the fills happened.  It proves the book is the one the journal describes,
which is all a paper venue can prove.  It also cannot resurrect an edited
journal: :meth:`~monster.journal.Journal.verify` runs first, and a chain that
fails is refused before a single event is replayed.

The reader is deliberately forgiving about *kinds* it does not know — a later
version may write ``margin.called`` and this one should still rebuild the book
it understands, and say so in ``ignored`` — and deliberately unforgiving about a
``trade.closed`` that names a position the journal never opened.  Forgiving
where the format grows and strict where the arithmetic must hold is the
difference between forward compatibility and a book that quietly drifts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Type, TypeVar

from monster.config import Config
from monster.contracts import ClosedTrade, Fill
from monster.exec.paper import PaperVenue
from monster.journal import Event, Journal

__all__ = [
    "ReplayError",
    "Divergence",
    "Replay",
    "ReconcileReport",
    "BOOK_KINDS",
    "replay",
    "replay_events",
    "compare",
    "reconcile",
]

#: The only kinds that change what the book owns.  Everything else is context:
#: a reading, a refusal, a mark.  A new *book* event must be added here, which is
#: the point of the list being short and explicit.
BOOK_KINDS = frozenset({"order.filled", "trade.closed", "funding.accrued", "day.rolled"})

#: Kinds the replay knows about and expects to leave the book alone.
QUIET_KINDS = frozenset(
    {
        "autopsy.summary",
        "carry.scan",
        "close.unmeasured",
        "coherence.read",
        "coherence.unmeasured",
        "cycle.aborted",
        "cycle.finished",
        "cycle.started",
        "data.captured",
        "data.derived",
        "equity.mark",
        "funding.unmeasured",
        "intent.proposed",
        "intent.refused",
        "journal.linked",
        "kill.reset",
        "kill.tripped",
        "position.opened",
        "state.diverged",
        "state.rebuilt",
        "truth.read",
    }
)

_T = TypeVar("_T", Fill, ClosedTrade)


class ReplayError(RuntimeError):
    """The journal cannot be rebuilt into one book."""


@dataclass(frozen=True)
class Divergence:
    """One field where two accounts of the same book disagree."""

    field: str
    cached: Any
    replayed: Any

    def to_dict(self) -> Dict[str, Any]:
        return {"field": self.field, "cached": self.cached, "replayed": self.replayed}

    def describe(self) -> str:
        return f"{self.field}: cache {self.cached!r}, journal {self.replayed!r}"


@dataclass(frozen=True)
class Replay:
    """The book the journal describes, and what the reader could not use."""

    venue: PaperVenue
    events: int
    ignored: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()
    disagreements: Tuple[Divergence, ...] = ()

    def describe(self) -> str:
        lines = [
            f"replay: {self.events} event(s) -> cash {self.venue.cash:.2f} USD, "
            f"{len(self.venue.positions)} position(s), {len(self.venue.closed)} closed"
        ]
        for key, position in sorted(self.venue.positions.items()):
            lines.append(f"  {key[0]} {key[1]} {key[2]}: {position.qty:+.8f} @ {position.avg_price:.4f}")
        for note in self.notes:
            lines.append(f"note: {note}")
        for divergence in self.disagreements:
            lines.append(f"disagreement: {divergence.describe()}")
        return "\n".join(lines)


@dataclass(frozen=True)
class ReconcileReport:
    """What the cycle should trade, and why that is the book it thinks it is."""

    #: ``ok`` (the cache agrees with the chain), ``rebuilt`` (no cache, the chain
    #: was used), ``fresh`` (nothing anywhere yet) or ``diverged`` (they disagree).
    mode: str
    book: PaperVenue
    events: int
    divergences: Tuple[Divergence, ...] = ()
    ignored: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    @property
    def diverged(self) -> bool:
        return self.mode == "diverged"

    @property
    def rebuilt(self) -> bool:
        return self.mode in ("rebuilt", "fresh")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "events": self.events,
            "divergences": [divergence.to_dict() for divergence in self.divergences],
            "ignored": list(self.ignored),
            "notes": list(self.notes),
            "cash_usd": self.book.cash,
            "positions": len(self.book.positions),
            "closed": len(self.book.closed),
        }

    def describe(self) -> str:
        lines = [f"reconcile: {self.mode} after {self.events} journal event(s)"]
        lines.append(
            f"  book: cash {self.book.cash:.2f} USD, {len(self.book.positions)} position(s), "
            f"{len(self.book.closed)} closed"
        )
        for divergence in self.divergences:
            lines.append(f"  {divergence.describe()}")
        for note in self.notes:
            lines.append(f"  note: {note}")
        if self.ignored:
            lines.append(f"  ignored kinds: {', '.join(self.ignored)}")
        return "\n".join(lines)


def _close(left: float, right: float, rel: float) -> bool:
    """Equal up to a relative tolerance, with an absolute floor at one unit."""
    return abs(left - right) <= rel * max(1.0, abs(left), abs(right))


def _label(key: Tuple[str, str, str]) -> str:
    return f"{key[0]} {key[1]} {key[2]}"


def _contract(cls: Type[_T], event: Event) -> _T:
    try:
        return cls.from_dict(event.payload)
    except (KeyError, TypeError, ValueError) as error:
        raise ReplayError(f"line {event.seq} ({event.kind}): {error}") from error


def _opening(config: Config, events: Sequence[Event]) -> Tuple[Dict[str, Any], Optional[str]]:
    """The parameters the book was opened with, not the ones configured today.

    ``cycle.started`` carries the whole config, so the first one in the chain is
    the book's own arithmetic: changing ``fee_bps`` in the environment must not
    silently re-price a position that was opened under the old fee.  Falling back
    to ``config`` is for a chain with no cycle in it (a backfill), and says so.
    """
    for event in events:
        if event.kind != "cycle.started":
            continue
        recorded = event.payload.get("config") or {}
        return (
            {
                "cash_usd": float(recorded.get("capital_usd", config.capital_usd)),
                "fee_bps": float(recorded.get("fee_bps", config.fee_bps)),
                "slippage_bps": float(recorded.get("slippage_bps", config.slippage_bps)),
                "leverage": float(recorded.get("leverage", config.leverage)),
                "ts": float(event.payload.get("ts", event.ts)),
            },
            None,
        )
    return (
        {
            "cash_usd": config.capital_usd,
            "fee_bps": config.fee_bps,
            "slippage_bps": config.slippage_bps,
            "leverage": config.leverage,
            "ts": 0.0,
        },
        "no cycle.started in the journal: the book was opened with today's config",
    )


def _apply_funding(venue: PaperVenue, event: Event, disagreements: List[Divergence]) -> None:
    """Credit the accruals the journal recorded, exactly as they were recorded.

    The amount comes from the event, not from a re-derived rate: what happened is
    the payment, and re-deriving it would make the replay agree with itself
    instead of with the book.  The position's ``last_funding_ts`` is advanced by
    whole periods so a later settlement is counted once.
    """
    for entry in event.payload.get("accruals", ()):
        try:
            key = (str(entry["venue"]).lower(), str(entry["symbol"]).lower(), "perp")
            amount = float(entry["amount_usd"])
            periods = int(entry["periods"])
            period_seconds = float(entry["period_hours"]) * 3600.0
        except (KeyError, TypeError, ValueError) as error:
            raise ReplayError(f"line {event.seq} (funding.accrued): {error}") from error
        position = venue.positions.get(key)
        if position is None:
            disagreements.append(
                Divergence(f"line {event.seq} funding for {key[1]}", "no open position", f"{amount:.6f} USD")
            )
            continue
        venue.cash += amount
        position.funding_usd += amount
        position.last_funding_ts += periods * period_seconds


def replay(journal: Journal, config: Config) -> Replay:
    """Rebuild the book from every event in one journal."""
    return replay_events(journal.events(), config)


def replay_events(events: Iterable[Event], config: Config) -> Replay:
    """Rebuild the book from an event stream; :func:`replay` is this over a journal."""
    stream = tuple(events)
    opening, opening_note = _opening(config, stream)
    venue = PaperVenue(**opening)
    notes: List[str] = [] if opening_note is None else [opening_note]
    ignored: List[str] = []
    disagreements: List[Divergence] = []

    for event in stream:
        if event.kind == "order.filled":
            venue.apply_fill(_contract(Fill, event))
        elif event.kind == "trade.closed":
            trade = _contract(ClosedTrade, event)
            try:
                net = venue.apply_close(trade)
            except KeyError as error:
                raise ReplayError(f"line {event.seq} (trade.closed): {error}") from error
            if not _close(net, trade.realized_usd, 1e-9):
                disagreements.append(
                    Divergence(f"line {event.seq} {trade.symbol} realized_usd", trade.realized_usd, net)
                )
        elif event.kind == "funding.accrued":
            _apply_funding(venue, event, disagreements)
        elif event.kind == "day.rolled":
            try:
                venue.day_start_equity = float(event.payload["day_start_equity"])
                venue.day_start_ts = float(event.payload["ts"])
            except (KeyError, TypeError, ValueError) as error:
                raise ReplayError(f"line {event.seq} (day.rolled): {error}") from error
        elif event.kind not in QUIET_KINDS:
            ignored.append(event.kind)

    if ignored:
        notes.append(
            "the replay does not know kind(s) "
            + ", ".join(sorted(set(ignored)))
            + ": the book is the part this reader understands"
        )
    return Replay(
        venue=venue,
        events=len(stream),
        ignored=tuple(sorted(set(ignored))),
        notes=tuple(notes),
        disagreements=tuple(disagreements),
    )


def compare(cached: PaperVenue, replayed: PaperVenue, *, rel: float = 1e-9) -> Tuple[Divergence, ...]:
    """Every field where the two accounts disagree; empty means they agree.

    Paired field by field rather than by hashing the state: a report that says
    ``qty: cache +0.0028, journal 0`` is a bug report, while ``state mismatch``
    is a shrug.
    """
    out: List[Divergence] = []
    if not _close(cached.cash, replayed.cash, rel):
        out.append(Divergence("cash", cached.cash, replayed.cash))
    if not _close(cached.day_start_equity, replayed.day_start_equity, rel):
        out.append(Divergence("day_start_equity", cached.day_start_equity, replayed.day_start_equity))
    if not _close(cached.day_start_ts, replayed.day_start_ts, 1e-12):
        out.append(Divergence("day_start_ts", cached.day_start_ts, replayed.day_start_ts))
    cached_keys, replayed_keys = set(cached.positions), set(replayed.positions)
    for key in sorted(cached_keys - replayed_keys):
        out.append(Divergence(f"position {_label(key)}", "open in the cache", "absent from the journal"))
    for key in sorted(replayed_keys - cached_keys):
        out.append(Divergence(f"position {_label(key)}", "absent from the cache", "open in the journal"))
    for key in sorted(cached_keys & replayed_keys):
        left, right = cached.positions[key], replayed.positions[key]
        for name, cached_value, replayed_value, tolerance in (
            ("qty", left.qty, right.qty, rel),
            ("avg_price", left.avg_price, right.avg_price, rel),
            ("margin_usd", left.margin_usd, right.margin_usd, rel),
            ("entry_fee_usd", left.entry_fee_usd, right.entry_fee_usd, rel),
            ("cost_usd", left.cost_usd, right.cost_usd, rel),
            ("funding_usd", left.funding_usd, right.funding_usd, rel),
            ("last_funding_ts", left.last_funding_ts, right.last_funding_ts, 1e-12),
        ):
            if not _close(cached_value, replayed_value, tolerance):
                out.append(Divergence(f"{_label(key)} {name}", cached_value, replayed_value))
    if len(cached.closed) != len(replayed.closed):
        out.append(Divergence("closed trades", len(cached.closed), len(replayed.closed)))
    else:
        for index, (left, right) in enumerate(zip(cached.closed, replayed.closed)):
            if not _close(left.realized_usd, right.realized_usd, rel):
                out.append(Divergence(f"closed[{index}] realized_usd", left.realized_usd, right.realized_usd))
            if not _close(left.fees_usd, right.fees_usd, rel):
                out.append(Divergence(f"closed[{index}] fees_usd", left.fees_usd, right.fees_usd))
    return tuple(out)


def reconcile(journal: Journal, config: Config, cached: Optional[PaperVenue]) -> ReconcileReport:
    """Decide what the book is: the cache if it agrees, the chain if it is gone.

    Three outcomes, three honest sentences:

    * the cache agrees with the chain — trade, and say ``ok``;
    * there is no cache (a first run, or a lost file) — rebuild from the chain
      and let the caller journal ``state.rebuilt``;
    * they disagree — refuse.  A book that cannot decide what it owns must not
      open a new position, and silently preferring the more convenient of the two
      accounts is how a phantom position is born.
    """
    replayed = replay(journal, config)
    if cached is None:
        divergences = replayed.disagreements
        mode = "diverged" if divergences else ("rebuilt" if replayed.events else "fresh")
        notes = list(replayed.notes)
        if mode == "rebuilt":
            notes.append(f"no state cache: the book was rebuilt from {replayed.events} journal event(s)")
        return ReconcileReport(
            mode=mode,
            book=replayed.venue,
            events=replayed.events,
            divergences=divergences,
            ignored=replayed.ignored,
            notes=tuple(notes),
        )
    divergences = compare(cached, replayed.venue) + replayed.disagreements
    return ReconcileReport(
        mode="diverged" if divergences else "ok",
        book=cached,
        events=replayed.events,
        divergences=divergences,
        ignored=replayed.ignored,
        notes=replayed.notes,
    )
