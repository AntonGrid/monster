"""The vocabulary every contour speaks, and the validation that makes it a contract.

Why a module of dataclasses is the first file
=============================================

The monster joins three languages and two processes.  A join is only as good as
its weakest field: a funding rate read as ``0`` where the venue said ``null``
turns "unknown" into "no carry" for a third of the market (the lesson
``agent/venues/funding.py`` learned the hard way), and a short leg whose
``side`` was spelled differently on two sides of a socket becomes a long.

So the wire is defined once, here, and every constructor validates:

* prices are finite and positive, ``ask >= bid``;
* a funding rate may be negative but not ``NaN``; its period is positive;
* a leg is one of four kinds (``spot``/``perp`` × ``buy``/``sell``) and carries
  a positive notional;
* an intent has at least one leg, a finite edge, a positive horizon and an id
  derived from its own content — two processes that build the same intent get
  the same id, and an id that does not match its payload is a bug, not a
  detail.

Unknown keys in a JSON document are refused rather than ignored.  A typo in a
snapshot is then an error on the *reading* side, where it is cheap, not a
silently missing leg on the *trading* side, where it is not.

Nothing here rounds: rounding is a display decision, and the numbers that reach
the journal are the numbers that were used.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

__all__ = [
    "ContractError",
    "canonical",
    "Quote",
    "FundingQuote",
    "Snapshot",
    "Leg",
    "Intent",
    "Fill",
    "ClosedTrade",
]

SIDES = ("buy", "sell")
KINDS = ("spot", "perp")


class ContractError(ValueError):
    """A document or an object that does not mean what it says."""


def canonical(obj: Any) -> str:
    """One spelling for a value, so a hash can be compared across processes.

    Keys sorted, no spaces, no ``NaN``/``Infinity`` (the JSON default lets both
    through, and ``NaN != NaN`` would make every journal line verify against
    itself only by accident).
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _number(name: str, value: Any, *, positive: bool = False, allow_zero: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{name} must be a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise ContractError(f"{name} must be finite, got {number!r}")
    if positive and number < 0.0:
        raise ContractError(f"{name} must be non-negative, got {number!r}")
    if not allow_zero and number == 0.0:
        raise ContractError(f"{name} must be non-zero")
    return number


def _text(name: str, value: Any, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{name} must be a string, got {type(value).__name__}")
    cleaned = value.strip()
    if required and not cleaned:
        raise ContractError(f"{name} must not be empty")
    return cleaned


def _keys(document: Mapping[str, Any], required: Iterable[str], optional: Iterable[str] = ()) -> None:
    required_set, optional_set = set(required), set(optional)
    missing = sorted(required_set - set(document))
    if missing:
        raise ContractError(f"missing field(s): {', '.join(missing)}")
    unknown = sorted(set(document) - required_set - optional_set)
    if unknown:
        raise ContractError(f"unknown field(s): {', '.join(unknown)} (a typo is refused, not ignored)")


@dataclass(frozen=True)
class Quote:
    """One venue's top of book for one symbol, at one instant."""

    venue: str
    symbol: str
    bid: float
    ask: float
    ts: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "venue", _text("venue", self.venue).lower())
        object.__setattr__(self, "symbol", _text("symbol", self.symbol).lower())
        bid = _number("bid", self.bid, positive=True, allow_zero=False)
        ask = _number("ask", self.ask, positive=True, allow_zero=False)
        if ask < bid:
            raise ContractError(f"{self.venue} {self.symbol}: ask {ask} < bid {bid} (crossed book)")
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)
        object.__setattr__(self, "ts", _number("ts", self.ts, positive=True, allow_zero=False))

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread_bps(self) -> float:
        return 1e4 * (self.ask - self.bid) / self.mid

    def to_dict(self) -> Dict[str, Any]:
        return {"venue": self.venue, "symbol": self.symbol, "bid": self.bid, "ask": self.ask, "ts": self.ts}

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "Quote":
        _keys(document, ("venue", "symbol", "bid", "ask", "ts"))
        return cls(**document)  # type: ignore[arg-type]


@dataclass(frozen=True)
class FundingQuote:
    """A published funding rate: what the venue announced, and over what period.

    ``rate`` is the fraction charged per settlement (``0.0001`` = 1 bp), signed:
    a positive rate is paid by longs to shorts.  The period is per contract —
    HTX settles 8 h, 4 h or 1 h depending on the swap — so a rate without its
    period is not a rate, it is a rumour.
    """

    venue: str
    symbol: str
    rate: float
    period_hours: float
    ts: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "venue", _text("venue", self.venue).lower())
        object.__setattr__(self, "symbol", _text("symbol", self.symbol).lower())
        object.__setattr__(self, "rate", _number("rate", self.rate))
        object.__setattr__(
            self, "period_hours", _number("period_hours", self.period_hours, positive=True, allow_zero=False)
        )
        object.__setattr__(self, "ts", _number("ts", self.ts, positive=True, allow_zero=False))

    @property
    def daily_rate(self) -> float:
        """The rate expressed per day, so rates with different periods compare."""
        return self.rate * 24.0 / self.period_hours

    def to_dict(self) -> Dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "rate": self.rate,
            "period_hours": self.period_hours,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "FundingQuote":
        _keys(document, ("venue", "symbol", "rate", "period_hours", "ts"))
        return cls(**document)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Snapshot:
    """Everything one decision is allowed to see, frozen at ``ts``.

    The snapshot is the look-ahead barrier in its cheapest form: a strategy is
    handed a file, not a live feed, so "what did the monster know at 12:00" has
    one answer that can be replayed a year later.  A quote stamped after the
    snapshot is refused — a reading from the future is not a reading.
    """

    ts: float
    quotes: Tuple[Quote, ...] = ()
    funding: Tuple[FundingQuote, ...] = ()
    source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", _number("ts", self.ts, positive=True, allow_zero=False))
        quotes, funding = tuple(self.quotes), tuple(self.funding)
        for item in quotes:
            if not isinstance(item, Quote):
                raise ContractError(f"quotes must be Quote, got {type(item).__name__}")
        for item in funding:
            if not isinstance(item, FundingQuote):
                raise ContractError(f"funding must be FundingQuote, got {type(item).__name__}")
        seen: set = set()
        for quote in quotes:
            key = (quote.venue, quote.symbol)
            if key in seen:
                raise ContractError(f"duplicate quote for {quote.venue} {quote.symbol}")
            seen.add(key)
        seen.clear()
        for rate in funding:
            key = (rate.venue, rate.symbol)
            if key in seen:
                raise ContractError(f"duplicate funding for {rate.venue} {rate.symbol}")
            seen.add(key)
        for item in quotes + funding:
            if item.ts > self.ts + 1e-9:
                raise ContractError(
                    f"{item.venue} {item.symbol} is stamped {item.ts} after the snapshot {self.ts}: "
                    "a reading from the future is not a reading"
                )
        object.__setattr__(self, "quotes", quotes)
        object.__setattr__(self, "funding", funding)
        object.__setattr__(self, "source", _text("source", self.source, required=False))

    def quote(self, venue: str, symbol: str) -> Optional[Quote]:
        key = (venue.strip().lower(), symbol.strip().lower())
        for quote in self.quotes:
            if (quote.venue, quote.symbol) == key:
                return quote
        return None

    def funding_quote(self, venue: str, symbol: str) -> Optional[FundingQuote]:
        key = (venue.strip().lower(), symbol.strip().lower())
        for rate in self.funding:
            if (rate.venue, rate.symbol) == key:
                return rate
        return None

    def symbols(self) -> Tuple[str, ...]:
        return tuple(sorted({quote.symbol for quote in self.quotes}))

    def venues(self) -> Tuple[str, ...]:
        return tuple(sorted({quote.venue for quote in self.quotes}))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "source": self.source,
            "quotes": [quote.to_dict() for quote in self.quotes],
            "funding": [rate.to_dict() for rate in self.funding],
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "Snapshot":
        _keys(document, ("ts", "quotes"), optional=("funding", "source"))
        quotes, funding = document.get("quotes") or [], document.get("funding") or []
        if not isinstance(quotes, Sequence) or isinstance(quotes, (str, bytes)):
            raise ContractError("quotes must be a list")
        if not isinstance(funding, Sequence) or isinstance(funding, (str, bytes)):
            raise ContractError("funding must be a list")
        return cls(
            ts=document["ts"],
            quotes=tuple(Quote.from_dict(item) for item in quotes),
            funding=tuple(FundingQuote.from_dict(item) for item in funding),
            source=document.get("source", ""),
        )


@dataclass(frozen=True)
class Leg:
    """One side of a trade, expressed in dollars so two legs can be compared."""

    venue: str
    symbol: str
    side: str
    kind: str
    notional_usd: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "venue", _text("venue", self.venue).lower())
        object.__setattr__(self, "symbol", _text("symbol", self.symbol).lower())
        side, kind = _text("side", self.side).lower(), _text("kind", self.kind).lower()
        if side not in SIDES:
            raise ContractError(f"side must be one of {SIDES}, got {side!r}")
        if kind not in KINDS:
            raise ContractError(f"kind must be one of {KINDS}, got {kind!r}")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self, "notional_usd", _number("notional_usd", self.notional_usd, positive=True, allow_zero=False)
        )

    @property
    def signed_notional(self) -> float:
        return self.notional_usd if self.side == "buy" else -self.notional_usd

    def to_dict(self) -> Dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "side": self.side,
            "kind": self.kind,
            "notional_usd": self.notional_usd,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "Leg":
        _keys(document, ("venue", "symbol", "side", "kind", "notional_usd"))
        return cls(**document)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Intent:
    """A strategy's proposal, priced and explained, before any risk check.

    An intent is not an order.  It is the sentence "I would put these legs on,
    because this edge, for this long" — with the id derived from that sentence,
    so the journal can point at it and the autopsy can find it later without
    trusting either process to name it the same way twice.
    """

    strategy: str
    legs: Tuple[Leg, ...]
    expected_edge_bps: float
    horizon_hours: float
    rationale: str
    id: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy", _text("strategy", self.strategy))
        legs = tuple(self.legs)
        if not legs:
            raise ContractError("an intent without legs is not a proposal")
        for leg in legs:
            if not isinstance(leg, Leg):
                raise ContractError(f"legs must be Leg, got {type(leg).__name__}")
        object.__setattr__(self, "legs", legs)
        object.__setattr__(self, "expected_edge_bps", _number("expected_edge_bps", self.expected_edge_bps))
        object.__setattr__(
            self, "horizon_hours", _number("horizon_hours", self.horizon_hours, positive=True, allow_zero=False)
        )
        object.__setattr__(self, "rationale", _text("rationale", self.rationale))
        if self.id:
            expected = self._digest()
            if self.id != expected:
                raise ContractError(f"id {self.id!r} does not match the intent it names ({expected!r})")
        else:
            object.__setattr__(self, "id", self._digest())

    def _payload(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "legs": [leg.to_dict() for leg in self.legs],
            "expected_edge_bps": self.expected_edge_bps,
            "horizon_hours": self.horizon_hours,
            "rationale": self.rationale,
        }

    def _digest(self) -> str:
        return hashlib.sha256(canonical(self._payload()).encode("utf-8")).hexdigest()[:16]

    @property
    def gross_usd(self) -> float:
        return sum(leg.notional_usd for leg in self.legs)

    @property
    def net_delta_usd(self) -> float:
        return sum(leg.signed_notional for leg in self.legs)

    @property
    def symbols(self) -> Tuple[str, ...]:
        return tuple(sorted({leg.symbol for leg in self.legs}))

    def to_dict(self) -> Dict[str, Any]:
        return {**self._payload(), "id": self.id}

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "Intent":
        _keys(document, ("strategy", "legs", "expected_edge_bps", "horizon_hours", "rationale", "id"))
        legs = document["legs"]
        if not isinstance(legs, Sequence) or isinstance(legs, (str, bytes)):
            raise ContractError("legs must be a list")
        return cls(
            strategy=document["strategy"],
            legs=tuple(Leg.from_dict(item) for item in legs),
            expected_edge_bps=document["expected_edge_bps"],
            horizon_hours=document["horizon_hours"],
            rationale=document["rationale"],
            id=document["id"],
        )


@dataclass(frozen=True)
class Fill:
    """One executed leg: quantity, price, fee — everything an autopsy needs."""

    ts: float
    strategy: str
    venue: str
    symbol: str
    kind: str
    side: str
    qty: float
    price: float
    fee_usd: float
    notional_usd: float
    leg_index: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", _number("ts", self.ts, positive=True, allow_zero=False))
        object.__setattr__(self, "strategy", _text("strategy", self.strategy))
        object.__setattr__(self, "venue", _text("venue", self.venue).lower())
        object.__setattr__(self, "symbol", _text("symbol", self.symbol).lower())
        kind, side = _text("kind", self.kind).lower(), _text("side", self.side).lower()
        if kind not in KINDS:
            raise ContractError(f"kind must be one of {KINDS}, got {kind!r}")
        if side not in SIDES:
            raise ContractError(f"side must be one of {SIDES}, got {side!r}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "qty", _number("qty", self.qty, positive=True, allow_zero=False))
        object.__setattr__(self, "price", _number("price", self.price, positive=True, allow_zero=False))
        object.__setattr__(self, "fee_usd", _number("fee_usd", self.fee_usd, positive=True))
        object.__setattr__(
            self, "notional_usd", _number("notional_usd", self.notional_usd, positive=True, allow_zero=False)
        )
        if isinstance(self.leg_index, bool) or not isinstance(self.leg_index, int) or self.leg_index < 0:
            raise ContractError("leg_index must be a non-negative integer")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "strategy": self.strategy,
            "venue": self.venue,
            "symbol": self.symbol,
            "kind": self.kind,
            "side": self.side,
            "qty": self.qty,
            "price": self.price,
            "fee_usd": self.fee_usd,
            "notional_usd": self.notional_usd,
            "leg_index": self.leg_index,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "Fill":
        _keys(document, tuple(Fill.__dataclass_fields__))
        return cls(**document)  # type: ignore[arg-type]


@dataclass(frozen=True)
class ClosedTrade:
    """A position that ended, with the three things that decided its return.

    ``realized_usd`` is after fees and after funding: a carry trade that
    collected the rate but paid the spread twice has not "made 3 bps", and the
    autopsy is only useful if it says so.
    """

    strategy: str
    venue: str
    symbol: str
    kind: str
    entry_side: str
    qty: float
    entry_price: float
    exit_price: float
    entry_notional_usd: float
    fees_usd: float
    funding_usd: float
    realized_usd: float
    bps: float
    opened_ts: float
    closed_ts: float
    reason: str

    def __post_init__(self) -> None:
        for name in ("strategy", "venue", "symbol", "kind", "entry_side", "reason"):
            value = _text(name, getattr(self, name))
            lowered = value.lower() if name in ("venue", "symbol", "kind", "entry_side") else value
            object.__setattr__(self, name, lowered)
        if self.kind not in KINDS:
            raise ContractError(f"kind must be one of {KINDS}, got {self.kind!r}")
        if self.entry_side not in SIDES:
            raise ContractError(f"entry_side must be one of {SIDES}, got {self.entry_side!r}")
        for name in ("qty", "entry_price", "exit_price", "entry_notional_usd"):
            object.__setattr__(self, name, _number(name, getattr(self, name), positive=True, allow_zero=False))
        for name in ("fees_usd", "funding_usd", "realized_usd", "bps"):
            object.__setattr__(self, name, _number(name, getattr(self, name)))
        for name in ("opened_ts", "closed_ts"):
            object.__setattr__(self, name, _number(name, getattr(self, name), positive=True, allow_zero=False))
        if self.closed_ts < self.opened_ts:
            raise ContractError("closed_ts precedes opened_ts")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "venue": self.venue,
            "symbol": self.symbol,
            "kind": self.kind,
            "entry_side": self.entry_side,
            "qty": self.qty,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "entry_notional_usd": self.entry_notional_usd,
            "fees_usd": self.fees_usd,
            "funding_usd": self.funding_usd,
            "realized_usd": self.realized_usd,
            "bps": self.bps,
            "opened_ts": self.opened_ts,
            "closed_ts": self.closed_ts,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "ClosedTrade":
        _keys(document, tuple(ClosedTrade.__dataclass_fields__))
        return cls(**document)  # type: ignore[arg-type]
