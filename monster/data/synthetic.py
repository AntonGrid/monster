"""A synthetic tape: the pipeline's proof, never a strategy's evidence.

Why this file exists
====================

The data layer must be runnable before the VPS has recorded a week, and a
coherence reading needs more bars than a single hand-written snapshot holds.
So this module writes tapes — files in exactly the format :class:`FileTransport`
replays — whose bodies have the *shape* of the real venues and prices from a
seeded, documented process.

What it is not
==============

It is not market data.  Every price here comes from a sine and a normal draw;
the coherence the demo reports is the coherence of that sine.  The tape exists
to prove that capture → store → derive → cycle runs end to end and that gaps,
deduplication and alignment behave, and the run that uses it says
``source=synthetic`` so no one mistakes the picture for the market.  A backtest
on this tape would be a backtest of this file.

The process (deliberately boring)
=================================

Every asset follows a common cycle plus its own phase and noise, accumulated as
returns so prices stay positive; venues differ by a constant few bps and a
small per-sample jitter; the perp sits a basis above spot.  Funding is constant
per asset — 5 bps/8 h on BTC (enough to clear the carry threshold after costs)
and 1 bp/8 h on ETH (21 bps over a week, below the ~29 bps of four fills and the
basis) — so the scan's arithmetic is reproducible by hand.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from monster.data.capture import endpoints

__all__ = ["Tape", "write_demo_tape", "DEFAULT_BASE_TS"]

#: 2026-11-17T00:00:00Z — the day the Phase 1 fixtures live on.
DEFAULT_BASE_TS = 1_794_873_600.0

_LEVELS: Mapping[str, float] = {"btc": 35_000.0, "eth": 2_000.0}
_FUNDING: Mapping[str, float] = {"btc": 0.0005, "eth": 0.0001}
#: ``venue -> (mid offset in bps, half-spread in bps)``.  A venue that is 1 bp
#: away from the median with a 0.5 bp spread is a normal venue, not a liar.
_VENUES: Mapping[str, Tuple[float, float]] = {"htx": (0.0, 1.0), "okx": (1.2, 0.8), "bybit": (-0.9, 0.7)}


@dataclass(frozen=True)
class Tape:
    """Where a tape was written and what clock it covers."""

    root: Path
    base_ts: float
    samples: int
    interval_s: float

    @property
    def last_ts(self) -> float:
        return self.base_ts + (self.samples - 1) * self.interval_s

    def describe(self) -> str:
        return (
            f"tape {self.root}: {self.samples} samples every {self.interval_s:g}s, "
            f"{self.base_ts:.0f}..{self.last_ts:.0f} (synthetic, not market data)"
        )


def _series(asset: str, samples: int, rng: random.Random) -> List[float]:
    level = _LEVELS[asset]
    phase = 0.0 if asset == "btc" else math.pi / 3.0
    prices = [level]
    for index in range(1, samples):
        cycle = 0.0012 * math.sin(2.0 * math.pi * index / 24.0 + phase)
        wobble = 0.0004 * math.sin(2.0 * math.pi * index / 7.0 + 2.0 * phase)
        noise = 0.0002 * rng.gauss(0.0, 1.0)
        prices.append(prices[-1] * math.exp(cycle + wobble + noise))
    return prices


def _quote_body(venue: str, asset: str, kind: str, mid: float, ts_ms: int) -> Mapping[str, object]:
    offset_bps, half_spread_bps = _VENUES[venue]
    centre = mid * (1.0 + (offset_bps + (1.0 if kind == "perp" else 0.0)) * 1e-4)
    half = centre * half_spread_bps * 1e-4
    bid, ask = centre - half, centre + half
    if venue == "htx":
        return {
            "status": "ok",
            "ch": f"market.{asset}usdt.detail.merged" if kind == "spot" else f"market.{asset.upper()}-USDT.detail.merged",
            "ts": ts_ms,
            "tick": {"bid": [round(bid, 4), 1.0], "ask": [round(ask, 4), 1.0], "close": round(centre, 4)},
        }
    if venue == "okx":
        inst = f"{asset.upper()}-USDT" if kind == "spot" else f"{asset.upper()}-USDT-SWAP"
        return {
            "code": "0",
            "msg": "",
            "data": [
                {
                    "instId": inst,
                    "bidPx": f"{bid:.4f}",
                    "askPx": f"{ask:.4f}",
                    "last": f"{centre:.4f}",
                    "ts": str(ts_ms),
                }
            ],
        }
    symbol = f"{asset.upper()}USDT"
    row = {
        "symbol": symbol,
        "bid1Price": f"{bid:.4f}",
        "bid1Size": "1.0",
        "ask1Price": f"{ask:.4f}",
        "ask1Size": "1.0",
        "lastPrice": f"{centre:.4f}",
        "fundingRate": f"{_FUNDING[asset]:.6f}",
        "nextFundingTime": str(ts_ms + 8 * 3_600_000),
    }
    return {"retCode": 0, "retMsg": "OK", "time": ts_ms, "result": {"category": "spot" if kind == "spot" else "linear", "list": [row]}}


def _funding_body(venue: str, asset: str, ts_ms: int) -> Mapping[str, object]:
    rate = _FUNDING[asset]
    if venue == "htx":
        return {
            "status": "ok",
            "data": {
                "contract_code": f"{asset.upper()}-USDT",
                "funding_rate": f"{rate:.6f}",
                "funding_time": ts_ms + 8 * 3_600_000,
                "settle_period": 8,
            },
        }
    if venue == "okx":
        return {
            "code": "0",
            "msg": "",
            "data": [
                {
                    "instId": f"{asset.upper()}-USDT-SWAP",
                    "fundingRate": f"{rate:.6f}",
                    "fundingTime": str(ts_ms),
                    "nextFundingTime": str(ts_ms + 8 * 3_600_000),
                }
            ],
        }
    return _quote_body(venue, asset, "perp", _LEVELS[asset], ts_ms)


def write_demo_tape(
    root: str | Path,
    *,
    assets: Sequence[str] = ("btc", "eth"),
    samples: int = 130,
    interval_s: float = 300.0,
    base_ts: float = DEFAULT_BASE_TS,
    seed: int = 7,
) -> Tape:
    """Write one tape file per endpoint under ``root/<venue>/<name>.json``."""
    if samples < 2:
        raise ValueError("a tape needs at least two samples to have an interval")
    if interval_s <= 0.0:
        raise ValueError("interval_s must be positive")
    rng = random.Random(seed)
    series = {asset: _series(asset, samples, rng) for asset in assets}
    tapes: Dict[Tuple[str, str], List[Any]] = {}
    for index in range(samples):
        stamp = float(base_ts) + index * float(interval_s)
        ts_ms = int(round(stamp * 1000.0))
        for asset in assets:
            mid = series[asset][index]
            for endpoint in endpoints(assets=(asset,)):
                if endpoint.kind == "funding":
                    # Bybit's funding rate rides inside the perp ticker, so its
                    # two files are one body; the capturer fetches it once.
                    kind = "perp" if endpoint.venue == "bybit" else endpoint.kind
                    body = (
                        _quote_body(endpoint.venue, asset, kind, mid, ts_ms)
                        if kind == "perp"
                        else _funding_body(endpoint.venue, asset, ts_ms)
                    )
                else:
                    body = _quote_body(endpoint.venue, asset, endpoint.kind, mid, ts_ms)
                tapes.setdefault((endpoint.venue, endpoint.name), []).append(body)

    target = Path(root).expanduser()
    for (venue, name), tape in tapes.items():
        directory = target / venue
        directory.mkdir(parents=True, exist_ok=True)
        document = {"synthetic": True, "interval_s": float(interval_s), "tape": tape}
        (directory / f"{name}.json").write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    return Tape(root=target, base_ts=float(base_ts), samples=int(samples), interval_s=float(interval_s))

