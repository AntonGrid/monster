"""Autopsy: what the closed trades say, and what they cannot say yet.

Why an autopsy is not a PnL chart
=================================

A strategy that made 12 USD looks the same whether it made it in eighty small
trades or in one lucky one.  The difference is not cosmetic: the first has an
expectancy, the second has an anecdote, and a system that learns from anecdotes
learns noise.  So every closed trade is summarised along the axes that decide
whether the record means anything:

* **n** — how many trades, and therefore how much of the distribution exists;
* **mean/median/sigma in bps** — the edge and its spread, not in dollars,
  because dollars conflate size with skill;
* **t** — the one-sample statistic the ENRG-AI gate already uses;
* **resolution** — the smallest edge this many trades *could* have named, from
  ``agent.backtest.power.minimum_edge_bps``.  If the measured mean is below it,
  the honest verdict is *unresolvable*, not *profitable*.

Refusing to answer
==================

With fewer than two trades there is no spread, so there is no resolution and no
t-statistic: the autopsy returns ``None`` for them and says
``unmeasurable: n=1``.  With two trades it will print a number and immediately
label it unresolved, because two trades cannot resolve a carry of a few bps —
which is the correct thing to tell an operator watching the first day of paper.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from monster import enrg
from monster.contracts import ClosedTrade

__all__ = ["TradeAutopsy", "autopsy", "summary", "MIN_TRADES"]

#: Below this there is no spread to divide by; the autopsy says so rather than
#: printing a t-statistic of one observation.
MIN_TRADES = 2


@dataclass(frozen=True)
class TradeAutopsy:
    """One strategy's record, with the resolution it does and does not have."""

    strategy: str
    n: int
    wins: int
    net_usd: float
    fees_usd: float
    funding_usd: float
    mean_bps: Optional[float]
    median_bps: Optional[float]
    sigma_bps: Optional[float]
    t_stat: Optional[float]
    min_resolvable_bps: Optional[float]
    resolvable: Optional[bool]
    note: str = ""

    @property
    def win_rate(self) -> Optional[float]:
        return None if self.n == 0 else self.wins / self.n

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "n": self.n,
            "wins": self.wins,
            "win_rate": self.win_rate,
            "net_usd": self.net_usd,
            "fees_usd": self.fees_usd,
            "funding_usd": self.funding_usd,
            "mean_bps": self.mean_bps,
            "median_bps": self.median_bps,
            "sigma_bps": self.sigma_bps,
            "t_stat": self.t_stat,
            "min_resolvable_bps": self.min_resolvable_bps,
            "resolvable": self.resolvable,
            "note": self.note,
        }

    def describe(self) -> str:
        if self.n < MIN_TRADES:
            return (
                f"{self.strategy}: unmeasurable: n={self.n}, net {self.net_usd:+.2f} USD "
                f"(fees {self.fees_usd:.2f}, funding {self.funding_usd:+.4f})"
            )
        head = (
            f"{self.strategy}: n={self.n} win {self.win_rate:.0%} net {self.net_usd:+.2f} USD "
            f"mean {self.mean_bps:+.2f} bps"
        )
        if self.t_stat is not None:
            head += f" t={self.t_stat:+.2f}"
        if self.resolvable is False:
            head += f" UNRESOLVABLE (needs {self.min_resolvable_bps:.1f} bps at this n)"
        elif self.resolvable is True:
            head += f" resolved above {self.min_resolvable_bps:.1f} bps"
        return head


def _resolution(mean_bps: float, sigma_bps: float, n: int) -> Tuple[Optional[float], Optional[bool], str]:
    """Ask ENRG-AI's gate how small an edge this sample could have named."""
    try:
        gate = enrg.power()
    except ModuleNotFoundError as error:
        return None, None, f"resolution gate unavailable ({error})"
    threshold = float(gate.minimum_edge_bps(n, sigma_bps))
    if sigma_bps == 0.0:
        return threshold, abs(mean_bps) > 0.0, "zero variance: identical trades, t is undefined"
    return threshold, abs(mean_bps) >= threshold, ""


def autopsy(trades: Sequence[ClosedTrade]) -> Tuple[TradeAutopsy, ...]:
    """Summarise closed trades per strategy, refusing what the sample cannot say."""
    by_strategy: Dict[str, List[ClosedTrade]] = {}
    for trade in trades:
        by_strategy.setdefault(trade.strategy, []).append(trade)

    results: List[TradeAutopsy] = []
    for strategy, group in sorted(by_strategy.items()):
        bps = [float(trade.bps) for trade in group]
        n = len(bps)
        net = sum(trade.realized_usd for trade in group)
        fees = sum(trade.fees_usd for trade in group)
        funding = sum(trade.funding_usd for trade in group)
        wins = sum(1 for trade in group if trade.realized_usd > 0.0)
        if n < MIN_TRADES:
            results.append(
                TradeAutopsy(
                    strategy=strategy,
                    n=n,
                    wins=wins,
                    net_usd=net,
                    fees_usd=fees,
                    funding_usd=funding,
                    mean_bps=None,
                    median_bps=None,
                    sigma_bps=None,
                    t_stat=None,
                    min_resolvable_bps=None,
                    resolvable=None,
                    note=f"unmeasurable: n={n} < {MIN_TRADES}",
                )
            )
            continue
        mean = statistics.fmean(bps)
        sigma = statistics.stdev(bps)
        t_stat = None if sigma == 0.0 else mean / (sigma / (n ** 0.5))
        threshold, resolvable, note = _resolution(mean, sigma, n)
        results.append(
            TradeAutopsy(
                strategy=strategy,
                n=n,
                wins=wins,
                net_usd=net,
                fees_usd=fees,
                funding_usd=funding,
                mean_bps=mean,
                median_bps=statistics.median(bps),
                sigma_bps=sigma,
                t_stat=t_stat,
                min_resolvable_bps=threshold,
                resolvable=resolvable,
                note=note,
            )
        )
    return tuple(results)


def summary(trades: Sequence[ClosedTrade]) -> Dict[str, Any]:
    """The autopsy as a journal payload: one call, no hidden state."""
    reports = autopsy(trades)
    return {
        "n_trades": len(trades),
        "net_usd": sum(trade.realized_usd for trade in trades),
        "fees_usd": sum(trade.fees_usd for trade in trades),
        "funding_usd": sum(trade.funding_usd for trade in trades),
        "strategies": [report.to_dict() for report in reports],
        "measured": any(report.resolvable is not None for report in reports),
    }
