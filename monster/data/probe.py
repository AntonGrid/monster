"""Ask every endpoint once: is the venue answering, and does its shape still parse?

A ``curl`` that returns 200 proves the network.  It does not prove that the
venue serves *our* ``User-Agent``, that the unauthenticated URL still points at
a ticker, or that the body is the shape the parser expects.  ``probe`` asks
exactly the questions ``capture`` will ask, through the same ``Transport`` and
the same parsers, and prints one line per endpoint: the reading, the latency,
or the parser's sentence.

``probe`` writes nothing.  It is a question, not evidence — the evidence is the
body ``capture`` keeps — so running it on the VPS before a week of collection
costs nothing and can only prevent a week of collecting the wrong thing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Tuple

from monster.data.capture import Endpoint, Transport, TransportError, endpoints
from monster.data.parsers import funding_from, quote_from
from monster.data.raw import ShapeError

__all__ = ["Probe", "probe"]


@dataclass(frozen=True)
class Probe:
    """One endpoint's answer: a reading with its latency, or a sentence."""

    endpoint: Endpoint
    latency_ms: float
    reading: str
    error: str

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict:
        return {
            "venue": self.endpoint.venue,
            "name": self.endpoint.name,
            "kind": self.endpoint.kind,
            "asset": self.endpoint.asset,
            "native": self.endpoint.native,
            "latency_ms": round(self.latency_ms, 1),
            "reading": self.reading,
            "error": self.error,
            "ok": self.ok,
        }

    def describe(self) -> str:
        venue, name = self.endpoint.venue, self.endpoint.name
        if self.ok:
            return f"  [ok]   {venue:<5} {name:<12} {self.reading:<24} {self.latency_ms:6.0f} ms"
        return f"  [fail] {venue:<5} {name:<12} {self.error}"


def _read(endpoint: Endpoint, body, received_ts: float) -> str:
    """The one-line summary of a parsed body, printed or raised."""
    if endpoint.kind == "funding":
        rate = funding_from(
            venue=endpoint.venue,
            payload=body,
            symbol=endpoint.symbol,
            native=endpoint.native,
            period_hours=endpoint.period_hours,
            received_ts=received_ts,
        )
        return f"{rate.rate * 1e4:+.3f} bps/{rate.period_hours:g}h"
    quote = quote_from(
        venue=endpoint.venue,
        payload=body,
        symbol=endpoint.symbol,
        native=endpoint.native,
        kind=endpoint.kind,
    )
    return f"{quote.bid:.6g} / {quote.ask:.6g}"


def probe(
    transport: Transport,
    wanted: Optional[Sequence[Endpoint]] = None,
    *,
    assets: Sequence[str] = ("btc", "eth"),
    venues: Sequence[str] = ("htx", "okx", "bybit"),
    clock: Callable[[], float] = time.time,
) -> Tuple[Probe, ...]:
    """Ask each endpoint once, with no retries.

    No retries is deliberate: a probe that retries hides the flakiness it was
    run to find.  A transient failure shows up as a failure, and the operator
    runs the probe again.
    """
    selected = tuple(wanted) if wanted is not None else endpoints(assets=assets, venues=venues)
    results = []
    for endpoint in selected:
        started = clock()
        reading = error = ""
        try:
            body = transport.get(endpoint)
        except TransportError as problem:
            error = str(problem)
        else:
            received = clock()
            try:
                reading = _read(endpoint, body, received)
            except ShapeError as problem:
                error = str(problem)
        latency = 1000.0 * (clock() - started)
        results.append(Probe(endpoint=endpoint, latency_ms=latency, reading=reading, error=error))
    return tuple(results)
