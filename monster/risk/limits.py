"""Portfolio risk: the part that says "no" and writes down why.

Why this is not a stop-loss
===========================

A stop-loss is a decision made *inside* a position, by the same process that
wants the position to work.  Risk here is the opposite: it sees the whole book
before a leg exists, it has no view on the trade, and every refusal it makes is
a journal line the autopsy can count.  That count is the only honest measure of
whether the limits are doing anything — a limit that never fires is either
perfectly sized or decorative, and only the record can tell you which.

What is checked, and what is deliberately crude
===============================================

* gross, net delta, per-symbol and per-venue notional — arithmetic over the
  current book plus the proposed intent, so a second intent cannot sneak past a
  limit the first one already used up;
* leverage as ``gross / equity``; a book with no equity refuses everything;
* free cash against the margin the intent would consume — spot pays 100 %, a
  perp pays ``1 / max_leverage``, which is a paper approximation, not a venue's
  margin schedule;
* the daily loss budget, which is the kill switch's arithmetic: once
  ``day_pnl <= -max_daily_loss_usd`` nothing passes, because a strategy that
  lost its budget today has demonstrated something about today.

``historical_var`` is a separate function on purpose: it is a *reading*, and it
returns ``None`` (with a sentence) rather than a number when there are fewer
than :data:`MIN_OBSERVATIONS` returns.  A VaR from nine days is not a small
VaR; it is a number that will be wrong in the direction that hurts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import numpy as np

__all__ = [
    "Limits",
    "Exposure",
    "Breach",
    "check",
    "VarReport",
    "historical_var",
    "KillSwitch",
    "MIN_OBSERVATIONS",
]

#: Below this, a historical quantile is an interpolation of a handful of days.
MIN_OBSERVATIONS = 30


@dataclass(frozen=True)
class Limits:
    """The numbers an operator is willing to lose, written before the trade."""

    max_gross_usd: float = 2_000.0
    max_net_delta_usd: float = 200.0
    max_symbol_usd: float = 1_000.0
    max_venue_usd: float = 2_000.0
    max_leverage: float = 2.0
    max_daily_loss_usd: float = 20.0
    min_free_cash_usd: float = 50.0

    def __post_init__(self) -> None:
        for name in (
            "max_gross_usd",
            "max_net_delta_usd",
            "max_symbol_usd",
            "max_venue_usd",
            "max_leverage",
            "max_daily_loss_usd",
            "min_free_cash_usd",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive number, got {value!r}")
        if self.max_leverage < 1.0:
            raise ValueError("max_leverage below 1.0 would forbid a fully cash-backed spot position")

    def to_dict(self) -> Dict[str, float]:
        return {name: float(getattr(self, name)) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class Exposure:
    """The book as risk sees it: dollars per symbol, per venue, and the budget."""

    gross_usd: float = 0.0
    net_delta_usd: float = 0.0
    equity_usd: float = 0.0
    free_cash_usd: float = 0.0
    day_pnl_usd: float = 0.0
    symbol_usd: Mapping[str, float] = field(default_factory=dict)
    venue_usd: Mapping[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gross_usd": self.gross_usd,
            "net_delta_usd": self.net_delta_usd,
            "equity_usd": self.equity_usd,
            "free_cash_usd": self.free_cash_usd,
            "day_pnl_usd": self.day_pnl_usd,
            "symbol_usd": dict(self.symbol_usd),
            "venue_usd": dict(self.venue_usd),
        }


@dataclass(frozen=True)
class Breach:
    """One reason the intent may not proceed — a code for machines, a sentence for humans."""

    code: str
    detail: str

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "detail": self.detail}


def _margin_fraction(kind: str, max_leverage: float) -> float:
    return 1.0 if kind == "spot" else 1.0 / max_leverage


def check(intent, exposure: Exposure, limits: Limits) -> Tuple[Breach, ...]:
    """Every reason this intent may not proceed, in the order a human reads them.

    The check is *projected*: current book plus the whole intent.  Leg-at-a-time
    checking would let a two-leg trade pass a per-leg limit and then exceed the
    total, which is exactly the failure a delta-neutral book invites.
    """
    breaches: list[Breach] = []
    if exposure.equity_usd <= 0.0:
        breaches.append(
            Breach("NO_EQUITY", f"equity is {exposure.equity_usd:.2f}; a book without capital cannot size a trade")
        )
    if exposure.day_pnl_usd <= -limits.max_daily_loss_usd:
        breaches.append(
            Breach(
                "DAILY_LOSS",
                f"day pnl {exposure.day_pnl_usd:+.2f} reached the budget -{limits.max_daily_loss_usd:.2f}",
            )
        )
    if intent.expected_edge_bps <= 0.0:
        breaches.append(
            Breach("NO_EDGE", f"expected edge is {intent.expected_edge_bps:+.2f} bps; a negative edge is a gift")
        )

    projected_gross = exposure.gross_usd + intent.gross_usd
    if projected_gross > limits.max_gross_usd:
        breaches.append(
            Breach(
                "GROSS",
                f"gross would be {projected_gross:.2f} > {limits.max_gross_usd:.2f} "
                f"(book {exposure.gross_usd:.2f} + intent {intent.gross_usd:.2f})",
            )
        )

    projected_net = exposure.net_delta_usd + intent.net_delta_usd
    if abs(projected_net) > limits.max_net_delta_usd:
        breaches.append(
            Breach(
                "NET_DELTA",
                f"net delta would be {projected_net:+.2f}, limit ±{limits.max_net_delta_usd:.2f}",
            )
        )

    per_symbol: Dict[str, float] = {}
    per_venue: Dict[str, float] = {}
    for leg in intent.legs:
        per_symbol[leg.symbol] = per_symbol.get(leg.symbol, 0.0) + leg.notional_usd
        per_venue[leg.venue] = per_venue.get(leg.venue, 0.0) + leg.notional_usd
    for symbol, added in sorted(per_symbol.items()):
        total = exposure.symbol_usd.get(symbol, 0.0) + added
        if total > limits.max_symbol_usd:
            breaches.append(Breach("SYMBOL", f"{symbol}: {total:.2f} > {limits.max_symbol_usd:.2f} per symbol"))
    for venue, added in sorted(per_venue.items()):
        total = exposure.venue_usd.get(venue, 0.0) + added
        if total > limits.max_venue_usd:
            breaches.append(Breach("VENUE", f"{venue}: {total:.2f} > {limits.max_venue_usd:.2f} per venue"))

    if exposure.equity_usd > 0.0:
        leverage = projected_gross / exposure.equity_usd
        if leverage > limits.max_leverage:
            breaches.append(
                Breach("LEVERAGE", f"gross/equity would be {leverage:.2f}x > {limits.max_leverage:.2f}x")
            )

    margin = sum(leg.notional_usd * _margin_fraction(leg.kind, limits.max_leverage) for leg in intent.legs)
    if margin + limits.min_free_cash_usd > exposure.free_cash_usd:
        breaches.append(
            Breach(
                "NO_CASH",
                f"margin {margin:.2f} + reserve {limits.min_free_cash_usd:.2f} > free cash "
                f"{exposure.free_cash_usd:.2f}",
            )
        )
    return tuple(breaches)


@dataclass(frozen=True)
class VarReport:
    """A historical VaR/CVaR, or the sentence explaining why there is none."""

    n: int
    level: float
    notional_usd: float
    var_usd: Optional[float]
    cvar_usd: Optional[float]
    note: str = ""

    @property
    def measured(self) -> bool:
        return self.var_usd is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n": self.n,
            "level": self.level,
            "notional_usd": self.notional_usd,
            "var_usd": self.var_usd,
            "cvar_usd": self.cvar_usd,
            "measured": self.measured,
            "note": self.note,
        }

    def describe(self) -> str:
        if not self.measured:
            return f"VaR {self.level:.0%}: unmeasured ({self.note})"
        return (
            f"VaR {self.level:.0%} = {self.var_usd:.2f} USD, CVaR = {self.cvar_usd:.2f} USD "
            f"on {self.n} observations"
        )


def historical_var(
    returns: Sequence[float],
    notional_usd: float,
    *,
    level: float = 0.95,
    min_observations: int = MIN_OBSERVATIONS,
) -> VarReport:
    """Historical VaR of a notional from observed returns.

    ``returns`` are fractions (``0.01`` = 1 %), not bps.  The result is a loss
    in dollars, positive for a loss.  Fewer than ``min_observations`` returns
    produce ``var_usd=None``: the quantile of a handful of days is a statement
    about those days, not about the risk.
    """
    values = np.asarray(list(returns), dtype=float)
    values = values[np.isfinite(values)]
    if not 0.5 < level < 1.0:
        raise ValueError(f"level must be in (0.5, 1), got {level}")
    if notional_usd <= 0.0:
        raise ValueError("notional_usd must be positive")
    if values.size < min_observations:
        return VarReport(
            n=int(values.size),
            level=float(level),
            notional_usd=float(notional_usd),
            var_usd=None,
            cvar_usd=None,
            note=f"{values.size} observations < {min_observations}: a quantile this thin is a guess",
        )
    quantile = float(np.quantile(values, 1.0 - level))
    var_usd = max(0.0, -quantile * notional_usd)
    tail = values[values <= quantile]
    cvar_usd = max(var_usd, -float(tail.mean()) * notional_usd) if tail.size else var_usd
    return VarReport(
        n=int(values.size),
        level=float(level),
        notional_usd=float(notional_usd),
        var_usd=float(var_usd),
        cvar_usd=float(cvar_usd),
    )


@dataclass
class KillSwitch:
    """The one state that survives a restart, and only a named human clears it.

    ``reset`` demands an operator name rather than a flag: a process that can
    clear its own kill switch will clear it on the first bad day, which is
    exactly the day it was installed for.
    """

    tripped: bool = False
    reason: str = ""
    ts: Optional[float] = None

    def trip(self, reason: str, *, ts: Optional[float] = None) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a kill switch needs a reason")
        self.tripped = True
        self.reason = reason.strip()
        self.ts = None if ts is None else float(ts)

    def reset(self, operator: str) -> None:
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("a kill switch may only be reset by a named operator")
        self.tripped = False
        self.reason = f"reset by {operator.strip()}"
        self.ts = None

    def to_dict(self) -> Dict[str, Any]:
        return {"tripped": self.tripped, "reason": self.reason, "ts": self.ts}

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "KillSwitch":
        return cls(
            tripped=bool(document.get("tripped", False)),
            reason=str(document.get("reason", "")),
            ts=None if document.get("ts") is None else float(document["ts"]),
        )
