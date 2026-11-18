"""One cycle: observe, record, judge, size, act, autopsy — and write it all down.

The order is the design
=======================

1. **Verify the journal** before anything else.  If the record of the past has
   been edited, the process does not get to add to it and call the result a
   history: the kill switch trips and the cycle aborts.  This is the one check
   that must come first, because every later line inherits its credibility.
2. **Accrue funding** for positions opened in earlier cycles.  Funding is the
   reason the first strategy exists; skipping it on a quiet cycle would make the
   book look better than it is.
3. **Roll the day** and mark the book.  An unpriced position stops the cycle —
   a book that cannot be valued cannot be risk-checked.
4. **Check the data's own honesty** (stale quotes, a venue that disagrees with
   the cross-section) and **read coherence** with its null.  Neither is a
   signal; both are recorded, and the truth check is a veto on the legs it
   names.
5. **Scan the strategy**, then for each intent: kill switch → truth veto → risk
   limits → venue.  Every one of those steps can refuse, and every refusal is
   journaled with its code and sentence.  The refusals are the dataset the
   monster will learn from.
6. **Close what has reached its horizon**, mark again, and record the autopsy.

What a cycle deliberately does not do
=====================================

It does not retrain, re-tune a threshold, or change a limit — those are
offline acts with their own pre-registration, and a loop that can edit its own
risk parameters is a loop that will.  It also does not place a real order: the
hands are ``ENRG-HFT``'s, and this package hands it an ``Intent``, not a
credential.

The paper book's state lives in a JSON cache beside the journal.  The journal is
authoritative for *why* and for *what*: before a cycle trades it replays the
chain (``monster.replay``) and compares the result with the cache.  A cache that
agrees is used; a missing cache is refilled from the chain; a cache that
disagrees stops the cycle and writes ``state.diverged``.  The cache is a
convenience for speed, and a convenience that cannot be checked is a second
opinion about the book — which is exactly one too many.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from monster import SCHEMA, __version__
from monster.autopsy import summary as autopsy_summary
from monster.brain.phase import CoherenceReading, phase_coherence
from monster.brain.truth import TruthReport, cross_venue_truth
from monster.config import Config
from monster.contracts import ClosedTrade, Snapshot
from monster.exec.paper import FundingReport, PaperVenue, Unpriced
from monster.journal import Journal, JournalError
from monster.replay import reconcile
from monster.risk.limits import KillSwitch, check
from monster.strategies.carry import CarryScan, scan_carry

__all__ = [
    "Decision",
    "CycleReport",
    "run_cycle",
    "load_state",
    "save_state",
    "main",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_DIVERGED",
    "EXIT_NOT_READY",
    "EXIT_STALE",
    "EXIT_USAGE",
]

#: What an exit code means.  The cycle runs unattended, so this number is the only
#: sentence a timer can read — and a number that means three things teaches the
#: operator to ignore all three.  Exactly one code says "wait":
#:
#: ``0`` a cycle ran: it traded, or it declined and said why;
#: ``1`` the beast says no: the kill switch is tripped, or the chain does not verify;
#: ``2`` the accounts disagree, or the store cannot be read — a human must look;
#: ``3`` too little history yet: the store is younger than the coherence window;
#: ``4`` the freshest capture pass is older than the limit — the collector is behind;
#: ``64`` the command itself was wrong; nothing was attempted.
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_DIVERGED = 2
EXIT_NOT_READY = 3
EXIT_STALE = 4
EXIT_USAGE = 64


@dataclass(frozen=True)
class Decision:
    """What happened to one intent, and the codes that decided it."""

    intent_id: str
    strategy: str
    outcome: str
    codes: Tuple[str, ...] = ()
    reasons: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "strategy": self.strategy,
            "outcome": self.outcome,
            "codes": list(self.codes),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class CycleReport:
    """Everything one cycle observed and decided — printable, journalable."""

    ts: float
    mode: str
    verdict: str
    equity_usd: float
    positions: int
    killed: bool
    truth: Optional[TruthReport] = None
    coherence: Optional[CoherenceReading] = None
    scan: Optional[CarryScan] = None
    funding: Optional[FundingReport] = None
    decisions: Tuple[Decision, ...] = ()
    closed: Tuple[ClosedTrade, ...] = ()
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "mode": self.mode,
            "verdict": self.verdict,
            "equity_usd": self.equity_usd,
            "positions": self.positions,
            "killed": self.killed,
            "truth": None if self.truth is None else self.truth.to_dict(),
            "coherence": None if self.coherence is None else self.coherence.to_dict(),
            "scan": None if self.scan is None else self.scan.to_dict(),
            "funding": None if self.funding is None else self.funding.to_dict(),
            "decisions": [decision.to_dict() for decision in self.decisions],
            "closed": [trade.to_dict() for trade in self.closed],
            "notes": list(self.notes),
            "schema": SCHEMA,
            "version": __version__,
        }

    def describe(self) -> str:
        lines = [
            f"monster {__version__} · {self.mode} · journal {self.verdict}",
            f"book: equity {self.equity_usd:.2f} USD, {self.positions} position(s)"
            + (" · KILL SWITCH TRIPPED" if self.killed else ""),
        ]
        if self.truth is not None:
            lines.append(self.truth.describe())
        if self.coherence is not None:
            lines.append(self.coherence.describe())
        if self.funding is not None and (self.funding.accruals or self.funding.unmeasured):
            lines.append(f"funding: {self.funding.total_usd:+.4f} USD")
            for reason in self.funding.unmeasured:
                lines.append(f"  {reason}")
        if self.scan is not None:
            lines.extend(self.scan.describe().splitlines())
        for decision in self.decisions:
            code = ",".join(decision.codes) if decision.codes else "ok"
            lines.append(f"  [{code}] {decision.outcome} {decision.intent_id} ({decision.strategy})")
            for reason in decision.reasons:
                lines.append(f"      {reason}")
        for trade in self.closed:
            lines.append(
                f"  closed {trade.symbol} {trade.kind}: {trade.bps:+.2f} bps, "
                f"{trade.realized_usd:+.4f} USD ({trade.reason})"
            )
        for note in self.notes:
            lines.append(f"note: {note}")
        return "\n".join(lines)


def save_state(path: str | os.PathLike[str], venue: PaperVenue, kill: KillSwitch) -> None:
    """Write the cache atomically: a half-written state file is a lost book."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": SCHEMA, "venue": venue.state(), "kill": kill.to_dict()}
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def load_state(path: str | os.PathLike[str], config: Config) -> Tuple[PaperVenue, KillSwitch]:
    """Load the cache, or start a paper book from the configured capital."""
    target = Path(path)
    if not target.exists():
        venue = PaperVenue(
            cash_usd=config.capital_usd,
            fee_bps=config.fee_bps,
            slippage_bps=config.slippage_bps,
            leverage=config.leverage,
        )
        return venue, KillSwitch()
    document = json.loads(target.read_text(encoding="utf-8"))
    if int(document.get("schema", 0)) != SCHEMA:
        raise ValueError(
            f"{target}: state schema {document.get('schema')!r} is not {SCHEMA}; refill from the journal"
        )
    return PaperVenue.from_state(document["venue"]), KillSwitch.from_dict(document.get("kill", {}))


def run_cycle(
    config: Config,
    snapshot: Snapshot,
    *,
    journal: Journal,
    venue: PaperVenue,
    kill: KillSwitch,
    history: Optional[Mapping[str, Sequence[float]]] = None,
    notes: Sequence[str] = (),
) -> CycleReport:
    """Run one observe→record→judge→size→act→autopsy cycle on a frozen snapshot."""
    stamp = float(snapshot.ts)
    notes: list[str] = list(notes)

    verdict = journal.verify()
    if verdict != "ok":
        kill.trip(f"journal verification failed: {verdict}", ts=stamp)
        journal.append("kill.tripped", {"reason": kill.reason, "ts": stamp}, ts=stamp)
        journal.append("cycle.aborted", {"verdict": verdict}, ts=stamp)
        return CycleReport(
            ts=stamp,
            mode=config.mode,
            verdict=verdict,
            equity_usd=venue.cash,
            positions=len(venue.positions),
            killed=True,
            notes=(f"journal: {verdict}", "equity is cash only: a compromised journal stops the cycle before marking"),
        )

    journal.append(
        "cycle.started",
        {"ts": stamp, "mode": config.mode, "config": config.to_dict(), "source": snapshot.source},
        ts=stamp,
    )

    funding: Optional[FundingReport] = None
    try:
        funding = venue.accrue_funding(snapshot)
        journal.append("funding.accrued", funding.to_dict(), ts=stamp)
    except Unpriced as error:
        notes.append(f"funding unmeasured: {error}")
        journal.append("funding.unmeasured", {"reason": str(error)}, ts=stamp)

    if venue.roll_day(snapshot):
        journal.append(
            "day.rolled",
            {"ts": stamp, "day_start_equity": venue.day_start_equity},
            ts=stamp,
        )

    try:
        exposure = venue.exposure(snapshot)
    except Unpriced as error:
        journal.append("cycle.aborted", {"reason": str(error)}, ts=stamp)
        return CycleReport(
            ts=stamp,
            mode=config.mode,
            verdict=verdict,
            equity_usd=venue.cash,
            positions=len(venue.positions),
            killed=kill.tripped,
            funding=funding,
            notes=tuple(notes + [f"unpriced book: {error}"]),
        )
    journal.append("equity.mark", exposure.to_dict(), ts=stamp)

    # The truth check only sees the venues the operator allowed to witness the
    # trading venue.  `Config` refuses a trading venue outside that list, so the
    # filter can only ever remove a witness, never the subject.
    witnesses = tuple(quote for quote in snapshot.quotes if quote.venue in config.truth_venues)
    truth_input = snapshot
    if len(witnesses) != len(snapshot.quotes):
        truth_input = Snapshot(ts=snapshot.ts, quotes=witnesses, source=snapshot.source)
    truth = cross_venue_truth(truth_input)
    journal.append("truth.read", truth.to_dict(), ts=stamp)

    coherence: Optional[CoherenceReading] = None
    if history is not None:
        try:
            coherence = phase_coherence(
                history,
                ts=stamp,
                window=config.coherence_window,
                surrogates=config.surrogates,
                seed=config.seed,
            )
            journal.append("coherence.read", coherence.to_dict(), ts=stamp)
        except ValueError as error:
            notes.append(f"coherence unmeasured: {error}")
            journal.append("coherence.unmeasured", {"reason": str(error)}, ts=stamp)
    else:
        notes.append("coherence unmeasured: no price history was supplied with the snapshot")
        journal.append(
            "coherence.unmeasured",
            {"reason": "no price history was supplied with the snapshot"},
            ts=stamp,
        )
    return _decide(
        config=config,
        snapshot=snapshot,
        journal=journal,
        venue=venue,
        kill=kill,
        exposure=exposure,
        truth=truth,
        coherence=coherence,
        funding=funding,
        verdict=verdict,
        notes=notes,
    )


def _decide(
    *,
    config: Config,
    snapshot: Snapshot,
    journal: Journal,
    venue: PaperVenue,
    kill: KillSwitch,
    exposure,
    truth: TruthReport,
    coherence: Optional[CoherenceReading],
    funding: Optional[FundingReport],
    verdict: str,
    notes: Sequence[str],
) -> CycleReport:
    """Scan, then put every intent through the same four gates, in order."""
    stamp = float(snapshot.ts)
    scan = scan_carry(
        snapshot,
        venue=config.venue,
        notional_usd=config.notional_usd,
        horizon_hours=config.horizon_hours,
        fee_bps=config.fee_bps,
        slippage_bps=config.slippage_bps,
        min_edge_bps=config.min_edge_bps,
    )
    journal.append("carry.scan", scan.to_dict(), ts=stamp)

    decisions: list[Decision] = []
    for intent in scan.intents:
        journal.append("intent.proposed", intent.to_dict(), ts=stamp)
        if kill.tripped:
            decision = Decision(intent.id, intent.strategy, "refused", ("KILLED",), (kill.reason or "kill switch",))
        else:
            flags = []
            for leg in intent.legs:
                finding = truth.flagged(leg.venue, leg.symbol)
                if finding is not None:
                    flags.append(f"{finding.venue} {finding.symbol}: {finding.reason}")
            if flags:
                decision = Decision(intent.id, intent.strategy, "refused", ("TRUTH_FLAG",), tuple(flags))
            elif any((leg.venue, leg.symbol, leg.kind) in venue.positions for leg in intent.legs):
                # A carry is a position, not a subscription: re-proposing the
                # same pair every cycle must not average up the book forever.
                decision = Decision(
                    intent.id,
                    intent.strategy,
                    "refused",
                    ("ALREADY_OPEN",),
                    ("a leg of this intent is already open; the horizon, not the scan, decides when it ends",),
                )
            else:
                breaches = check(intent, exposure, config.limits)
                if breaches:
                    decision = Decision(
                        intent.id,
                        intent.strategy,
                        "refused",
                        tuple(breach.code for breach in breaches),
                        tuple(breach.detail for breach in breaches),
                    )
                else:
                    result = venue.execute(intent, snapshot)
                    if not result.executed:
                        decision = Decision(intent.id, intent.strategy, "refused", ("VENUE",), result.refused)
                    else:
                        for fill in result.fills:
                            journal.append("order.filled", fill.to_dict(), ts=stamp)
                        journal.append(
                            "position.opened",
                            {
                                "intent_id": intent.id,
                                "strategy": intent.strategy,
                                "legs": [leg.to_dict() for leg in intent.legs],
                                "notional_usd": intent.gross_usd,
                            },
                            ts=stamp,
                        )
                        decision = Decision(intent.id, intent.strategy, "filled")
                        exposure = venue.exposure(snapshot)
        if decision.outcome == "refused":
            journal.append("intent.refused", decision.to_dict(), ts=stamp)
        decisions.append(decision)

    closed: list[ClosedTrade] = []
    notes = list(notes)
    for key, position in sorted(venue.positions.items()):
        if stamp - position.opened_ts < config.close_after_hours * 3600.0:
            continue
        try:
            trade = venue.close(key, snapshot, reason="horizon", ts=stamp)
        except Unpriced as error:
            notes.append(f"close unmeasured: {error}")
            journal.append("close.unmeasured", {"position": list(key), "reason": str(error)}, ts=stamp)
            continue
        journal.append("trade.closed", trade.to_dict(), ts=stamp)
        closed.append(trade)

    try:
        exposure = venue.exposure(snapshot)
    except Unpriced as error:
        notes.append(f"final mark unmeasured: {error}")

    if not kill.tripped and exposure.day_pnl_usd <= -config.limits.max_daily_loss_usd:
        kill.trip(
            f"day pnl {exposure.day_pnl_usd:+.2f} USD reached the budget "
            f"-{config.limits.max_daily_loss_usd:.2f} USD",
            ts=stamp,
        )
        journal.append("kill.tripped", {"reason": kill.reason, "ts": stamp}, ts=stamp)

    journal.append("autopsy.summary", autopsy_summary(venue.closed), ts=stamp)
    journal.append(
        "cycle.finished",
        {
            "equity_usd": exposure.equity_usd,
            "cash_usd": venue.cash,
            "positions": len(venue.positions),
            "filled": sum(1 for decision in decisions if decision.outcome == "filled"),
            "refused": sum(1 for decision in decisions if decision.outcome == "refused"),
            "closed": len(closed),
            "killed": kill.tripped,
        },
        ts=stamp,
    )

    return CycleReport(
        ts=stamp,
        mode=config.mode,
        verdict=verdict,
        equity_usd=exposure.equity_usd,
        positions=len(venue.positions),
        killed=kill.tripped,
        truth=truth,
        coherence=coherence,
        scan=scan,
        funding=funding,
        decisions=tuple(decisions),
        closed=tuple(closed),
        notes=tuple(notes),
    )


def _link_capture_chain(config: Config, journal: Journal, *, ts: float) -> None:
    """Write the head of the data collector's chain into the trading chain.

    The collector keeps its own journal because two writers on one chain break it
    (``Journal.append`` refuses such a file outright).  The link is what keeps
    the split honest: every cycle records which version of the capture history it
    was reading, so a capture file edited afterwards no longer matches what the
    book says it saw.  A checkout with no collector has nothing to link, which is
    not an error.
    """
    path = config.data_journal_path
    if path is None or not Path(path).exists() or journal.verify() != "ok":
        return
    try:
        capture = Journal(path, fsync=False)
        verdict, head, events = capture.verify(), capture.last(), capture.count()
    except JournalError as error:
        journal.append(
            "journal.linked",
            {"chain": "capture", "path": str(path), "verdict": str(error), "head": None, "seq": None, "events": None},
            ts=ts,
        )
        return
    journal.append(
        "journal.linked",
        {
            "chain": "capture",
            "path": str(path),
            "verdict": verdict,
            "head": None if head is None else head.hash,
            "seq": None if head is None else head.seq,
            "events": events,
        },
        ts=ts,
    )


def _replay_command(
    args: argparse.Namespace,
    journal: Journal,
    journal_path: Path,
    state_path: Path,
    config: Config,
) -> int:
    """``--replay``: print the book the journal describes, and compare it."""
    verdict = journal.verify()
    if verdict != "ok":
        print(f"{journal_path}: {verdict} — the journal cannot be replayed", file=sys.stderr)
        return EXIT_REFUSED
    cached = load_state(state_path, config)[0] if state_path.exists() else None
    outcome = reconcile(journal, config, cached)
    print(outcome.describe())
    if not args.write_state:
        return EXIT_DIVERGED if outcome.diverged else EXIT_OK
    if outcome.diverged:
        print(
            "refusing to overwrite the cache: the two accounts disagree and this command cannot "
            "prove which is right — move the cache aside to adopt the journal's book",
            file=sys.stderr,
        )
        return EXIT_DIVERGED
    kill = load_state(state_path, config)[1] if state_path.exists() else KillSwitch()
    save_state(state_path, outcome.book, kill)
    if outcome.mode != "ok":
        journal.append("state.rebuilt", outcome.to_dict())
    print(f"{state_path}: written from the journal ({outcome.events} events)")
    return EXIT_OK


def _first_history_note(notes: Sequence[str]) -> str:
    """The derivation's own sentence about how far the store reaches, if it has one."""
    for note in notes:
        if note.startswith("history:"):
            return f" ({note})"
    return ""


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI: ``python -m monster.cycle (--snapshot file | --store dir) [--history file]``."""
    parser = argparse.ArgumentParser(
        prog="monster",
        description="ENRG Monster — one paper cycle over a frozen snapshot, with a hash-chained journal",
    )
    parser.add_argument("--snapshot", help="snapshot JSON: quotes + funding, frozen at one timestamp")
    parser.add_argument("--history", help="price history JSON {symbol: [prices...]} for the coherence reading")
    parser.add_argument("--store", help="data store root: derive the snapshot and history as of --at")
    parser.add_argument("--at", type=float, help="as-of timestamp for --store (default: the newest quote)")
    parser.add_argument("--interval", type=float, help="bar interval in seconds for the --store history")
    parser.add_argument("--bars", type=int, help="history bars for --store (default: the coherence window)")
    parser.add_argument("--journal", help="journal path (overrides MONSTER_JOURNAL)")
    parser.add_argument("--state", help="state cache path (overrides MONSTER_STATE)")
    parser.add_argument(
        "--replay",
        action="store_true",
        help="rebuild the book from the journal and compare it with the state cache",
    )
    parser.add_argument(
        "--write-state",
        action="store_true",
        help="with --replay: adopt the journal's book as the new cache",
    )
    parser.add_argument(
        "--max-snapshot-age",
        type=float,
        metavar="SECONDS",
        help="--store without --at: refuse a pass older than this (default MONSTER_MAX_SNAPSHOT_AGE_S; 0 disables)",
    )
    parser.add_argument("--verify", action="store_true", help="verify this chain and the chains it links, then exit")
    parser.add_argument(
        "--no-links",
        action="store_true",
        help="with --verify: check only this chain, not the chains it links",
    )
    parser.add_argument("--reset-kill", metavar="OPERATOR", help="clear the kill switch, naming the human doing it")
    parser.add_argument("--json", action="store_true", help="print the cycle report as JSON")
    args = parser.parse_args(argv)

    config = Config.from_env()
    journal_path = Path(args.journal).expanduser() if args.journal else config.journal_path
    state_path = Path(args.state).expanduser() if args.state else config.state_path
    try:
        journal = Journal(journal_path)
    except JournalError as error:
        print(f"journal: {error}", file=sys.stderr)
        return EXIT_REFUSED

    if args.verify:
        verdict = journal.verify()
        print(f"{journal_path}: {verdict} ({journal.count()} events)")
        if verdict != "ok":
            return EXIT_REFUSED
        if args.no_links:
            return EXIT_OK
        # The chain proves this file was not edited.  The links prove the *other*
        # files were not edited after this one read them: a cycle that recorded
        # "the capture chain had 130 lines, head 4f1a..." is making a claim, and
        # a claim nobody checks is a comment.
        links = journal.verify_links()
        for report in links:
            print(report.describe(), file=sys.stdout if report.ok else sys.stderr)
        broken = [report for report in links if not report.ok]
        if broken:
            print(
                f"links: {len(broken)} of {len(links)} do not hold — a chain this book read from was "
                "changed after it read from it; the past this book describes is not the past on disk",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        if links:
            print(f"links: {len(links)} ok")
        return EXIT_OK

    if args.replay:
        return _replay_command(args, journal, journal_path, state_path, config)

    if args.write_state:
        print("--write-state only makes sense with --replay", file=sys.stderr)
        return EXIT_USAGE

    venue, kill = load_state(state_path, config)
    if args.reset_kill:
        kill.reset(args.reset_kill)
        save_state(state_path, venue, kill)
        journal.append("kill.reset", {"operator": args.reset_kill})
        print(f"kill switch: {kill.reason}")
        return EXIT_OK

    if not args.snapshot and not args.store:
        print(f"mode={config.mode} venue={config.venue} capital={config.capital_usd:.2f} USD")
        print(f"journal={journal_path} ({journal.count()} events, {journal.verify()})")
        print("nothing to do: pass --snapshot or --store to run a cycle", file=sys.stderr)
        return EXIT_USAGE

    # The cache is a convenience; the chain is the book.  A cache that disagrees
    # is refused rather than averaged, and a cache that has gone missing is
    # refilled from the journal: a phantom position is a disagreement nobody
    # asked about, and this is the question that catches it.
    outcome = reconcile(journal, config, venue if state_path.exists() else None)
    if outcome.diverged:
        journal.append("state.diverged", outcome.to_dict())
        print(outcome.describe(), file=sys.stderr)
        print(
            "the state cache and the journal describe different books; nothing was traded. "
            "Move the cache aside to trade the journal's book, or investigate the divergence.",
            file=sys.stderr,
        )
        return EXIT_DIVERGED
    if outcome.mode == "rebuilt":
        journal.append("state.rebuilt", outcome.to_dict())
        save_state(state_path, outcome.book, kill)
        print(f"state: rebuilt from the journal ({outcome.events} events)", file=sys.stderr)
    venue = outcome.book

    data_notes: Tuple[str, ...] = ()
    if args.store:
        if args.snapshot or args.history:
            print("--store derives the snapshot and history itself; do not pass --snapshot/--history", file=sys.stderr)
            return EXIT_USAGE
        from monster.data.derive import DataError, derive
        from monster.data.store import Store, StoreError

        try:
            derivation = derive(
                Store(args.store),
                at=args.at,
                interval_s=args.interval or float(config.interval_s),
                bars=args.bars or config.coherence_window,
                quote_max_age_s=config.quote_max_age_s,
                funding_max_age_h=config.funding_max_age_h,
            )
        except (DataError, StoreError) as error:
            # The store's own reason decides the code: "not yet" and "the file is
            # unreadable" are the same sentence to a human and different sentences
            # to a timer, and only one of them should be allowed to stay quiet.
            code = getattr(error, "code", "bad-data")
            print(f"data: {error}", file=sys.stderr)
            if code == "not-ready":
                return EXIT_NOT_READY
            if code == "stale":
                return EXIT_STALE
            return EXIT_DIVERGED
        snapshot = derivation.snapshot
        history = derivation.history
        data_notes = derivation.notes
        # A pass that stopped three intervals ago is not a picture of now, and a
        # decision on it would be about the past.  This checks only the runs that
        # mean "as of now": an explicit --at is a replay of a chosen moment, and
        # freshness is then enforced per quote by derive.
        limit = config.snapshot_max_age_s if args.max_snapshot_age is None else float(args.max_snapshot_age)
        if args.at is None and limit > 0.0:
            age = time.time() - snapshot.ts
            if age > limit:
                if journal.verify() == "ok":
                    journal.append(
                        "cycle.stale",
                        {"age_s": age, "limit_s": limit, "at": snapshot.ts},
                        ts=snapshot.ts,
                    )
                print(
                    f"data: the freshest capture pass is {age:.0f}s old (limit {limit:.0f}s): "
                    "the collector is behind, and a decision on this data would be about the past",
                    file=sys.stderr,
                )
                return EXIT_STALE
            data_notes = tuple(data_notes) + (f"capture pass age at decision time: {age:.1f}s",)
        # The provenance is written before the decision, and only into a journal
        # that still verifies: a broken past gets no new lines from this process.
        if journal.verify() == "ok":
            journal.append("data.derived", derivation.to_dict(), ts=snapshot.ts)
        # A store that cannot fill the coherence window is not a store to trade on:
        # the physics reading would be unmeasured, the report would say so only in a
        # note, and the beast would trade on carry alone.  This is the normal shape
        # of the first two hours after a collector starts, so it gets its own exit
        # code — the one code a timer is allowed to treat as "wait".
        window = int(config.coherence_window)
        available = min((len(series) for series in history.values()), default=0)
        requested = args.bars or window
        if available < window:
            if journal.verify() == "ok":
                journal.append(
                    "cycle.not_ready",
                    {
                        "bars": available,
                        "window": window,
                        "bars_requested": int(requested),
                        "interval_s": args.interval or float(config.interval_s),
                        "at": snapshot.ts,
                        "notes": list(data_notes),
                    },
                    ts=snapshot.ts,
                )
            print(
                f"data: the store holds {available} aligned bar(s) of the {window} the coherence "
                f"window needs{_first_history_note(data_notes)}; a young store is waited for, "
                "not traded on",
                file=sys.stderr,
            )
            return EXIT_NOT_READY
    else:
        snapshot = Snapshot.from_dict(
            json.loads(Path(str(args.snapshot)).expanduser().read_text(encoding="utf-8"))
        )
        history = None
        if args.history:
            history = json.loads(Path(args.history).expanduser().read_text(encoding="utf-8"))

    # Two chains, tied together before anything is decided: the capture journal
    # says what the world looked like, and this line says which version of it the
    # book was reading.
    try:
        _link_capture_chain(config, journal, ts=snapshot.ts)
        report = run_cycle(
            config, snapshot, journal=journal, venue=venue, kill=kill, history=history, notes=data_notes
        )
    except JournalError as error:
        print(f"journal: {error}", file=sys.stderr)
        return EXIT_REFUSED
    save_state(state_path, venue, kill)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True) if args.json else report.describe())
    return EXIT_OK if not report.killed else EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover - exercised through ``python -m``
    raise SystemExit(main())
