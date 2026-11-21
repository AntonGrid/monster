"""``python -m monster.data`` — probe, capture, replay, derive, audit, gaps.

The commands are deliberately small and composable, because the VPS will run
them from cron:

``probe``    asks every endpoint once and says what answered and what it said;
``capture``  one pass (or ``--count`` passes) from a file transport or the live
             venues; writes raw bodies and normalized rows, prints failures, and
             journals its passes into its **own** chain
             (``var/journal.capture.jsonl``), because two writers on one chain
             break it;
``tape``     writes a synthetic tape for offline runs (labelled synthetic);
``derive``   prints (and optionally saves) the snapshot and history a cycle
             would see at ``--at``;
``audit``    re-parses a day of raw bodies and compares with the store;
``gaps``     missing slots and coverage for a window;
``truth-report`` replays the truth check over a day and classifies its findings
             by what the next pass did — the instrument for the false-positive
             gate, which cannot be measured at the moment of a refusal;
``doctor``   one screen over everything above, judged at this minute: store,
             freshness, coverage, gaps, the capture chain, whether the cycle can
             derive a snapshot, what the truth check flags, and the disk.  It
             writes nothing, so it is safe to run while the collector appends,
             and it separates ``wait`` (a young store) from ``warn``/``fail``
             (a human should look).

Every command returns a process exit code: 0 when the answer is clean, 1 when
it is not, 2 when the question could not be asked.  A cron job that ignores
stdout still learns something.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

from monster.data.capture import Capturer, FileTransport, UrllibTransport, endpoints
from monster.data.derive import DataError, derive
from monster.data.doctor import diagnose
from monster.data.probe import probe as probe_endpoints
from monster.data.quality import audit_day, coverage, find_gaps, unparsable
from monster.data.store import Store, StoreError
from monster.data.synthetic import write_demo_tape
from monster.data.truth_report import truth_day
from monster.journal import Journal, JournalError

__all__ = ["main"]


def _split(text: str) -> tuple:
    return tuple(part.strip() for part in str(text).split(",") if part.strip())


def _sleep_until(target: float, *, clock=time.time, sleep=time.sleep) -> float:
    """Sleep until ``target``; a target already in the past returns at once.

    The capture loop wakes on an absolute grid instead of sleeping a fixed
    interval, because a pass that takes two seconds would otherwise move every
    subsequent sample two seconds later, and a week of that turns a 60-second
    cadence into a cadence with missing minutes — a self-inflicted violation of
    the coverage gate.
    """
    delay = target - clock()
    if delay <= 0.0:
        return 0.0
    sleep(delay)
    return delay


def _probe(args: argparse.Namespace) -> int:
    transport = FileTransport(args.fixtures) if args.fixtures else UrllibTransport(timeout=args.timeout)
    results = probe_endpoints(transport, assets=_split(args.assets), venues=_split(args.venues))
    if args.json:
        print(json.dumps([item.to_dict() for item in results], indent=2, sort_keys=True))
    else:
        for item in results:
            print(item.describe())
        failed = [item for item in results if not item.ok]
        summary = f"probe: {len(results) - len(failed)}/{len(results)} endpoint(s) answered and parsed"
        if failed:
            summary += " | not usable: " + ", ".join(
                f"{item.endpoint.venue} {item.endpoint.name}" for item in failed
            )
        print(summary)
    return 0 if all(item.ok for item in results) else 1


def _capture(args: argparse.Namespace) -> int:
    interval = float(args.interval)
    if args.live and interval <= 0.0:
        raise ValueError("live capture needs --interval > 0")
    store = Store(args.store, fsync=args.fsync)
    transport = FileTransport(args.fixtures) if args.fixtures else UrllibTransport(timeout=args.timeout)
    capturer = Capturer(store, transport, retries=args.retries, workers=args.workers)
    wanted = endpoints(assets=_split(args.assets), venues=_split(args.venues))
    journal = Journal(args.journal) if args.journal else None
    journal_ok = journal is None or journal.verify() == "ok"
    if journal is not None and not journal_ok:
        print("data: the journal fails verification; this run will not append to it", file=sys.stderr)
    journal_error: Optional[str] = None
    # A replay is stamped by the grid it replays; a live pass is stamped by the
    # receive clock and merely *woken* by the grid, because the raw body's job
    # is to say when we received it, not when we meant to.
    grid = float(args.at) if args.at is not None else time.time()
    passes = max(1, int(args.count))
    reports = []
    for index in range(passes):
        stamp = time.time() if args.live else grid + index * interval
        report = capturer.capture(at=stamp, wanted=wanted)
        reports.append(report)
        if journal is not None and journal_ok:
            try:
                journal.append("data.captured", report.to_dict(), ts=stamp)
            except JournalError as error:
                # The data matters more than the record of it: a collector that
                # dies because somebody shares its journal stops collecting, and
                # then the store has a gap *and* the journal has a hole.  Say so
                # once, keep capturing, and exit non-zero at the end.
                journal_ok, journal_error = False, str(error)
                print(f"data: journal disabled: {error}", file=sys.stderr)
        if not args.json:
            print(report.describe())
        if args.live and index + 1 < passes:
            next_at = grid + (index + 1) * interval
            if time.time() - next_at > interval:
                # More than one whole slot late: the missed slots are gaps, not
                # a backlog, so re-anchor the grid instead of firing a burst of
                # passes that pretend the interval was never missed.
                grid = time.time()
                next_at = grid
            _sleep_until(next_at)
    if args.json:
        print(json.dumps([report.to_dict() for report in reports], indent=2, sort_keys=True))
    if journal_error is not None:
        print(f"data: the journal was not written: {journal_error}", file=sys.stderr)
        return 1
    return 0 if all(report.ok for report in reports) else 1


def _tape(args: argparse.Namespace) -> int:
    tape = write_demo_tape(
        args.root,
        assets=_split(args.assets),
        samples=args.samples,
        interval_s=args.interval,
        base_ts=args.base,
        seed=args.seed,
    )
    print(tape.describe())
    print(
        "next: python -m monster.data capture "
        f"--fixtures {tape.root} --store {args.store} "
        f"--count {tape.samples} --interval {tape.interval_s:g} --at {tape.base_ts:.0f}"
    )
    return 0


def _derive(args: argparse.Namespace) -> int:
    store = Store(args.store)
    try:
        derivation = derive(
            store,
            at=args.at,
            interval_s=args.interval,
            bars=args.bars,
            quote_max_age_s=args.max_quote_age,
            funding_max_age_h=args.max_funding_age,
        )
    except (DataError, StoreError) as error:
        print(f"data: {error}", file=sys.stderr)
        return 2
    if args.snapshot_out:
        target = Path(args.snapshot_out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(derivation.snapshot.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    if args.history_out:
        target = Path(args.history_out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(derivation.history_lists(), indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(derivation.to_dict(), indent=2, sort_keys=True) if args.json else derivation.describe())
    return 0


def _audit(args: argparse.Namespace) -> int:
    store = Store(args.store)
    days = store.days()
    day = args.day or (days[-1] if days else "")
    if not day:
        print(f"data: {store.root} has no day to audit", file=sys.stderr)
        return 2
    try:
        report = audit_day(store, day)
        examples = unparsable(store, day, limit=args.skipped)
    except StoreError as error:
        print(f"data: {error}", file=sys.stderr)
        return 2
    if args.json:
        payload = report.to_dict()
        payload["unparsable"] = list(examples)
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(report.describe())
        for line in examples:
            print(f"  {line}")
    # Two questions live here.  ``report.ok`` asks whether the store can
    # re-derive its own day; the exit code asks the gate's question — whether
    # every body in it was parsed — because a nightly audit that stays green on
    # a day the gate will refuse is an alarm that rings after the window closes.
    return 0 if report.ok and (args.allow_skipped or not report.skipped) else 1


def _gaps(args: argparse.Namespace) -> int:
    store = Store(args.store)
    try:
        items = list(store.quotes(since=args.since, until=args.until))
        if args.funding:
            # A funding hole is invisible to a quote-only check, and one of them
            # (HTX funding-btc, 18.11) is the reason this flag exists: the gate
            # counts funding among its 18 series, so the diagnosis has to see it.
            items.extend(store.fundings(since=args.since, until=args.until))
    except StoreError as error:
        print(f"data: {error}", file=sys.stderr)
        return 2
    if not items:
        print(f"data: {store.root} holds no quote in the requested window", file=sys.stderr)
        return 2
    gaps = find_gaps(items, args.interval, since=args.since, until=args.until)
    covs = coverage(items, args.interval, since=args.since, until=args.until)
    payload = {"gaps": [gap.to_dict() for gap in gaps], "coverage": [item.to_dict() for item in covs]}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for item in covs:
            print(f"coverage {item.venue} {item.symbol} ({item.kind}): {item.fraction:.1%} of {item.expected} slots")
        for gap in gaps:
            print(gap.describe())
        if not gaps:
            print(f"gaps: none at {args.interval:g}s")
    return 0 if not gaps else 1


def _truth(args: argparse.Namespace) -> int:
    store = Store(args.store)
    try:
        floors = tuple(float(part) for part in _split(args.min_bps)) or (0.0,)
    except ValueError:
        print(f"data: --min-bps is a list of bps numbers, not {args.min_bps!r}", file=sys.stderr)
        return 2
    reports = []
    try:
        for floor in floors:
            reports.append(
                truth_day(
                    store,
                    args.day,
                    min_venues=args.min_venues,
                    z_threshold=args.z,
                    max_deviation_bps=args.max_deviation_bps,
                    min_deviation_bps=floor,
                )
            )
    except (DataError, StoreError) as error:
        print(f"data: {error}", file=sys.stderr)
        return 2
    if args.json:
        payload = [report.to_dict() for report in reports]
        print(json.dumps(payload[0] if len(payload) == 1 else payload, indent=2, sort_keys=True))
    elif len(reports) == 1:
        print(reports[0].describe())
    else:
        print(_sweep(reports, floors))
    if args.max_fp_share is not None:
        worst = max(
            (report.false_positive_share for report in reports if report.false_positive_share is not None),
            default=None,
        )
        if worst is not None and worst > args.max_fp_share:
            print(
                f"truth: false-positive share {worst:.1%} exceeds {args.max_fp_share:.1%}",
                file=sys.stderr,
            )
            return 1
    return 0


def _sweep(reports, floors) -> str:
    """One line per materiality floor: what the gate costs and what it buys."""
    rows = ["  bps floor  findings  snapped back      share  per section  judged"]
    for floor, report in zip(floors, reports):
        share = report.false_positive_share
        per_section = report.false_positives_per_section
        rows.append(
            f"{floor:11.1f} {len(report.events):9d} {report.false_positives:13d} "
            f"{'n/a' if share is None else f'{share:6.1%}':>10} "
            f"{'n/a' if per_section is None else f'{per_section:11.2e}':>12} {report.judged:7d}"
        )
    return "\n".join(rows)


def _doctor(args: argparse.Namespace) -> int:
    store = Store(args.store)
    try:
        diagnosis = diagnose(
            store,
            now=args.at,
            interval_s=args.interval,
            window_min=args.minutes,
            max_age_s=args.max_age,
            floor=args.floor,
            coherence_window=args.window,
            journal_path=Path(args.journal),
            min_free_gb=args.min_free_gb,
            quote_max_age_s=args.quote_max_age,
        )
    except OSError as error:
        # A path that is not a store is a question that could not be asked: the
        # doctor must not answer "healthy" or crash its way out of a cron line.
        print(f"data: {error}", file=sys.stderr)
        return 2
    print(json.dumps(diagnosis.to_dict(), indent=2, sort_keys=True) if args.json else diagnosis.describe())
    # ``wait`` is a calendar, not a defect: a young store exits 0 here exactly as
    # it exits 3 in the cycle, and only ``warn``/``fail`` wake a cron line.
    return 0 if diagnosis.ok else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="monster-data", description="ENRG Monster data layer: record, replay, audit")
    commands = parser.add_subparsers(dest="command", required=True)

    probe_cmd = commands.add_parser(
        "probe", help="ask every endpoint once: does it answer, and does the answer parse?"
    )
    probe_source = probe_cmd.add_mutually_exclusive_group(required=True)
    probe_source.add_argument("--fixtures", help="probe recorded answers instead of the venues")
    probe_source.add_argument("--live", action="store_true", help="ask the venues (public endpoints, no keys)")
    probe_cmd.add_argument("--assets", default="btc,eth")
    probe_cmd.add_argument("--venues", default="htx,okx,bybit")
    probe_cmd.add_argument("--timeout", type=float, default=10.0)
    probe_cmd.add_argument("--json", action="store_true")
    probe_cmd.set_defaults(handler=_probe)

    capture = commands.add_parser("capture", help="one pass (or --count passes) of venue reads")
    capture.add_argument("--store", default="var/data", help="where the day files live")
    source = capture.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixtures", help="replay recorded answers from this directory")
    source.add_argument("--live", action="store_true", help="ask the venues (public endpoints, no keys)")
    capture.add_argument(
        "--at", type=float, help="the clock to stamp a replay with; live passes are stamped by the receive clock"
    )
    capture.add_argument("--count", type=int, default=1, help="number of passes (live mode sleeps between them)")
    capture.add_argument("--interval", type=float, default=60.0, help="seconds between passes")
    capture.add_argument("--assets", default="btc,eth")
    capture.add_argument("--venues", default="htx,okx,bybit")
    capture.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="seconds per request; with two waves of workers, 5 s keeps a stalled venue inside a 60 s slot",
    )
    capture.add_argument("--retries", type=int, default=1)
    capture.add_argument(
        "--workers", type=int, default=8, help="requests in flight at once (1 = strictly sequential)"
    )
    capture.add_argument("--fsync", action="store_true", help="fsync every day file after a write")
    capture.add_argument(
        "--journal",
        help="append data.captured events to this journal — the collector's own chain, never the cycle's",
    )
    capture.add_argument("--json", action="store_true")
    capture.set_defaults(handler=_capture)

    tape = commands.add_parser("tape", help="write a synthetic tape for offline runs")
    tape.add_argument("--root", default="var/tape")
    tape.add_argument("--store", default="var/data", help="only used to print the next command")
    tape.add_argument("--samples", type=int, default=130)
    tape.add_argument("--interval", type=float, default=300.0)
    tape.add_argument("--base", type=float, default=1_794_873_600.0)
    tape.add_argument("--assets", default="btc,eth")
    tape.add_argument("--seed", type=int, default=7)
    tape.set_defaults(handler=_tape)

    derive_cmd = commands.add_parser("derive", help="the snapshot and history a cycle would see")
    derive_cmd.add_argument("--store", default="var/data")
    derive_cmd.add_argument("--at", type=float, help="as-of timestamp (default: the newest quote)")
    derive_cmd.add_argument("--interval", type=float, default=60.0)
    derive_cmd.add_argument("--bars", type=int, default=128)
    derive_cmd.add_argument("--max-quote-age", type=float, default=5.0)
    derive_cmd.add_argument("--max-funding-age", type=float, default=12.0)
    derive_cmd.add_argument("--snapshot-out", help="write the Snapshot JSON here")
    derive_cmd.add_argument("--history-out", help="write the history JSON here")
    derive_cmd.add_argument("--json", action="store_true")
    derive_cmd.set_defaults(handler=_derive)

    audit = commands.add_parser("audit", help="re-parse a day of raw bodies and compare")
    audit.add_argument("--store", default="var/data")
    audit.add_argument("--day", help="YYYY-MM-DD (default: the newest day)")
    audit.add_argument("--json", action="store_true")
    audit.add_argument(
        "--skipped",
        type=int,
        default=0,
        help="also print this many bodies the parser refused, with their content",
    )
    audit.add_argument(
        "--allow-skipped",
        action="store_true",
        dest="allow_skipped",
        help="exit 0 even when some bodies were unparsable (re-derivability only)",
    )
    audit.set_defaults(handler=_audit)

    gaps = commands.add_parser("gaps", help="missing slots and coverage in a window")
    gaps.add_argument("--store", default="var/data")
    gaps.add_argument("--interval", type=float, default=60.0)
    gaps.add_argument("--since", type=float)
    gaps.add_argument("--until", type=float)
    gaps.add_argument(
        "--funding",
        action="store_true",
        help="include funding rows: the gate counts them among its 18 series",
    )
    gaps.add_argument("--json", action="store_true")
    gaps.set_defaults(handler=_gaps)

    truth = commands.add_parser(
        "truth-report",
        help="replay the truth check over a day: how often it fired, and how often it was noise",
    )
    truth.add_argument("--store", default="var/data")
    truth.add_argument("--day", help="YYYY-MM-DD (default: the newest day)")
    truth.add_argument("--min-venues", type=int, default=3, help="venues needed before disagreement can be judged")
    truth.add_argument("--z", type=float, default=4.0, help="robust z above which a venue is flagged")
    truth.add_argument(
        "--max-deviation-bps",
        type=float,
        default=100.0,
        help="flag when the others agree exactly and the venue is this far off",
    )
    truth.add_argument(
        "--min-bps",
        default="0",
        help=(
            "materiality floor: a deviation below this many bps is not a finding, "
            "whatever the z says; a comma list (0,1,2,3) sweeps the floor and "
            "prints the gate for each"
        ),
    )
    truth.add_argument(
        "--max-fp-share",
        type=float,
        help="exit 1 when the false-positive share of judged findings exceeds this (the gate is 0.01)",
    )
    truth.add_argument("--json", action="store_true")
    truth.set_defaults(handler=_truth)

    doctor = commands.add_parser(
        "doctor",
        help="one screen: is the collector delivering, and can the cycle decide right now?",
    )
    doctor.add_argument("--store", default="var/data")
    doctor.add_argument(
        "--journal",
        default="var/journal.capture.jsonl",
        help="the collector's own chain (never the cycle's)",
    )
    doctor.add_argument("--at", type=float, help="the clock to judge by (default: now)")
    doctor.add_argument("--interval", type=float, default=60.0, help="the capture cadence")
    doctor.add_argument("--minutes", type=float, default=60.0, help="window for coverage and gaps")
    doctor.add_argument(
        "--max-age",
        type=float,
        default=180.0,
        help="how old the newest quote may be before the collector is behind",
    )
    doctor.add_argument(
        "--quote-max-age",
        type=float,
        default=5.0,
        help="how old a quote may be inside a pass (MONSTER_MAX_QUOTE_AGE_S)",
    )
    doctor.add_argument(
        "--window",
        type=int,
        default=128,
        help="the coherence window the cycle needs (MONSTER_COHERENCE_WINDOW)",
    )
    doctor.add_argument("--floor", type=float, default=0.98, help="coverage floor per series")
    doctor.add_argument(
        "--min-free-gb",
        type=float,
        default=1.0,
        help="free space below which a growing store is at risk",
    )
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=_doctor)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (StoreError, DataError, JournalError, ValueError) as error:
        print(f"data: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised through ``python -m``
    raise SystemExit(main())

