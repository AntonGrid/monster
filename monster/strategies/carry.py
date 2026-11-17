"""Funding carry: the first strategy because it is the first one that can be honest.

Why carry, and not the arb the latency people want
=================================================

The workspace has a fast Rust bot and a research stack that once showed a 4 h
cross-section with a t-statistic of 5.77.  Neither is the right first trade.
Latency arbitrage is a race against firms that colocate; a cross-section signal
that has not been re-run on a fresh panel is a hypothesis, not an edge.  Funding
carry is different in kind:

* the thing being harvested — the funding rate — is **published by the venue**,
  with a period, before the position exists;
* the position is **delta-neutral**: long spot, short perp, so the PnL does not
  depend on whether BTC goes up;
* the costs are **enumerable**: four fills, two spreads, a basis you can read
  off the book, and the funding you are trying to collect;
* and the failure mode is **legible**: if the PnL is wrong, the arithmetic in
  ``monster.exec.paper`` is wrong, and the autopsy can find it.

That is the standard this module holds itself to: no candidate is accepted
unless its *net* edge — funding minus four fees, four slippages and the basis —
clears the threshold.  A 3 bp gross carry that costs 4.2 bps to put on is a
donation, and printing it as "3 bps" is how a strategy lies to its author.

Two legs, one venue
===================

Cross-venue carry (buy spot on A, short perp on B) is a different trade: it
needs a transfer assumption, a withdrawal limit and a bridge time, none of which
the snapshot contains.  This module therefore refuses a pair whose spot and perp
are not on the same venue, and says so in the candidate's reason rather than
silently pricing a transfer it cannot make.

A refused candidate is not an error
===================================

Every pair the scan looks at produces a :class:`CarryCandidate`, accepted or
not, with a sentence.  The refusals are the interesting half of the record: they
are what the monster will read when it asks whether the threshold was right.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from monster.contracts import Intent, Leg, Snapshot
from monster.instruments import PAIRS

__all__ = ["CarryCandidate", "CarryScan", "scan_carry", "DEFAULT_PAIRS"]

#: The pairs the first paper cycle knows how to price.  The table lives in
#: :mod:`monster.instruments` so the data layer and the strategy cannot disagree
#: about which perp belongs to which spot: a wrong pair is a trade in the wrong
#: asset.
DEFAULT_PAIRS: Mapping[str, Tuple[str, str]] = PAIRS


@dataclass(frozen=True)
class CarryCandidate:
    """One pair, priced end to end, with the reason it was or was not taken."""

    pair: str
    venue: str
    spot_symbol: str
    perp_symbol: str
    funding_rate: Optional[float]
    period_hours: Optional[float]
    annualized_pct: Optional[float]
    funding_bps: Optional[float]
    cost_bps: Optional[float]
    net_edge_bps: Optional[float]
    accepted: bool
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pair": self.pair,
            "venue": self.venue,
            "spot_symbol": self.spot_symbol,
            "perp_symbol": self.perp_symbol,
            "funding_rate": self.funding_rate,
            "period_hours": self.period_hours,
            "annualized_pct": self.annualized_pct,
            "funding_bps": self.funding_bps,
            "cost_bps": self.cost_bps,
            "net_edge_bps": self.net_edge_bps,
            "accepted": self.accepted,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CarryScan:
    """The whole scan: what was looked at, what was proposed, and why not."""

    ts: float
    horizon_hours: float
    min_edge_bps: float
    candidates: Tuple[CarryCandidate, ...] = ()
    intents: Tuple[Intent, ...] = ()

    @property
    def accepted(self) -> Tuple[CarryCandidate, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.accepted)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "horizon_hours": self.horizon_hours,
            "min_edge_bps": self.min_edge_bps,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "intents": [intent.to_dict() for intent in self.intents],
        }

    def describe(self) -> str:
        if not self.candidates:
            return "carry: no pair was priceable"
        lines = [f"carry: {len(self.accepted)}/{len(self.candidates)} accepted at {self.horizon_hours:.0f}h"]
        for candidate in self.candidates:
            lines.append(f"  {candidate.pair} {candidate.venue}: {candidate.reason}")
        return "\n".join(lines)


def scan_carry(
    snapshot: Snapshot,
    pairs: Optional[Mapping[str, Tuple[str, str]]] = None,
    *,
    venue: str = "htx",
    notional_usd: float = 100.0,
    horizon_hours: float = 168.0,
    fee_bps: float = 5.0,
    slippage_bps: float = 2.0,
    min_edge_bps: float = 10.0,
) -> CarryScan:
    """Price every pair's funding against the cost of holding it for a horizon.

    ``notional_usd`` is per leg, so the intent's gross is twice that.  The
    horizon must be a whole number of settlements or the arithmetic understates
    the income; it does not need to be, but a partial period is not paid and
    this function does not pretend otherwise (it simply uses ``horizon/period``,
    so a 4 h horizon on an 8 h contract earns half a settlement and says so by
    the number it prints).

    Costs are one-time, funding accrues, so the comparison is only meaningful
    over a holding period: a 5 bp funding rate is not "an edge" on an 8 h
    horizon that pays 28 bps of fills to enter and leave.
    """
    if notional_usd <= 0.0:
        raise ValueError("notional_usd must be positive")
    if horizon_hours <= 0.0:
        raise ValueError("horizon_hours must be positive")
    if fee_bps < 0.0 or slippage_bps < 0.0:
        raise ValueError("fees and slippage are costs; they cannot be negative")
    if min_edge_bps < 0.0:
        raise ValueError("min_edge_bps must be non-negative")
    venue = venue.strip().lower()
    table = dict(DEFAULT_PAIRS if pairs is None else pairs)

    candidates: list[CarryCandidate] = []
    intents: list[Intent] = []
    for pair, (spot_symbol, perp_symbol) in sorted(table.items()):
        spot_quote = snapshot.quote(venue, spot_symbol)
        perp_quote = snapshot.quote(venue, perp_symbol)
        rate_quote = snapshot.funding_quote(venue, perp_symbol)
        missing = [
            name
            for name, value in (
                (f"spot {spot_symbol}", spot_quote),
                (f"perp {perp_symbol}", perp_quote),
                (f"funding {perp_symbol}", rate_quote),
            )
            if value is None
        ]
        if missing:
            candidates.append(
                CarryCandidate(
                    pair=pair,
                    venue=venue,
                    spot_symbol=spot_symbol,
                    perp_symbol=perp_symbol,
                    funding_rate=None,
                    period_hours=None,
                    annualized_pct=None,
                    funding_bps=None,
                    cost_bps=None,
                    net_edge_bps=None,
                    accepted=False,
                    reason=f"unmeasured: no {', '.join(missing)} in the snapshot",
                )
            )
            continue

        assert spot_quote is not None and perp_quote is not None and rate_quote is not None
        funding_bps = rate_quote.rate * (horizon_hours / rate_quote.period_hours) * 1e4
        annualized_pct = rate_quote.daily_rate * 365.0 * 100.0
        midpoint = 0.5 * (spot_quote.mid + perp_quote.mid)
        # Entering long spot at the ask and short perp at the bid loses the
        # basis when the perp is cheap.  A perp premium is *not* booked as
        # income: it can revert before the exit, so the cost is floored at zero.
        basis_bps = max(0.0, (spot_quote.ask - perp_quote.bid) / midpoint * 1e4)
        cost_bps = 4.0 * (fee_bps + slippage_bps) + basis_bps
        net_edge_bps = funding_bps - cost_bps

        if rate_quote.rate <= 0.0:
            reason = (
                f"refused: funding rate {rate_quote.rate:+.6f} per {rate_quote.period_hours:g}h is not paid "
                "to a short; the reverse carry needs a spot borrow this venue does not offer"
            )
            accepted = False
        elif net_edge_bps < min_edge_bps:
            reason = (
                f"refused: funding {funding_bps:+.2f} bps - costs {cost_bps:.2f} bps "
                f"(4 fills + basis {basis_bps:.2f}) = {net_edge_bps:+.2f} bps < {min_edge_bps:.2f}"
            )
            accepted = False
        else:
            reason = (
                f"accepted: funding {funding_bps:+.2f} bps - costs {cost_bps:.2f} bps "
                f"(4 fills + basis {basis_bps:.2f}) = {net_edge_bps:+.2f} bps >= {min_edge_bps:.2f}"
            )
            accepted = True

        candidates.append(
            CarryCandidate(
                pair=pair,
                venue=venue,
                spot_symbol=spot_symbol,
                perp_symbol=perp_symbol,
                funding_rate=rate_quote.rate,
                period_hours=rate_quote.period_hours,
                annualized_pct=annualized_pct,
                funding_bps=funding_bps,
                cost_bps=cost_bps,
                net_edge_bps=net_edge_bps,
                accepted=accepted,
                reason=reason,
            )
        )
        if accepted:
            intents.append(
                Intent(
                    strategy="carry",
                    legs=(
                        Leg(venue=venue, symbol=spot_symbol, side="buy", kind="spot", notional_usd=notional_usd),
                        Leg(venue=venue, symbol=perp_symbol, side="sell", kind="perp", notional_usd=notional_usd),
                    ),
                    expected_edge_bps=net_edge_bps,
                    horizon_hours=horizon_hours,
                    rationale=reason,
                )
            )

    return CarryScan(
        ts=float(snapshot.ts),
        horizon_hours=float(horizon_hours),
        min_edge_bps=float(min_edge_bps),
        candidates=tuple(candidates),
        intents=tuple(intents),
    )
