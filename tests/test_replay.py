"""The cache is a convenience; the chain is the book.

Every test here starts from a real cycle through the CLI or from fills written
into a real journal, because the claim being tested is that two *independent*
paths through the same arithmetic agree — a fixture that builds the expected book
by hand would only prove the fixture.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import numpy as np
import pytest

from monster.config import Config
from monster.contracts import Fill, FundingQuote, Quote, Snapshot
from monster.cycle import load_state, main
from monster.exec.paper import PaperVenue
from monster.journal import Journal
from monster.replay import ReplayError, compare, reconcile, replay

BASE = 1_700_000_000.0


def _snapshot() -> Snapshot:
    """A spot and a perp on one venue, plus two witnesses for the truth check."""
    quotes = (
        Quote(venue="htx", symbol="btcusdt", bid=34_999.0, ask=35_001.0, ts=BASE),
        Quote(venue="okx", symbol="btcusdt", bid=34_999.0, ask=35_001.0, ts=BASE),
        Quote(venue="bybit", symbol="btcusdt", bid=34_998.0, ask=35_000.0, ts=BASE),
        Quote(venue="htx", symbol="btc-usdt-swap", bid=34_999.0, ask=35_001.0, ts=BASE),
        Quote(venue="htx", symbol="ethusdt", bid=1_999.0, ask=2_001.0, ts=BASE),
    )
    funding = (FundingQuote(venue="htx", symbol="btc-usdt-swap", rate=0.0005, period_hours=8.0, ts=BASE),)
    return Snapshot(ts=BASE, quotes=quotes, funding=funding, source="unit-test fixture")


def _history(n: int = 96) -> dict:
    rng = np.random.default_rng(3)
    clock = np.arange(n, dtype=float)
    base = 0.10 * np.sin(2.0 * np.pi * clock / 16.0)
    return {
        symbol: (level * np.exp(base + 0.002 * rng.standard_normal(n))).tolist()
        for symbol, level in (
            ("btcusdt", 35_000.0),
            ("btc-usdt-swap", 35_000.0),
            ("ethusdt", 2_000.0),
            ("eth-usdt-swap", 2_000.0),
        )
    }


@pytest.fixture
def traded(tmp_path):
    """One real cycle through the CLI: a journal, a state cache, a filled carry."""
    snapshot_path, history_path = tmp_path / "snapshot.json", tmp_path / "history.json"
    snapshot_path.write_text(json.dumps(_snapshot().to_dict()), encoding="utf-8")
    history_path.write_text(json.dumps(_history()), encoding="utf-8")
    journal_path, state_path = tmp_path / "journal.jsonl", tmp_path / "state.json"
    args = [
        "--snapshot", str(snapshot_path),
        "--history", str(history_path),
        "--journal", str(journal_path),
        "--state", str(state_path),
    ]
    assert main(args) == 0
    return SimpleNamespace(
        args=args,
        journal_path=journal_path,
        state_path=state_path,
        config=Config.from_env(os.environ),
    )


def _opening_fill() -> Fill:
    return Fill(
        ts=BASE,
        strategy="carry",
        venue="htx",
        symbol="btc-usdt-swap",
        kind="perp",
        side="sell",
        qty=10.0,
        price=100.0,
        fee_usd=0.5,
        notional_usd=1_000.0,
        leg_index=0,
    )


def _accrual() -> dict:
    return {"venue": "htx", "symbol": "btc-usdt-swap", "amount_usd": -0.4, "periods": 1, "period_hours": 8.0}


def _journalled_round_trip(tmp_path, *, realized_usd=None):
    """A fill, a funding payment and a close, written to a journal by the venue.

    Returns ``(journal, venue, trade)``: the venue is the book that *traded*, and
    whatever the replay produces has to be that same book.
    """
    config = Config.from_env({})
    journal = Journal(tmp_path / "journal.jsonl", fsync=False)
    journal.append("cycle.started", {"cycle": 1, "ts": BASE, "config": config.to_dict()}, ts=BASE)
    venue = PaperVenue(cash_usd=1_000.0, fee_bps=5.0, slippage_bps=2.0, leverage=2.0, ts=BASE)
    fill = _opening_fill()
    venue.apply_fill(fill)
    journal.append("order.filled", fill.to_dict(), ts=fill.ts)

    accrual = _accrual()
    journal.append("funding.accrued", {"accruals": [accrual]}, ts=BASE + 1.0)
    position = venue.positions[("htx", "btc-usdt-swap", "perp")]
    venue.cash += accrual["amount_usd"]
    position.funding_usd += accrual["amount_usd"]
    position.last_funding_ts += accrual["period_hours"] * 3600.0

    exit_ts = BASE + 3_600.0
    snapshot = Snapshot(
        ts=exit_ts,
        quotes=(Quote(venue="htx", symbol="btc-usdt-swap", bid=95.0, ask=97.0, ts=exit_ts),),
    )
    trade = venue.close(("htx", "btc-usdt-swap", "perp"), snapshot, reason="horizon", ts=exit_ts)
    document = trade.to_dict()
    if realized_usd is not None:
        document["realized_usd"] = realized_usd
    journal.append("trade.closed", document, ts=exit_ts)
    return journal, venue, trade


def test_a_cycle_leaves_a_journal_that_rebuilds_its_own_book(traded):
    journal = Journal(traded.journal_path)
    cached = load_state(traded.state_path, traded.config)[0]
    replayed = replay(journal, traded.config)

    assert replayed.events == journal.count()
    assert replayed.venue.positions, "the cycle filled something; otherwise this proves nothing"
    assert compare(cached, replayed.venue) == ()
    assert cached.cash == pytest.approx(replayed.venue.cash, rel=1e-12)


def test_the_rebuild_is_the_book_field_by_field_not_merely_the_same_size(traded):
    journal = Journal(traded.journal_path)
    cached = load_state(traded.state_path, traded.config)[0]
    replayed = replay(journal, traded.config)

    assert set(cached.positions) == set(replayed.venue.positions)
    for key, position in cached.positions.items():
        twin = replayed.venue.positions[key]
        assert (twin.qty, twin.avg_price) == (position.qty, position.avg_price)
        assert twin.margin_usd == pytest.approx(position.margin_usd, rel=1e-12)
        assert twin.strategy == position.strategy
    assert [trade.realized_usd for trade in cached.closed] == [
        pytest.approx(trade.realized_usd, rel=1e-12) for trade in replayed.venue.closed
    ]


def test_funding_and_a_close_survive_the_round_trip(tmp_path):
    journal, venue, trade = _journalled_round_trip(tmp_path)
    replayed = replay(journal, Config.from_env({}))

    assert compare(venue, replayed.venue) == ()
    assert replayed.venue.positions == {}, "a closed position must not survive the replay"
    assert replayed.venue.closed[0].realized_usd == pytest.approx(trade.realized_usd)
    # A short 10 @ 100 is bought back at the ask *plus* slippage: 97 * 1.0002.  The
    # entry fee (0.50), the exit fee and the funding (-0.40) are all in the number.
    assert trade.exit_price == pytest.approx(97.0 * 1.0002)
    assert trade.realized_usd == pytest.approx(29.806 - 0.5 - 0.485097 - 0.4, rel=1e-9)
    assert replayed.disagreements == ()


def test_a_close_that_names_a_position_the_journal_never_opened_is_refused(tmp_path):
    journal, _, _ = _journalled_round_trip(tmp_path)
    orphan = Journal(tmp_path / "orphan.jsonl", fsync=False)
    for event in journal.events():
        if event.kind != "order.filled":
            orphan.append(event.kind, event.payload, ts=event.ts)

    with pytest.raises(ReplayError) as refused:
        replay(orphan, Config.from_env({}))
    assert "no open position" in str(refused.value)


def test_journal_arithmetic_that_disagrees_with_the_fills_is_named(tmp_path):
    journal, _, _ = _journalled_round_trip(tmp_path, realized_usd=99.0)
    replayed = replay(journal, Config.from_env({}))

    assert replayed.disagreements, "a realized_usd the fills do not produce must be reported"
    assert "realized_usd" in replayed.disagreements[0].field
    outcome = reconcile(journal, Config.from_env({}), None)
    assert outcome.mode == "diverged" and outcome.diverged
    assert "realized_usd" in outcome.describe()


def test_the_replay_ignores_kinds_it_does_not_know_and_says_so(tmp_path):
    journal, venue, _ = _journalled_round_trip(tmp_path)
    journal.append("margin.called", {"why": "a later version's business"}, ts=BASE + 7_200.0)
    replayed = replay(journal, Config.from_env({}))

    assert replayed.ignored == ("margin.called",)
    assert any("margin.called" in note for note in replayed.notes)
    assert compare(venue, replayed.venue) == ()


def test_a_lost_cache_is_rebuilt_from_the_journal(traded, capsys):
    before = load_state(traded.state_path, traded.config)[0]
    traded.state_path.unlink()
    capsys.readouterr()

    assert main(traded.args) == 0
    assert "rebuilt from the journal" in capsys.readouterr().err

    after = load_state(traded.state_path, traded.config)[0]
    assert after.positions and set(after.positions) == set(before.positions)
    assert after.cash == pytest.approx(before.cash, rel=1e-12)
    kinds = [event.kind for event in Journal(traded.journal_path).events()]
    assert "state.rebuilt" in kinds


def test_a_phantom_position_stops_the_cycle_and_is_named(traded, capsys):
    document = json.loads(traded.state_path.read_text(encoding="utf-8"))
    ghost = dict(document["venue"]["positions"][0], symbol="ethusdt")
    document["venue"]["positions"].append(ghost)
    traded.state_path.write_text(json.dumps(document), encoding="utf-8")
    before = len(list(Journal(traded.journal_path).events()))
    capsys.readouterr()

    assert main(traded.args) == 2
    err = capsys.readouterr().err
    assert "ethusdt" in err and "different books" in err

    events = list(Journal(traded.journal_path).events())
    assert [event.kind for event in events[before:]] == ["state.diverged"]
    assert any("ethusdt" in item["field"] for item in events[-1].payload["divergences"])


def test_a_recentred_cash_balance_is_a_divergence_not_a_rounding_error(traded, capsys):
    document = json.loads(traded.state_path.read_text(encoding="utf-8"))
    document["venue"]["cash"] += 100.0
    traded.state_path.write_text(json.dumps(document), encoding="utf-8")
    capsys.readouterr()

    assert main(traded.args) == 2
    assert "cash:" in capsys.readouterr().err


def test_a_divergence_does_not_trade(traded, capsys):
    document = json.loads(traded.state_path.read_text(encoding="utf-8"))
    document["venue"]["cash"] += 100.0
    traded.state_path.write_text(json.dumps(document), encoding="utf-8")
    before = [event.kind for event in Journal(traded.journal_path).events()]
    capsys.readouterr()

    main(traded.args)
    after = [event.kind for event in Journal(traded.journal_path).events()]
    assert "cycle.started" not in after[len(before):]
    assert "order.filled" not in after[len(before):]


def test_the_cycle_links_the_capture_chain_by_its_head(traded, monkeypatch, capsys):
    capture_path = traded.journal_path.parent / "journal.capture.jsonl"
    capture = Journal(capture_path, fsync=False)
    capture.append("data.captured", {"written": 18}, ts=BASE)
    monkeypatch.setenv("MONSTER_DATA_JOURNAL", str(capture_path))
    capsys.readouterr()

    assert main(traded.args) == 0
    linked = [event for event in Journal(traded.journal_path).events() if event.kind == "journal.linked"]
    assert len(linked) == 1
    assert linked[0].payload["verdict"] == "ok"
    assert linked[0].payload["head"] == Journal(capture_path).last().hash
    assert linked[0].payload["events"] == 1


def test_verify_checks_the_chain_and_the_chains_it_links(traded, monkeypatch, capsys):
    """`--verify` is an audit of what the book *read*, not only of the book."""
    capture_path = traded.journal_path.parent / "journal.capture.jsonl"
    capture = Journal(capture_path, fsync=False)
    capture.append("data.captured", {"written": 18}, ts=BASE)
    monkeypatch.setenv("MONSTER_DATA_JOURNAL", str(capture_path))
    capsys.readouterr()
    assert main(traded.args) == 0
    capsys.readouterr()

    verify_args = ["--verify", "--journal", str(traded.journal_path)]
    assert main(verify_args) == 0
    printed = capsys.readouterr().out
    assert "ok (line 1 hash" in printed
    assert "links: 1 ok" in printed

    # A capture chain rewritten into a *valid* chain that says something else:
    # the book's own chain still verifies, and the claim about what it read does not.
    forged_path = traded.journal_path.parent / "forged.jsonl"
    forged = Journal(forged_path, fsync=False)
    forged.append("data.captured", {"written": 18, "note": "nothing failed"}, ts=BASE)
    capture_path.write_bytes(forged_path.read_bytes())
    capsys.readouterr()

    assert main(verify_args) == 1
    assert "links: 1 of 1 do not hold" in capsys.readouterr().err
    # The escape hatch is for an archive the operator moved on purpose, and it is
    # explicit: the default answer to "somebody rewrote the data" is refusal.
    assert main(verify_args + ["--no-links"]) == 0


def test_the_replay_command_prints_the_book_and_can_adopt_it(traded, capsys):
    replay_args = ["--replay", "--journal", str(traded.journal_path), "--state", str(traded.state_path)]
    capsys.readouterr()
    assert main(replay_args) == 0
    printed = capsys.readouterr().out
    assert "reconcile: ok" in printed and "position(s)" in printed

    traded.state_path.unlink()
    assert main(replay_args + ["--write-state"]) == 0
    assert "written from the journal" in capsys.readouterr().out
    assert traded.state_path.exists()
    assert load_state(traded.state_path, traded.config)[0].positions


def test_adopting_a_cache_that_already_agrees_writes_no_line(traded, capsys):
    """--write-state on a cache that already is the book changes nothing.

    A file rewritten with identical bytes is fine; a journal line claiming a
    rebuild that did not happen is not, because the next reader would believe
    the cache had been lost.
    """
    replay_args = [
        "--replay",
        "--write-state",
        "--journal",
        str(traded.journal_path),
        "--state",
        str(traded.state_path),
    ]
    capsys.readouterr()

    assert main(replay_args) == 0
    kinds = [event.kind for event in Journal(traded.journal_path).events()]
    assert "state.rebuilt" not in kinds
    assert "state.diverged" not in kinds


def test_write_state_without_replay_is_refused(traded, capsys):
    capsys.readouterr()

    assert main(["--write-state", "--journal", str(traded.journal_path), "--state", str(traded.state_path)]) == 2
    assert "--replay" in capsys.readouterr().err


def test_the_replay_command_will_not_overwrite_a_diverged_cache(traded, capsys):
    document = json.loads(traded.state_path.read_text(encoding="utf-8"))
    document["venue"]["cash"] += 1.0
    traded.state_path.write_text(json.dumps(document), encoding="utf-8")
    replay_args = [
        "--replay",
        "--write-state",
        "--journal",
        str(traded.journal_path),
        "--state",
        str(traded.state_path),
    ]
    capsys.readouterr()

    assert main(replay_args) == 1
    assert "refusing to overwrite" in capsys.readouterr().err
    assert json.loads(traded.state_path.read_text(encoding="utf-8"))["venue"]["cash"] == pytest.approx(
        document["venue"]["cash"]
    ), "the cache must be exactly as wrong as it was: the operator decides, not this command"
