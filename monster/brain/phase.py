"""Coherence of a cross-section, reported beside the null that could fake it.

Why this file exists, and why it is not a signal
================================================

``entrain`` says a medium can be read as oscillators: each series has a phase,
and ``R = |mean(e^{iθ})|`` says how much of the park sounds together.  Ported to
markets, that is a real question — are these symbols moving as a crowd, or is
each one doing its own thing — and it is the question a regime filter needs.

It is also the easiest number in this repository to lie with.  ``R`` is
positive-biased: with four series and a short window, ``R ≈ 0.5`` happens when
nothing is aligned at all, and a threshold picked by eye ("above 0.7 is a
crowd") is a threshold picked on noise.  Worse, smoothing or a shared trend can
push ``R`` toward 1 while the only thing the symbols share is that they were
detrended by the same procedure.

So the reading is never ``R`` alone.  Every call computes the **same statistic
on the same data with the alignment destroyed**: each symbol's detrended window
is circularly shifted by its own random offset, which keeps that symbol's own
spectrum and volatility and removes only the cross-symbol phase relationship.
The surrogate distribution answers the only question that matters — *how large
is R when there is nothing to see* — and the reading carries ``p`` and
``excess`` next to ``R``.  A caller that ignores them is not using this module.

Causality
=========

Only samples at or before the last one are touched, so appending tomorrow's
prices cannot change today's reading (there is a test that appends them and
compares).  The phase itself is a summary of the end of the window — the
circular mean of the last eighth — because the Hilbert transform has edge
transients and an "instantaneous" phase read at the very last sample would be
mostly an artefact of where the window was cut.

The arithmetic of ``R`` is not re-implemented here: it is
``agent.physics.oscillator.coherence`` from ``ENRG-AI``, the bit-exact port of
``entrain/julia/kuramoto/basic.jl``.  This module owns the market plumbing, the
window, the surrogate and the refusal to answer when the sample is too small.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import numpy as np
from scipy import signal

from monster import enrg

__all__ = ["CoherenceReading", "phase_coherence", "MIN_SERIES", "MIN_WINDOW"]

#: Fewer than three oscillators and "coherence" is a two-body problem: the null
#: is too wide for the reading to say anything.
MIN_SERIES = 3

#: A window shorter than this cannot hold a cycle of anything, so the phase is
#: noise dressed as a measurement.
MIN_WINDOW = 32


@dataclass(frozen=True)
class CoherenceReading:
    """``R`` with the distribution it has to be compared against."""

    ts: float
    symbols: Tuple[str, ...]
    window: int
    surrogates: int
    R: Optional[float]
    null_mean: Optional[float]
    null_p95: Optional[float]
    p_value: Optional[float]
    excess: Optional[float]
    alpha: float
    dominant_cycles: Tuple[float, ...]
    note: str = ""

    @property
    def measured(self) -> bool:
        return self.R is not None

    def is_coherent(self, alpha: Optional[float] = None) -> bool:
        """``True`` only when the crowd beats its own null, never on ``R`` alone."""
        level = self.alpha if alpha is None else float(alpha)
        if self.p_value is None or self.excess is None:
            return False
        return self.p_value <= level and self.excess > 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "symbols": list(self.symbols),
            "window": self.window,
            "surrogates": self.surrogates,
            "R": self.R,
            "null_mean": self.null_mean,
            "null_p95": self.null_p95,
            "p_value": self.p_value,
            "excess": self.excess,
            "alpha": self.alpha,
            "dominant_cycles": list(self.dominant_cycles),
            "coherent": self.is_coherent(),
            "measured": self.measured,
            "note": self.note,
        }

    def describe(self) -> str:
        if not self.measured:
            return f"coherence: unmeasured ({self.note})"
        return (
            f"coherence R={self.R:.3f} null={self.null_mean:.3f} "
            f"p={self.p_value:.3f} excess={self.excess:+.3f} "
            f"({'coherent' if self.is_coherent() else 'indistinguishable from its null'})"
        )


def _unmeasured(ts: float, symbols: Sequence[str], window: int, surrogates: int, alpha: float, note: str):
    return CoherenceReading(
        ts=float(ts),
        symbols=tuple(symbols),
        window=int(window),
        surrogates=int(surrogates),
        R=None,
        null_mean=None,
        null_p95=None,
        p_value=None,
        excess=None,
        alpha=float(alpha),
        dominant_cycles=(),
        note=note,
    )


def _window_of(series: Sequence[float], window: int, name: str) -> np.ndarray:
    values = np.asarray(series, dtype=float)
    if values.ndim != 1:
        raise ValueError(f"{name}: expected a 1-D series, got shape {values.shape}")
    if values.size < window:
        raise ValueError(f"{name}: {values.size} samples is fewer than the window {window}")
    tail = values[-window:]
    if not np.all(np.isfinite(tail)):
        raise ValueError(f"{name}: the window contains a non-finite price")
    if np.any(tail <= 0.0):
        raise ValueError(f"{name}: prices must be positive (log returns are undefined otherwise)")
    return np.log(tail)


def _detrend(values: np.ndarray) -> np.ndarray:
    """Remove the straight line, keep the oscillation.

    A trend is a phase too, and a shared trend would produce a shared phase for
    a reason that has nothing to do with the crowd.  The fit uses only the
    window it is given, so this stays inside the look-ahead barrier.
    """
    clock = np.arange(values.size, dtype=float)
    design = np.column_stack((clock, np.ones_like(clock)))
    coefficients, *_ = np.linalg.lstsq(design, values, rcond=None)
    return values - design @ coefficients


def _phase_summary(detrended: np.ndarray) -> Tuple[float, float]:
    """A window's phase and its dominant frequency (cycles per sample).

    The Hilbert transform gives an analytic signal whose angle is the phase; the
    last eighth of the window is averaged circularly, because the transform's
    edges are transients and a single-sample reading would measure the cut.
    """
    analytic = signal.hilbert(detrended)
    phase = np.unwrap(np.angle(analytic))
    tail = max(8, int(detrended.size // 8))
    tail = min(tail, detrended.size)
    summary = float(np.angle(np.mean(np.exp(1j * phase[-tail:]))))
    cycles = float(np.mean(np.diff(phase[-tail:])) / (2.0 * math.pi)) if tail > 1 else 0.0
    return summary, cycles


def phase_coherence(
    prices: Mapping[str, Sequence[float]],
    *,
    ts: float,
    window: int = 128,
    surrogates: int = 200,
    alpha: float = 0.05,
    seed: int = 0,
) -> CoherenceReading:
    """Read a cross-section's coherence, with the null that could fake it.

    ``prices`` maps a symbol to its price history at one frequency, aligned by
    position (the last sample of every series is the same instant).  Series of
    different lengths are refused rather than silently tail-aligned: a
    misalignment is the one bug this statistic cannot see.
    """
    symbols = tuple(sorted(prices))
    if len(symbols) < MIN_SERIES:
        return _unmeasured(
            ts, symbols, window, surrogates, alpha,
            f"{len(symbols)} series is fewer than the {MIN_SERIES} a cross-section needs",
        )
    if window < MIN_WINDOW:
        return _unmeasured(ts, symbols, window, surrogates, alpha, f"window {window} < {MIN_WINDOW}")
    if surrogates < 20:
        return _unmeasured(
            ts, symbols, window, surrogates, alpha,
            f"{surrogates} surrogates cannot resolve a p-value at alpha {alpha}",
        )
    lengths = {len(np.asarray(prices[symbol], dtype=float)) for symbol in symbols}
    if len(lengths) != 1:
        return _unmeasured(
            ts, symbols, window, surrogates, alpha,
            f"series have different lengths {sorted(lengths)}: align them before asking about phase",
        )

    windows = np.vstack([_detrend(_window_of(prices[symbol], window, symbol)) for symbol in symbols])
    phases, cycles = [], []
    for row in windows:
        phase, frequency = _phase_summary(row)
        phases.append(phase)
        cycles.append(frequency)
    theta = np.asarray(phases, dtype=float).reshape(-1, 1)

    # One copy of the arithmetic: ENRG-AI's port of entrain's order parameter.
    oscillator = enrg.oscillator()
    observed = float(oscillator.coherence(theta))

    rng = np.random.default_rng(seed)
    low, high = window // 8, window - window // 8
    null: list[float] = []
    for _ in range(surrogates):
        shifted = np.empty_like(windows)
        for index, row in enumerate(windows):
            shifted[index] = np.roll(row, int(rng.integers(low, high)))
        surrogate_phases = np.asarray([_phase_summary(row)[0] for row in shifted], dtype=float)
        null.append(float(oscillator.coherence(surrogate_phases.reshape(-1, 1))))
    null_array = np.asarray(null, dtype=float)
    null_mean = float(null_array.mean())
    null_p95 = float(np.quantile(null_array, 0.95))
    p_value = float((1 + int(np.sum(null_array >= observed))) / (surrogates + 1))

    return CoherenceReading(
        ts=float(ts),
        symbols=symbols,
        window=int(window),
        surrogates=int(surrogates),
        R=observed,
        null_mean=null_mean,
        null_p95=null_p95,
        p_value=p_value,
        excess=observed - null_mean,
        alpha=float(alpha),
        dominant_cycles=tuple(cycles),
        note="",
    )

