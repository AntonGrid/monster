"""One cycle end to end: every step journaled, every gate able to say no."""
from __future__ import annotations

import json

import numpy as np
import pytest

from monster.brain.phase import phase_coherence
from monster.config import Config
from monster.contracts import FundingQuote, Quote, Snapshot
from monster.cycle import load_state, run_cycle, save_state
from monster.exec.paper import PaperVenue
from monster.journal import Journal
from monster.risk.limits import KillSwitch, Limits

BASE = 1_700_000_000.0


def _snapshot(
    offset: float = 0.0,
    *,
    htx_spot: float = 35_000.0,
    htx_perp: float = 35_000.0,
    other: float = 35_000.0,
    rate: float = 0.0005,
    with_funding: bool = True,
) -> Snapshot:
    stamp = BASE + offset
    quotes = (
        Quote(venue="htx", symbol="btcusdt", bid=htx_spot - 1.0, ask=htx_spot + 1.0, ts=stamp),
        Quote(venue="okx", symbol="btcusdt", bid=other - 1.0, ask=other + 1.0, ts=stamp),
        Quote(venue="bybit", symbol="btcusdt", bid=other - 2.0, ask=other, ts=stamp),
        Quote(venue="htx", symbol="btc-usdt-swap", bid=htx_perp - 1.0, ask=htx_perp + 1.0, ts=stamp),
        Quote(venue="htx", symbol="ethusdt", bid=1_999.0, ask=2_001.0, ts=stamp),
    )
    funding = (
        (FundingQuote(venue="htx", symbol="btc-usdt-swap", rate=rate, period_hours=8.0, ts=stamp),)
        if with_funding
        else ()
    )
    return Snapshot(ts=stamp, quotes=quotes, funding=funding, source="unit-test fixture")


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


def _config(**overrides) -> Config:
    values = dict(
        capital_usd=1_000.0,
        notional_usd=100.0,
        fee_bps=5.0,
        slippage_bps=2.0,
        leverage=2.0,
        horizon_hours=168.0,
        min_edge_bps=10.0,
        close_after_hours=168.0,
        coherence_window=64,
        surrogates=50,
        seed=1,
        venue="htx",
        limits=Limits(
            max_gross_usd=2_000.0,
            max_net_delta_usd=200.0,
            max_symbol_usd=1_000.0,
            max_venue_usd=2_000.0,
            max_leverage=2.0,
            max_daily_loss_usd=20.0,
            min_free_cash_usd=50.0,
        ),
    )
    values.update(overrides)
    return Config(**values)


def _venue() -> PaperVenue:
    return PaperVenue(cash_usd=1_000.0, fee_bps=5.0, slippage_bps=2.0, leverage=2.0, ts=BASE)


def test_a_cycle_records_every_step_and_fills_the_fat_carry(tmp_path):
    journal = Journal(tmp_path / "journal.jsonl", fsync=False)
    venue, kill = _venue(), KillSwitch()
    report = run_cycle(
        _config(),
        _snapshot(),
        journal=journal,
        venue=venue,
        kill=kill,
        history=_history(),
    )

    assert report.verdict == "ok"
    assert [decision.outcome for decision in report.decisions] == ["filled"]
    assert report.positions == 2
    assert 995.0 < report.equity_usd < 1_000.0
    assert journal.verify() == "ok"

    kinds = [event.kind for event in journal.events()]
    for kind in (
        "cycle.started",
        "funding.accrued",
        "equity.mark",
        "truth.read",
        "coherence.read",
        "carry.scan",
        "intent.proposed",
        "order.filled",
        "position.opened",
        "autopsy.summary",
        "cycle.finished",
    ):
        assert kind in kinds, kind
    assert report.coherence is not None and report.coherence.measured
    assert report.truth is not None and report.truth.clean
    assert "carry: 1/2 accepted" in report.describe()


def test_the_same_carry_is_not_stacked_every_cycle(tmp_path):
    journal = Journal(tmp_path / "journal.jsonl", fsync=False)
    venue, kill = _venue(), KillSwitch()
    config = _config()
    run_cycle(config, _snapshot(), journal=journal, venue=venue, kill=kill)
    second = run_cycle(config, _snapshot(), journal=journal, venue=venue, kill=kill)
    assert [decision.outcome for decision in second.decisions] == ["refused"]
    assert second.decisions[0].codes == ("ALREADY_OPEN",)
    assert second.positions == 2
    assert journal.verify() == "ok"


def test_funding_accrues_and_the_horizon_closes_the_pair(tmp_path):
    journal = Journal(tmp_path / "journal.jsonl", fsync=False)
    config = _config(close_after_hours=8.0)
    venue, kill = _venue(), KillSwitch()
    run_cycle(config, _snapshot(), journal=journal, venue=venue, kill=kill)

    report = run_cycle(config, _snapshot(8 * 3600.0), journal=journal, venue=venue, kill=kill)
    assert report.funding is not None and report.funding.total_usd > 0.0
    assert len(report.closed) == 2 and report.positions == 0
    assert report.equity_usd == pytest.approx(venue.cash)
    # The exits are at the horizon, not at a stop; the reason is in the record.
    assert {trade.reason for trade in report.closed} == {"horizon"}
    assert journal.verify() == "ok"


def test_a_lying_venue_vetoes_the_leg_it_quotes(tmp_path):
    journal = Journal(tmp_path / "journal.jsonl", fsync=False)
    venue, kill = _venue(), KillSwitch()
    report = run_cycle(
        _config(),
        _snapshot(htx_spot=36_800.0, htx_perp=36_800.0, other=35_000.0),
        journal=journal,
        venue=venue,
        kill=kill,
    )
    assert report.truth is not None and not report.truth.clean
    assert report.decisions[0].codes == ("TRUTH_FLAG",)
    assert report.positions == 0
    assert "deviates" in report.decisions[0].reasons[0]


def test_a_tripped_kill_switch_refuses_everything(tmp_path):
    journal = Journal(tmp_path / "journal.jsonl", fsync=False)
    venue, kill = _venue(), KillSwitch()
    kill.trip("manual stop before the weekend")
    report = run_cycle(_config(), _snapshot(), journal=journal, venue=venue, kill=kill)
    assert report.killed and report.positions == 0
    assert report.decisions[0].codes == ("KILLED",)
    assert "manual stop" in report.decisions[0].reasons[0]


def test_a_tampered_journal_stops_the_cycle_before_it_marks(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = Journal(path, fsync=False)
    journal.append("decision", {"take": False})
    journal.append("decision", {"take": True})
    journal.append("decision", {"take": False})

    lines = path.read_text(encoding="utf-8").splitlines()
    document = json.loads(lines[0])
    document["payload"]["take"] = True
    lines[0] = json.dumps(document, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    reopened = Journal(path, fsync=False)
    venue, kill = _venue(), KillSwitch()
    report = run_cycle(_config(), _snapshot(), journal=reopened, venue=venue, kill=kill)
    assert report.killed and report.verdict.startswith("line 1")
    assert report.decisions == () and venue.positions == {}
    assert kill.tripped
    assert "cash only" in report.notes[1]


def test_state_cache_survives_a_restart(tmp_path):
    journal = Journal(tmp_path / "journal.jsonl", fsync=False)
    venue, kill = _venue(), KillSwitch()
    run_cycle(_config(), _snapshot(), journal=journal, venue=venue, kill=kill)
    state_path = tmp_path / "state.json"
    save_state(state_path, venue, kill)

    restored, restored_kill = load_state(state_path, _config())
    assert restored.cash == pytest.approx(venue.cash)
    assert set(restored.positions) == set(venue.positions)
    assert not restored_kill.tripped
    assert restored.exposure(_snapshot()) == venue.exposure(_snapshot())


def test_live_mode_is_not_a_flag():
    with pytest.raises(ValueError, match="paper"):
        Config(mode="live")
    with pytest.raises(ValueError, match="paper"):
        Config.from_env({"MONSTER_MODE": "live"})


def test_env_defaults_are_safe_and_explicit():
    config = Config.from_env({})
    assert config.mode == "paper"
    assert config.limits.max_daily_loss_usd == 20.0
    changed = Config.from_env({"MONSTER_NOTIONAL_USD": "25", "MONSTER_MIN_EDGE_BPS": "3.5"})
    assert changed.notional_usd == 25.0 and changed.min_edge_bps == 3.5
    with pytest.raises(ValueError, match="not a number"):
        Config.from_env({"MONSTER_NOTIONAL_USD": "twenty"})
    with pytest.raises(ValueError, match="whole number"):
        Config.from_env({"MONSTER_SURROGATES": "10.5"})


def test_the_fixture_coherence_reading_is_conservative():
    # The committed history has two clusters of two; with four oscillators the
    # null can easily produce R≈0.8, so "coherent" must not be claimed.
    with open("data/snapshots/history.json", encoding="utf-8") as handle:
        history = json.load(handle)
    reading = phase_coherence(history, ts=BASE, window=128, surrogates=200, seed=0)
    assert reading.measured and reading.R is not None
    assert reading.null_mean is not None and reading.R > reading.null_mean
    assert reading.p_value is not None and reading.p_value > 0.01