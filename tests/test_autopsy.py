"""The autopsy must say "unresolvable" before it says "profitable"."""
from __future__ import annotations

import pytest

from monster.autopsy import MIN_TRADES, autopsy, summary
from monster.contracts import ClosedTrade

BASE = 1_700_000_000.0


def _trade(bps: float, *, strategy: str = "carry", fees: float = 0.1, funding: float = 0.05, n: int = 0) -> ClosedTrade:
    net = bps / 1e4 * 100.0
    return ClosedTrade(
        strategy=strategy,
        venue="htx",
        symbol="btcusdt",
        kind="perp",
        entry_side="sell",
        qty=0.01,
        entry_price=100.0,
        exit_price=100.0,
        entry_notional_usd=100.0,
        fees_usd=fees,
        funding_usd=funding,
        realized_usd=net,
        bps=bps,
        opened_ts=BASE + n,
        closed_ts=BASE + n + 60.0,
        reason="horizon",
    )


def test_one_trade_is_unmeasurable_not_a_win_rate():
    reports = autopsy([_trade(50.0)])
    assert len(reports) == 1
    report = reports[0]
    assert report.n == 1 and report.mean_bps is None and report.t_stat is None
    assert report.resolvable is None and "unmeasurable" in report.note
    assert "unmeasurable" in report.describe()
    assert MIN_TRADES == 2


def test_a_sample_gets_a_t_and_a_resolution_threshold():
    reports = autopsy([_trade(60.0 + index, n=index) for index in range(8)])
    report = reports[0]
    assert report.n == 8
    assert report.mean_bps == pytest.approx(63.5)
    assert report.sigma_bps is not None and report.sigma_bps > 0
    assert report.t_stat is not None and report.t_stat > 10
    assert report.min_resolvable_bps is not None and report.min_resolvable_bps > 0
    assert report.resolvable is not None
    assert "resolved" in report.describe() or "UNRESOLVABLE" in report.describe()


def test_a_noisy_sample_is_called_unresolvable_even_when_the_mean_is_positive():
    bps = [200.0, -180.0, 150.0, -140.0, 120.0, -110.0]
    report = autopsy([_trade(value, n=index) for index, value in enumerate(bps)])[0]
    assert report.mean_bps is not None and report.mean_bps > 0
    assert report.resolvable is False
    assert "UNRESOLVABLE" in report.describe()
    # And the gate's threshold is larger than the mean it refuses to believe.
    assert report.min_resolvable_bps is not None and report.min_resolvable_bps > report.mean_bps


def test_strategies_are_kept_apart_and_costs_are_summed():
    trades = [
        _trade(30.0, strategy="carry", fees=0.2, funding=0.1, n=1),
        _trade(40.0, strategy="carry", fees=0.2, funding=0.1, n=2),
        _trade(-10.0, strategy="momentum", n=3),
    ]
    reports = autopsy(trades)
    assert [report.strategy for report in reports] == ["carry", "momentum"]
    carry = reports[0]
    assert carry.fees_usd == pytest.approx(0.4) and carry.funding_usd == pytest.approx(0.2)
    assert carry.wins == 2 and carry.win_rate == pytest.approx(1.0)


def test_summary_is_journal_shaped():
    payload = summary([_trade(10.0, n=1), _trade(20.0, n=2)])
    assert payload["n_trades"] == 2
    assert payload["net_usd"] == pytest.approx(0.3)
    assert payload["measured"] is True
    assert payload["strategies"][0]["strategy"] == "carry"
    assert set(payload) == {"n_trades", "net_usd", "fees_usd", "funding_usd", "strategies", "measured"}


def test_summary_of_nothing_is_empty_not_an_error():
    payload = summary([])
    assert payload["n_trades"] == 0 and payload["strategies"] == []
    assert payload["measured"] is False
