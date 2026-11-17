"""Configuration: one place where an operator's numbers enter, validated.

Everything here has a default that is safe to run on a laptop with no keys and
no network.  Two rules are deliberate:

* ``MONSTER_MODE`` accepts ``paper`` and nothing else.  Live trading is not a
  configuration flag in this package — the hands are Rust's, and a Python
  process that can be told ``MODE=live`` by an environment variable is a
  process a prompt injection can tell too;
* every number is validated by the component that uses it (``Limits``,
  ``PaperVenue``, ``scan_carry``), so this module only parses and forwards.  A
  second set of opinions about what "positive" means would drift from the
  first.

Paths default under ``var/`` inside the working directory: a journal and a state
file per checkout, both easy to delete and both useless to keep secret.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from monster.risk.limits import Limits

__all__ = ["Config", "PAPER_MODE"]


PAPER_MODE = "paper"


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        return float(raw)
    except ValueError as error:
        raise ValueError(f"{name} is not a number: {raw!r}") from error


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    value = _float(env, name, default)
    if value != int(value):
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    return int(value)


def _path(env: Mapping[str, str], name: str, default: Path) -> Path:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    return Path(str(raw)).expanduser()


@dataclass(frozen=True)
class Config:
    """The paper book's numbers, as the operator set them."""

    mode: str = PAPER_MODE
    capital_usd: float = 1_000.0
    notional_usd: float = 100.0
    fee_bps: float = 5.0
    slippage_bps: float = 2.0
    leverage: float = 2.0
    horizon_hours: float = 168.0
    min_edge_bps: float = 10.0
    close_after_hours: float = 168.0
    coherence_window: int = 128
    surrogates: int = 200
    seed: int = 0
    venue: str = "htx"
    #: Venues allowed to check the trading venue's prices.  HTX trades, OKX and
    #: Bybit vote: the median of three is what makes one liar visible.
    truth_venues: Tuple[str, ...] = ("htx", "okx", "bybit")
    data_path: Path = field(default_factory=lambda: Path("var/data"))
    interval_s: int = 60
    quote_max_age_s: float = 5.0
    funding_max_age_h: float = 12.0
    journal_path: Path = field(default_factory=lambda: Path("var/journal.jsonl"))
    state_path: Path = field(default_factory=lambda: Path("var/state.json"))
    limits: Limits = field(default_factory=Limits)

    def __post_init__(self) -> None:
        if self.mode != PAPER_MODE:
            raise ValueError(
                f"mode {self.mode!r} is not available: this package replaces no venue, "
                f"and only {PAPER_MODE!r} is implemented"
            )
        venues = tuple(dict.fromkeys(item.strip().lower() for item in self.truth_venues if str(item).strip()))
        if not venues:
            raise ValueError("truth_venues must name at least one venue")
        object.__setattr__(self, "truth_venues", venues)
        if self.venue not in venues:
            raise ValueError(
                f"venue {self.venue!r} is not in truth_venues {venues}: "
                "a venue you trade must be a venue you check"
            )
        if self.capital_usd <= 0.0:
            raise ValueError("capital_usd must be positive")
        if self.notional_usd <= 0.0:
            raise ValueError("notional_usd must be positive")
        if self.horizon_hours <= 0.0 or self.close_after_hours <= 0.0:
            raise ValueError("horizon_hours and close_after_hours must be positive")
        if self.coherence_window < 32:
            raise ValueError("coherence_window below 32 cannot hold a cycle")
        if self.surrogates < 20:
            raise ValueError("fewer than 20 surrogates cannot resolve a p-value")
        if int(self.interval_s) <= 0:
            raise ValueError("interval_s must be a positive number of seconds")
        if self.quote_max_age_s <= 0.0:
            raise ValueError("quote_max_age_s must be positive")
        if self.funding_max_age_h <= 0.0:
            raise ValueError("funding_max_age_h must be positive")

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Config":
        source = dict(os.environ if env is None else env)
        limits = Limits(
            max_gross_usd=_float(source, "MONSTER_MAX_GROSS_USD", 2_000.0),
            max_net_delta_usd=_float(source, "MONSTER_MAX_NET_DELTA_USD", 200.0),
            max_symbol_usd=_float(source, "MONSTER_MAX_SYMBOL_USD", 1_000.0),
            max_venue_usd=_float(source, "MONSTER_MAX_VENUE_USD", 2_000.0),
            max_leverage=_float(source, "MONSTER_MAX_LEVERAGE", 2.0),
            max_daily_loss_usd=_float(source, "MONSTER_MAX_DAILY_LOSS_USD", 20.0),
            min_free_cash_usd=_float(source, "MONSTER_MIN_FREE_CASH_USD", 50.0),
        )
        return cls(
            mode=str(source.get("MONSTER_MODE", PAPER_MODE)).strip().lower(),
            capital_usd=_float(source, "MONSTER_CAPITAL_USD", 1_000.0),
            notional_usd=_float(source, "MONSTER_NOTIONAL_USD", 100.0),
            fee_bps=_float(source, "MONSTER_FEE_BPS", 5.0),
            slippage_bps=_float(source, "MONSTER_SLIPPAGE_BPS", 2.0),
            leverage=_float(source, "MONSTER_LEVERAGE", 2.0),
            horizon_hours=_float(source, "MONSTER_HORIZON_HOURS", 168.0),
            min_edge_bps=_float(source, "MONSTER_MIN_EDGE_BPS", 10.0),
            close_after_hours=_float(source, "MONSTER_CLOSE_AFTER_HOURS", 168.0),
            coherence_window=_int(source, "MONSTER_COHERENCE_WINDOW", 128),
            surrogates=_int(source, "MONSTER_SURROGATES", 200),
            seed=_int(source, "MONSTER_SEED", 0),
            venue=str(source.get("MONSTER_VENUE", "htx")).strip().lower(),
            truth_venues=tuple(
                part.strip().lower()
                for part in str(source.get("MONSTER_TRUTH_VENUES", "htx,okx,bybit")).split(",")
                if part.strip()
            ),
            data_path=_path(source, "MONSTER_DATA_PATH", Path("var/data")),
            interval_s=_int(source, "MONSTER_INTERVAL_S", 60),
            quote_max_age_s=_float(source, "MONSTER_MAX_QUOTE_AGE_S", 5.0),
            funding_max_age_h=_float(source, "MONSTER_MAX_FUNDING_AGE_H", 12.0),
            journal_path=_path(source, "MONSTER_JOURNAL", Path("var/journal.jsonl")),
            state_path=_path(source, "MONSTER_STATE", Path("var/state.json")),
            limits=limits,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "capital_usd": self.capital_usd,
            "notional_usd": self.notional_usd,
            "fee_bps": self.fee_bps,
            "slippage_bps": self.slippage_bps,
            "leverage": self.leverage,
            "horizon_hours": self.horizon_hours,
            "min_edge_bps": self.min_edge_bps,
            "close_after_hours": self.close_after_hours,
            "coherence_window": self.coherence_window,
            "surrogates": self.surrogates,
            "seed": self.seed,
            "venue": self.venue,
            "truth_venues": list(self.truth_venues),
            "data_path": str(self.data_path),
            "interval_s": self.interval_s,
            "quote_max_age_s": self.quote_max_age_s,
            "funding_max_age_h": self.funding_max_age_h,
            "journal_path": str(self.journal_path),
            "state_path": str(self.state_path),
            "limits": self.limits.to_dict(),
        }
