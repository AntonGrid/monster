"""Instrument identity: one canonical name per instrument, and each venue's own.

Why a module for names
======================

Three venues quote the same coin under three spellings — ``btcusdt`` on HTX
spot, ``BTC-USDT`` on HTX's swap and on OKX, ``BTCUSDT`` on Bybit — and the
truth check compares venues *by symbol*.  If two spellings of BTC reach it as
two symbols, the cross-section has one venue each and the detector reports
``unmeasured`` for both, forever, while looking perfectly healthy.  If a
spelling is guessed wrong (``BTC-USDT`` asked of a venue that lists
``BTC-USDT-SWAP``), the venue politely returns a different instrument and the
monster trades ETH believing it holds BTC.

So the mapping is written down once, here, explicitly, with no string
tinkering: an asset has a canonical spot and perp name (the ones the strategies
and the journal use), and every venue has a literal native id for each.  A
missing entry is an :class:`InstrumentError` naming what was asked for, never a
fallback built by uppercasing something.

Canonical names are the HTX spot/perp spellings, because ``strategies/carry.py``
already declared its pairs in them and a second convention would be a second
truth.  ``DEFAULT_PAIRS`` there now re-exports :data:`PAIRS`, so there is only
one table to edit.

Funding periods
===============

``FUNDING_PERIOD_HOURS`` is the venue's *default* settlement interval — 8 h on
all three venues today, but per-contract in reality (HTX lists 1 h and 4 h
swaps).  It is a starting point, not a fact: the parser overrides it whenever
the payload carries the interval (``settle_period`` on HTX, the gap between
``fundingTime`` and ``nextFundingTime`` on OKX), and the journal records the
period that was actually used.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

__all__ = [
    "InstrumentError",
    "PAIRS",
    "NATIVE",
    "FUNDING_PERIOD_HOURS",
    "canonical_pair",
    "kind_of",
    "asset_for",
    "kinds_for",
    "native_symbol",
    "assets",
    "venues",
]


class InstrumentError(ValueError):
    """An instrument was asked for under a name the tables do not hold."""


#: ``asset -> (canonical spot symbol, canonical perp symbol)``.  Lower case,
#: because ``contracts`` lower-cases every symbol and two cases of one coin
#: must not become two coins.
PAIRS: Dict[str, Tuple[str, str]] = {
    "btc": ("btcusdt", "btc-usdt-swap"),
    "eth": ("ethusdt", "eth-usdt-swap"),
}

#: ``venue -> asset -> (spot native id, perp native id)``.  Literal on purpose:
#: a rule that can be derived can also be derived wrongly.
NATIVE: Dict[str, Dict[str, Tuple[str, str]]] = {
    "htx": {"btc": ("btcusdt", "BTC-USDT"), "eth": ("ethusdt", "ETH-USDT")},
    "okx": {"btc": ("BTC-USDT", "BTC-USDT-SWAP"), "eth": ("ETH-USDT", "ETH-USDT-SWAP")},
    "bybit": {"btc": ("BTCUSDT", "BTCUSDT"), "eth": ("ETHUSDT", "ETHUSDT")},
}

#: Default settlement interval per venue, in hours.  Overridden per payload
#: when the venue states it; see the module docstring.
FUNDING_PERIOD_HOURS: Dict[str, float] = {"htx": 8.0, "okx": 8.0, "bybit": 8.0}

_KINDS = ("spot", "perp")


def canonical_pair(asset: str) -> Tuple[str, str]:
    """Return ``(spot, perp)`` canonical symbols for an asset."""
    key = asset.strip().lower()
    if key not in PAIRS:
        raise InstrumentError(f"unknown asset {asset!r}; known: {', '.join(sorted(PAIRS))}")
    return PAIRS[key]


def kind_of(symbol: str) -> Optional[str]:
    """Return ``"spot"``/``"perp"`` for a canonical symbol, or ``None``."""
    key = symbol.strip().lower()
    for spot, perp in PAIRS.values():
        if key == spot:
            return "spot"
        if key == perp:
            return "perp"
    return None


def native_symbol(venue: str, asset: str, kind: str) -> str:
    """Return the venue's own id for an asset, refusing to guess one."""
    venue_key, asset_key = venue.strip().lower(), asset.strip().lower()
    table = NATIVE.get(venue_key)
    if table is None:
        raise InstrumentError(f"unknown venue {venue!r}; known: {', '.join(sorted(NATIVE))}")
    pair = table.get(asset_key)
    if pair is None:
        raise InstrumentError(f"{venue_key} does not quote {asset_key!r}; its assets: {', '.join(sorted(table))}")
    if kind not in _KINDS:
        raise InstrumentError(f"kind must be one of {_KINDS}, got {kind!r}")
    return pair[0] if kind == "spot" else pair[1]


def asset_for(venue: str, native: str) -> Optional[Tuple[str, str]]:
    """Reverse lookup: ``(asset, kind)`` for a venue's own id, or ``None``.

    Returns the first kind whose native id matches.  On Bybit spot and linear
    share one spelling (``BTCUSDT``), so callers that care about the kind must
    use :func:`kinds_for` instead of trusting this answer.
    """
    table = NATIVE.get(venue.strip().lower())
    if table is None:
        return None
    key = native.strip()
    for asset, (spot, perp) in table.items():
        if key == spot:
            return asset, "spot"
        if key == perp:
            return asset, "perp"
    return None


def kinds_for(venue: str, native: str) -> Tuple[str, ...]:
    """Every kind a venue's native id can denote (Bybit reuses one spelling)."""
    table = NATIVE.get(venue.strip().lower())
    if table is None:
        return ()
    key = native.strip()
    found: Tuple[str, ...] = tuple(
        kind
        for kind, index in (("spot", 0), ("perp", 1))
        if any(key == pair[index] for pair in table.values())
    )
    return found


def assets() -> Tuple[str, ...]:
    return tuple(sorted(PAIRS))


def venues() -> Tuple[str, ...]:
    return tuple(sorted(NATIVE))
