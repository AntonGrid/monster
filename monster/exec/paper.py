"""Paper execution: the venue that charges you, so the backtest cannot forget to.

Why a paper venue is not a simulator
====================================

A backtest computes what a strategy *would* have made if its legs filled at the
close.  The difference between that number and reality is not "slippage", it is
a list: the fee on each leg, the spread you crossed to get in, the spread you
cross to get out, the funding you paid while you waited, and the margin that
was unavailable for anything else.  Each of those is a small number and there
are four of them, which is why a strategy that "makes 3 bps per trade" can lose
money while every individual assumption looks reasonable.

So the paper venue is written as an accountant, not as an optimiser:

* a **buy** fills at the ask plus slippage, a **sell** at the bid minus it — no
  mid-price fills, ever;
* fees are taken from cash at the moment of the fill, in dollars;
* a spot leg pays 100 % of its notional as margin, a perp leg pays
  ``1 / leverage`` — an approximation of a venue's margin, labelled as one;
* funding is accrued over whole settlement periods and paid in cash, with the
  sign the venue uses: a positive rate moves money from longs to shorts;
* a **spot short is refused**, because the venues this workspace can reach do
  not lend spot.  That refusal is the reason the carry strategy is built as
  long spot + short perp rather than the reverse;
* an unpriced position is an error (``Unpriced``), not a mark at its entry: a
  book that values itself at cost is a book that never has a bad day.

``equity = cash + margin + unrealised``, for both kinds, because a long spot
*is* margin posted in full.  One equation, so the risk module and the autopsy
cannot disagree about what the account is worth.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from monster.contracts import ClosedTrade, Fill, Intent, Quote, Snapshot
from monster.risk.limits import Exposure

__all__ = ["Unpriced", "PaperPosition", "ExecutionResult", "PaperVenue"]

#: Key of a position: one per (venue, symbol, kind).  Two strategies holding the
#: same instrument are the same position as far as the venue is concerned.
PositionKey = Tuple[str, str, str]


class Unpriced(RuntimeError):
    """A position or a leg cannot be valued from the snapshot in hand."""


@dataclass
class PaperPosition:
    """One open position, with the three accumulators that decide its return."""

    venue: str
    symbol: str
    kind: str
    strategy: str
    entry_side: str
    qty: float
    avg_price: float
    margin_usd: float
    entry_fee_usd: float
    cost_usd: float
    funding_usd: float
    opened_ts: float
    last_funding_ts: float

    @property
    def key(self) -> PositionKey:
        return (self.venue, self.symbol, self.kind)

    def notional(self, mark: float) -> float:
        return abs(self.qty) * mark

    def unrealized(self, mark: float) -> float:
        return (mark - self.avg_price) * self.qty

    def to_dict(self) -> Dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "kind": self.kind,
            "strategy": self.strategy,
            "entry_side": self.entry_side,
            "qty": self.qty,
            "avg_price": self.avg_price,
            "margin_usd": self.margin_usd,
            "entry_fee_usd": self.entry_fee_usd,
            "cost_usd": self.cost_usd,
            "funding_usd": self.funding_usd,
            "opened_ts": self.opened_ts,
            "last_funding_ts": self.last_funding_ts,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "PaperPosition":
        return cls(**{name: document[name] for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class FundingAccrual:
    """One settlement: how many periods, at what rate, and which way the cash went."""

    venue: str
    symbol: str
    periods: int
    rate: float
    period_hours: float
    notional_usd: float
    amount_usd: float
    ts: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "periods": self.periods,
            "rate": self.rate,
            "period_hours": self.period_hours,
            "notional_usd": self.notional_usd,
            "amount_usd": self.amount_usd,
            "ts": self.ts,
        }


@dataclass(frozen=True)
class FundingReport:
    """What was credited, and which positions could not be credited at all."""

    accruals: Tuple[FundingAccrual, ...] = ()
    unmeasured: Tuple[str, ...] = ()

    @property
    def total_usd(self) -> float:
        return sum(accrual.amount_usd for accrual in self.accruals)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accruals": [accrual.to_dict() for accrual in self.accruals],
            "unmeasured": list(self.unmeasured),
            "total_usd": self.total_usd,
        }


@dataclass(frozen=True)
class ExecutionResult:
    """What the venue did with an intent: fills, or the reasons it did nothing."""

    intent_id: str
    fills: Tuple[Fill, ...] = ()
    refused: Tuple[str, ...] = ()

    @property
    def executed(self) -> bool:
        return bool(self.fills) and not self.refused

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "fills": [fill.to_dict() for fill in self.fills],
            "refused": list(self.refused),
            "executed": self.executed,
        }


class PaperVenue:
    """An accountant with a book: charges fees, crosses spreads, accrues funding."""

    def __init__(
        self,
        *,
        cash_usd: float = 1_000.0,
        fee_bps: float = 10.0,
        slippage_bps: float = 2.0,
        leverage: float = 2.0,
        ts: float = 0.0,
    ) -> None:
        for name, value in (("cash_usd", cash_usd), ("fee_bps", fee_bps), ("slippage_bps", slippage_bps)):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative number, got {value!r}")
        if cash_usd <= 0:
            raise ValueError("cash_usd must be positive")
        if leverage < 1.0:
            raise ValueError("leverage below 1.0 would forbid a fully cash-backed spot position")
        self.cash = float(cash_usd)
        self.fee_bps = float(fee_bps)
        self.slippage_bps = float(slippage_bps)
        self.leverage = float(leverage)
        self.positions: Dict[PositionKey, PaperPosition] = {}
        self.closed: List[ClosedTrade] = []
        self.day_start_equity = float(cash_usd)
        self.day_start_ts = float(ts)

    # ── pricing ─────────────────────────────────────────────────────────────

    def _quote(self, snapshot: Snapshot, venue: str, symbol: str) -> Quote:
        quote = snapshot.quote(venue, symbol)
        if quote is None:
            raise Unpriced(f"no quote for {venue} {symbol} in the snapshot")
        return quote

    def _fill_price(self, quote: Quote, side: str) -> float:
        """Cross the spread and then some: a buy pays the ask, a sell hits the bid."""
        slip = self.slippage_bps / 1e4
        return quote.ask * (1.0 + slip) if side == "buy" else quote.bid * (1.0 - slip)

    def _margin_fraction(self, kind: str) -> float:
        return 1.0 if kind == "spot" else 1.0 / self.leverage

    # ── execution ───────────────────────────────────────────────────────────

    def execute(self, intent: Intent, snapshot: Snapshot, *, ts: Optional[float] = None) -> ExecutionResult:
        """Fill every leg or none of them.

        A carry trade is two legs; filling one and refusing the other leaves a
        naked direction that no one asked for.  So the venue prices every leg
        first and refuses the whole intent if any part is unknown — including a
        leg that would flip an existing position, which is a close written as an
        open and belongs in :meth:`close`.
        """
        stamp = float(snapshot.ts if ts is None else ts)
        refused: List[str] = []
        priced: List[Tuple[Any, Quote, float, float, float]] = []
        for leg in intent.legs:
            try:
                quote = self._quote(snapshot, leg.venue, leg.symbol)
            except Unpriced as error:
                refused.append(str(error))
                continue
            existing = self.positions.get((leg.venue, leg.symbol, leg.kind))
            if existing is None and leg.kind == "spot" and leg.side == "sell":
                refused.append(f"{leg.venue} {leg.symbol}: spot short is refused (no lending on spot here)")
                continue
            if existing is not None and (existing.qty > 0) != (leg.side == "buy"):
                refused.append(
                    f"{leg.venue} {leg.symbol} {leg.kind}: an open of the opposite side would flip the "
                    "position; close it first"
                )
                continue
            price = self._fill_price(quote, leg.side)
            qty = leg.notional_usd / price
            margin = leg.notional_usd * self._margin_fraction(leg.kind)
            priced.append((leg, quote, price, qty, margin))

        needed = sum(item[4] + item[0].notional_usd * self.fee_bps / 1e4 for item in priced)
        if not refused and needed > self.cash + 1e-9:
            refused.append(f"insufficient cash: needs {needed:.2f}, free {self.cash:.2f}")
        if refused:
            return ExecutionResult(intent_id=intent.id, fills=(), refused=tuple(sorted(set(refused))))

        fills: List[Fill] = []
        for index, (leg, quote, price, qty, margin) in enumerate(priced):
            fill = Fill(
                ts=stamp,
                strategy=intent.strategy,
                venue=leg.venue,
                symbol=leg.symbol,
                kind=leg.kind,
                side=leg.side,
                qty=qty,
                price=price,
                fee_usd=leg.notional_usd * self.fee_bps / 1e4,
                notional_usd=leg.notional_usd,
                leg_index=index,
            )
            self.apply_fill(fill)
            fills.append(fill)
        return ExecutionResult(intent_id=intent.id, fills=tuple(fills), refused=())

    def apply_fill(self, fill: Fill) -> PaperPosition:
        """Post one fill to the book: margin and fee out of cash, position in.

        Public because the journal holds every fill: a replay that recomputed
        the book with different arithmetic would prove nothing about the book it
        claims to check.  The cash check stays in :meth:`execute`, where the
        intent is priced as a whole — a replay must not refuse a fill that
        already happened.
        """
        margin = fill.notional_usd * self._margin_fraction(fill.kind)
        key = (fill.venue, fill.symbol, fill.kind)
        existing = self.positions.get(key)
        if existing is None:
            position = PaperPosition(
                venue=fill.venue,
                symbol=fill.symbol,
                kind=fill.kind,
                strategy=fill.strategy,
                entry_side=fill.side,
                qty=fill.qty if fill.side == "buy" else -fill.qty,
                avg_price=fill.price,
                margin_usd=margin,
                entry_fee_usd=fill.fee_usd,
                cost_usd=fill.notional_usd,
                funding_usd=0.0,
                opened_ts=fill.ts,
                last_funding_ts=fill.ts,
            )
            self.positions[key] = position
        else:
            signed = fill.qty if fill.side == "buy" else -fill.qty
            weight = abs(existing.qty) + abs(signed)
            existing.avg_price = (existing.avg_price * abs(existing.qty) + fill.price * abs(signed)) / weight
            existing.qty += signed
            existing.margin_usd += margin
            existing.entry_fee_usd += fill.fee_usd
            existing.cost_usd += fill.notional_usd
            position = existing
        self.cash -= margin + fill.fee_usd
        return position

    # ── closing ─────────────────────────────────────────────────────────────

    def close(
        self,
        key: PositionKey,
        snapshot: Snapshot,
        *,
        reason: str,
        ts: Optional[float] = None,
    ) -> ClosedTrade:
        """Close one position at the far side of the book and book the result.

        A long exits at the bid, a short at the ask: the same rule as entry,
        applied in the direction that costs money.  ``realized_usd`` is after
        entry fee, exit fee and funding — the number the autopsy is allowed to
        believe.
        """
        position = self.positions.get(key)
        if position is None:
            raise KeyError(f"no open position {key}")
        stamp = float(snapshot.ts if ts is None else ts)
        quote = self._quote(snapshot, position.venue, position.symbol)
        exit_side = "sell" if position.qty > 0 else "buy"
        price = self._fill_price(quote, exit_side)
        quantity = abs(position.qty)
        gross = (price - position.avg_price) * position.qty
        exit_notional = quantity * price
        exit_fee = exit_notional * self.fee_bps / 1e4
        net = gross - position.entry_fee_usd - exit_fee + position.funding_usd
        trade = ClosedTrade(
            strategy=position.strategy,
            venue=position.venue,
            symbol=position.symbol,
            kind=position.kind,
            entry_side=position.entry_side,
            qty=quantity,
            entry_price=position.avg_price,
            exit_price=price,
            entry_notional_usd=position.cost_usd,
            fees_usd=position.entry_fee_usd + exit_fee,
            funding_usd=position.funding_usd,
            realized_usd=net,
            bps=net / position.cost_usd * 1e4,
            opened_ts=position.opened_ts,
            closed_ts=stamp,
            reason=reason,
        )
        self.apply_close(trade)
        return trade

    def apply_close(self, trade: ClosedTrade) -> float:
        """Post a closed trade to the book; return the net the position implies.

        The returned number is computed from the position and the recorded exit
        price rather than read from ``trade.realized_usd``: comparing the two is
        how a replay notices that the journal's arithmetic and the book's have
        drifted apart.  ``cash`` is credited with margin and gross minus the exit
        fee, because the entry fee and the funding were paid as they happened.
        """
        key = (trade.venue, trade.symbol, trade.kind)
        position = self.positions.get(key)
        if position is None:
            raise KeyError(f"no open position {key} for the closed trade at {trade.closed_ts}")
        exit_fee = abs(position.qty) * trade.exit_price * self.fee_bps / 1e4
        gross = (trade.exit_price - position.avg_price) * position.qty
        net = gross - position.entry_fee_usd - exit_fee + position.funding_usd
        self.cash += position.margin_usd + gross - exit_fee
        self.closed.append(trade)
        del self.positions[key]
        return net

    # ── funding ─────────────────────────────────────────────────────────────

    def accrue_funding(self, snapshot: Snapshot, *, ts: Optional[float] = None) -> "FundingReport":
        """Pay or collect funding for whole settlement periods that have passed.

        The rate is the venue's published fraction per settlement; a positive
        rate moves cash from the long to the short.  ``last_funding_ts`` starts
        at the open, so a position opened mid-period is charged for a full one —
        a documented approximation, because the snapshot does not carry the
        venue's settlement calendar.  It errs against the strategy, which is the
        direction an approximation should err.
        """
        stamp = float(snapshot.ts if ts is None else ts)
        accruals: List[FundingAccrual] = []
        unmeasured: List[str] = []
        for position in (self.positions[key] for key in sorted(self.positions)):
            if position.kind != "perp":
                continue
            rate_quote = snapshot.funding_quote(position.venue, position.symbol)
            if rate_quote is None:
                unmeasured.append(
                    f"{position.venue} {position.symbol}: no funding rate in the snapshot, "
                    "so the carry leg's income is unknown"
                )
                continue
            period_seconds = rate_quote.period_hours * 3600.0
            elapsed = stamp - position.last_funding_ts
            periods = int(elapsed // period_seconds) if period_seconds > 0 else 0
            if periods <= 0:
                continue
            mark = self._mark(snapshot, position)
            notional = abs(position.qty) * mark
            amount = -math.copysign(1.0, position.qty) * rate_quote.rate * notional * periods
            self.cash += amount
            position.funding_usd += amount
            position.last_funding_ts += periods * period_seconds
            accruals.append(
                FundingAccrual(
                    venue=position.venue,
                    symbol=position.symbol,
                    periods=periods,
                    rate=rate_quote.rate,
                    period_hours=rate_quote.period_hours,
                    notional_usd=notional,
                    amount_usd=amount,
                    ts=stamp,
                )
            )
        return FundingReport(accruals=tuple(accruals), unmeasured=tuple(unmeasured))

    # ── valuation ───────────────────────────────────────────────────────────

    def _mark(self, snapshot: Snapshot, position: PaperPosition) -> float:
        quote = snapshot.quote(position.venue, position.symbol)
        if quote is None:
            raise Unpriced(
                f"cannot mark {position.venue} {position.symbol}: no quote in the snapshot "
                "(valuing at cost is how a book stops having bad days)"
            )
        return quote.mid

    def equity(self, snapshot: Snapshot) -> float:
        """``cash + margin + unrealised`` — the same equation for spot and perp."""
        total = self.cash
        for position in self.positions.values():
            total += position.margin_usd + position.unrealized(self._mark(snapshot, position))
        return total

    def exposure(self, snapshot: Snapshot) -> Exposure:
        """The book in the shape risk wants: dollars, by symbol and by venue."""
        gross = net = 0.0
        by_symbol: Dict[str, float] = {}
        by_venue: Dict[str, float] = {}
        for position in self.positions.values():
            mark = self._mark(snapshot, position)
            value = abs(position.qty) * mark
            gross += value
            net += position.qty * mark
            by_symbol[position.symbol] = by_symbol.get(position.symbol, 0.0) + value
            by_venue[position.venue] = by_venue.get(position.venue, 0.0) + value
        equity = self.equity(snapshot)
        return Exposure(
            gross_usd=gross,
            net_delta_usd=net,
            equity_usd=equity,
            free_cash_usd=self.cash,
            day_pnl_usd=equity - self.day_start_equity,
            symbol_usd=by_symbol,
            venue_usd=by_venue,
        )

    def roll_day(self, snapshot: Snapshot) -> bool:
        """Start a new loss budget when the UTC date changes; ``True`` if it did.

        The budget is measured from the equity at the boundary, not from cash:
        an open position that moved against the book overnight has already
        spent part of today's allowance.
        """
        if int(snapshot.ts // 86_400) == int(self.day_start_ts // 86_400):
            return False
        self.day_start_equity = self.equity(snapshot)
        self.day_start_ts = float(snapshot.ts)
        return True

    # ── persistence ─────────────────────────────────────────────────────────

    def state(self) -> Dict[str, Any]:
        """A cache for the next process; the journal remains the audit trail."""
        return {
            "cash": self.cash,
            "fee_bps": self.fee_bps,
            "slippage_bps": self.slippage_bps,
            "leverage": self.leverage,
            "day_start_equity": self.day_start_equity,
            "day_start_ts": self.day_start_ts,
            "positions": [position.to_dict() for position in self.positions.values()],
            "closed": [trade.to_dict() for trade in self.closed],
        }

    @classmethod
    def from_state(cls, document: Mapping[str, Any]) -> "PaperVenue":
        venue = cls(
            cash_usd=float(document["cash"]),
            fee_bps=float(document["fee_bps"]),
            slippage_bps=float(document["slippage_bps"]),
            leverage=float(document["leverage"]),
            ts=float(document["day_start_ts"]),
        )
        venue.day_start_equity = float(document["day_start_equity"])
        for item in document.get("positions", []):
            position = PaperPosition.from_dict(item)
            venue.positions[position.key] = position
        venue.closed = [ClosedTrade.from_dict(item) for item in document.get("closed", [])]
        return venue
