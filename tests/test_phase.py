"""Coherence is only a reading when it is compared with its own null."""
from __future__ import annotations

import numpy as np
import pytest

from monster.brain.phase import MIN_SERIES, phase_coherence


def _locked(n: int = 200, symbols: int = 5, seed: int = 7) -> dict:
    """Series that share a cycle and differ only by a little noise."""
    rng = np.random.default_rng(seed)
    clock = np.arange(n, dtype=float)
    base = 0.10 * np.sin(2.0 * np.pi * clock / 32.0)
    return {
        f"s{index}": 100.0 * np.exp(base + 0.002 * rng.standard_normal(n))
        for index in range(symbols)
    }


def _scattered(n: int = 200, symbols: int = 5, seed: int = 11) -> dict:
    """Series with the same amplitude but independent phases."""
    rng = np.random.default_rng(seed)
    clock = np.arange(n, dtype=float)
    return {
        f"s{index}": 100.0 * np.exp(
            0.10 * np.sin(2.0 * np.pi * clock / 32.0 + rng.uniform(0.0, 2.0 * np.pi))
            + 0.002 * rng.standard_normal(n)
        )
        for index in range(symbols)
    }


def test_a_locked_cross_section_beats_its_null():
    reading = phase_coherence(_locked(), ts=1.0, window=64, surrogates=100)
    assert reading.measured
    assert reading.R is not None and reading.R > 0.9
    assert reading.null_p95 is not None and reading.R > reading.null_p95
    assert reading.p_value is not None and reading.p_value <= 0.05
    assert reading.excess is not None and reading.excess > 0.0
    assert reading.is_coherent()
    assert "coherent" in reading.describe()


def test_independent_phases_are_not_a_crowd():
    reading = phase_coherence(_scattered(), ts=1.0, window=64, surrogates=100, seed=3)
    assert reading.measured
    # The point is not that R is small — it is that R is not larger than what
    # the same series produce with their alignment destroyed.
    assert not reading.is_coherent()
    assert reading.p_value is not None and reading.p_value > 0.05


def test_the_reading_depends_only_on_the_window_it_was_given():
    prices = _locked()
    short = {symbol: values[-64:] for symbol, values in prices.items()}
    longer = {symbol: np.concatenate([np.full(100, 50.0), values[-64:]]) for symbol, values in prices.items()}
    first = phase_coherence(short, ts=1.0, window=64, surrogates=50, seed=5)
    second = phase_coherence(longer, ts=1.0, window=64, surrogates=50, seed=5)
    assert first.R == pytest.approx(second.R)
    assert first.p_value == second.p_value
    # A constant prehistory is detrended away inside the window, so the only
    # thing that could move the reading is data the caller chose to include.


def test_the_same_seed_gives_the_same_null():
    prices = _scattered(seed=13)
    first = phase_coherence(prices, ts=1.0, window=64, surrogates=60, seed=21)
    second = phase_coherence(prices, ts=1.0, window=64, surrogates=60, seed=21)
    assert first.to_dict() == second.to_dict()


def test_unmeasured_is_a_sentence_not_a_zero():
    two = _locked(symbols=2)
    reading = phase_coherence(two, ts=1.0, window=64)
    assert not reading.measured
    assert reading.R is None and reading.p_value is None
    assert not reading.is_coherent()
    assert str(MIN_SERIES) in reading.note
    assert "unmeasured" in reading.describe()

    unequal = _locked()
    unequal["s0"] = unequal["s0"][:-5]
    reading = phase_coherence(unequal, ts=1.0, window=64)
    assert not reading.measured and "different lengths" in reading.note

    reading = phase_coherence(_locked(), ts=1.0, window=16)
    assert not reading.measured and "window" in reading.note

    reading = phase_coherence(_locked(), ts=1.0, window=64, surrogates=5)
    assert not reading.measured and "surrogates" in reading.note


def test_a_hole_or_a_non_positive_price_is_an_error_not_a_zero():
    prices = _locked()
    prices["s0"] = prices["s0"].copy()
    prices["s0"][-1] = 0.0
    with pytest.raises(ValueError, match="positive"):
        phase_coherence(prices, ts=1.0, window=64)

    prices = _locked()
    prices["s0"] = prices["s0"].copy()
    prices["s0"][-1] = np.nan
    with pytest.raises(ValueError, match="non-finite"):
        phase_coherence(prices, ts=1.0, window=64)

    prices = _locked()
    prices = {symbol: values[-16:] for symbol, values in prices.items()}
    with pytest.raises(ValueError, match="fewer than the window"):
        phase_coherence(prices, ts=1.0, window=64)

    # A single short series is a structural mismatch: unmeasured, with the
    # lengths named, because tail-aligning it silently is the bug this
    # statistic cannot see.
    prices = _locked()
    prices["s0"] = prices["s0"][-16:]
    reading = phase_coherence(prices, ts=1.0, window=64)
    assert not reading.measured and "different lengths" in reading.note
