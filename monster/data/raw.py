"""The raw payload: what the venue said, before anyone decided what it meant.

Why keep the body and not only the quote
========================================

A normalized record is an *interpretation*: "this string, in this field, is the
bid".  Interpretations have bugs, and a data layer that keeps only its
interpretation cannot fix an old bug without re-downloading history — which is
impossible for the past, and dishonest for a backtest that claims to replay it.

So every capture writes the body as it arrived, with the request it answered,
and the normalized rows are derived from it.  ``quality.audit_day`` re-parses a
day of raw bodies and compares the result with what the store says the day
contains; when the two disagree, the parser or the store is wrong and the
report names which.

What "missing" means here
=========================

``decimal`` refuses ``None``, ``""``, ``"  "``, ``"null"``, ``NaN`` and
``Infinity`` with a sentence.  It does *not* refuse ``"0E-18"``: that is a venue
stating zero, and a zero the venue stated is data.  The bug this rule exists to
prevent is the opposite one — ``float(value or 0)`` turns silence into a zero,
and a funding rate of "unknown" into "no carry" for every contract that failed
to publish.  Silence is refused; zero is read.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

__all__ = ["ShapeError", "Refused", "RawPayload", "pick", "decimal", "timestamp_s", "KINDS"]

#: What an endpoint is asked for.  ``funding`` is a reading about a perp, and
#: is kept distinct so a spot ticker can never be filed as a rate.
KINDS = ("spot", "perp", "funding")


class ShapeError(ValueError):
    """A payload that does not mean what the venue's schema says it means.

    Raised instead of returning a partially filled object: a quote with a zero
    bid is not a cheap quote, it is a reading that failed, and the difference
    has to reach the operator before it reaches a strategy.
    """


class Refused(ShapeError):
    """The venue answered, and the answer was a refusal rather than data.

    A refusal is not a schema change, and the difference decides what a
    collector does next.  ``rate_limit`` is worth asking about again, later,
    with a real delay; ``refused`` (a delisted instrument, a rejected
    parameter) is not, because a second identical question gets a second
    identical answer.  Retrying a schema change is how a feed stops being
    watched: the retries hide the breakage behind noise.

    It subclasses :class:`ShapeError` so every reader that already knows "this
    body produced no reading" — the audit, the doctor, the capture loop —
    keeps working unchanged, and only the code that decides *whether to ask
    again* has to look at the subclass.
    """

    #: Kinds a later attempt can plausibly cure.  Everything else is a fact.
    RETRYABLE = ("rate_limit",)

    def __init__(self, message: str, *, kind: str = "refused", status: Optional[int] = None) -> None:
        super().__init__(message)
        self.kind = str(kind)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.kind in self.RETRYABLE


@dataclass(frozen=True)
class RawPayload:
    """One request's answer, stamped with the moment it was received.

    ``received_ts`` is our clock, not the venue's: it says when the monster
    learned this, which is what staleness and "as of" queries must use.  The
    body's own timestamp stays inside the body, where it is evidence about the
    venue's clock rather than a substitute for ours.

    ``symbol``/``native``/``kind``/``period_hours`` describe the request the
    body answers.  They are optional so a hand-saved recording still loads, but
    a payload without them cannot be re-parsed by :func:`monster.data.quality.audit_day`,
    and the audit says so instead of guessing.
    """

    venue: str
    endpoint: str
    received_ts: float
    body: Union[Mapping[str, Any], List[Any]]
    symbol: str = ""
    native: str = ""
    kind: str = ""
    period_hours: float = 0.0

    def __post_init__(self) -> None:
        for name in ("venue", "endpoint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ShapeError(f"{name} must be a non-empty string, got {value!r}")
            object.__setattr__(self, name, value.strip().lower())
        if isinstance(self.body, (str, bytes)) or not isinstance(self.body, (Mapping, Sequence)):
            raise ShapeError(f"body must be a JSON object or array, got {type(self.body).__name__}")
        object.__setattr__(self, "received_ts", timestamp_s("received_ts", self.received_ts))
        symbol = str(self.symbol or "").strip().lower()
        native = str(self.native or "").strip()
        kind = str(self.kind or "").strip().lower()
        if kind and kind not in KINDS:
            raise ShapeError(f"kind must be one of {KINDS} or empty, got {kind!r}")
        period = float(self.period_hours or 0.0)
        if period < 0.0 or not math.isfinite(period):
            raise ShapeError(f"period_hours must be finite and non-negative, got {period!r}")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "native", native)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "period_hours", period)

    @property
    def parsable(self) -> bool:
        """Whether enough context survived to re-parse this body later."""
        return bool(self.symbol and self.native and self.kind)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "venue": self.venue,
            "endpoint": self.endpoint,
            "received_ts": self.received_ts,
            "symbol": self.symbol,
            "native": self.native,
            "kind": self.kind,
            "period_hours": self.period_hours,
            "body": self.body,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "RawPayload":
        return cls(**{name: document[name] for name in cls.__dataclass_fields__ if name in document})


def pick(document: Any, path: str) -> Any:
    """Read a dotted path, naming the step that was missing.

    ``tick.bid.0`` walks mappings and lists alike, so a venue that wraps its
    book in an array needs no special case in every parser.
    """
    node = document
    walked: List[str] = []
    for part in path.split("."):
        walked.append(part)
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)) and part.isdigit():
            index = int(part)
            if index >= len(node):
                raise ShapeError(f"payload has no {'.'.join(walked)}: index {index} of {len(node)}")
            node = node[index]
        else:
            raise ShapeError(f"payload has no {'.'.join(walked)} (path {path!r})")
    return node


def decimal(name: str, value: Any, *, allow_zero: bool = True) -> float:
    """A venue's number, or a refusal.

    Strings are accepted because every venue in this workspace serialises
    decimals as strings; blanks and words are refused because a blank is not a
    price.  Exponent form is accepted: ``"0E-18"`` is zero, stated.
    """
    number: float
    if value is None or isinstance(value, bool):
        raise ShapeError(f"{name} is missing ({value!r})")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ShapeError(f"{name} is an empty string: silence is not a zero")
        try:
            number = float(text)
        except ValueError as error:
            raise ShapeError(f"{name} is not a number: {text!r}") from error
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        raise ShapeError(f"{name} must be a number or a numeric string, got {type(value).__name__}")
    if not math.isfinite(number):
        raise ShapeError(f"{name} is not finite: {value!r}")
    if not allow_zero and number == 0.0:
        raise ShapeError(f"{name} must be non-zero")
    return number


def timestamp_s(name: str, value: Any) -> float:
    """A positive epoch in seconds, from seconds or milliseconds.

    Venues disagree about units and both spellings are in the wild, so the unit
    is inferred once here (anything past ``1e11`` is milliseconds: year 5138,
    nobody quotes there) rather than in each parser.
    """
    number = decimal(name, value)
    if number <= 0.0:
        raise ShapeError(f"{name} must be positive, got {number!r}")
    if number > 1e11:
        number /= 1000.0
    return number

