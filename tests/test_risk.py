"""A limit that never fires is either sized perfectly or decorative; test both."""
from __future__ import annotations

import numpy as np
import pytest

from monster.contracts import Intent, Leg
from monster.risk.limits import Exposure, KillSwitch, Limits, check, historical_var


def _carry(notional: float = 100.0, edge: float = 12.0) -> Intent:
    return Intent(
        strategy="carry",
        legs=(
            Leg(venue="htx", symbol="btcusdt", side="buy", kind="spot", notional_usd=notional),
            Leg(venue="htx", symbol="btcusdt", side="sell", kind="perp", notional_usd=notional),
        ),
        expected_edge_bps=edge,
        horizon_hours=8.0,
        rationale="delta-neutral carry",
    )


def _book(**overrides) -> Exposure:
    values = {
        "gross_usd": 0.0,
        "net_delta_usd": 0.0,
        "equity_usd": 1_000.0,
        "free_cash_usd": 1_000.0,
        "day_pnl_usd": 0.0,
        "symbol_usd": {},
        "venue_usd": {},
    }
    values.update(overrides)
    return Exposure(**values)


def test_a_delta_neutral_carry_inside_the_book_passes():
    assert check(_carry(), _book(), Limits()) == ()


def test_every_refusal_has_a_code_and_a_sentence():
    breached = check(_carry(edge=-1.0), _book(equity_usd=0.0, day_pnl_usd=-50.0), Limits())
    codes = {breach.code for breach in breached}
    assert {"NO_EQUITY", "DAILY_LOSS", "NO_EDGE"} <= codes
    for breach in breached:
        assert breach.detail and breach.detail[0].islower() or breach.detail[0].isupper()


def test_gross_is_projected_not_checked_leg_by_leg():
    limits = Limits(max_gross_usd=150.0)
    # Each leg is 100, so a leg-at-a-time check would pass; the pair is 200.
    breaches = check(_carry(notional=100.0), _book(), limits)
    assert [breach.code for breach in breaches] == ["GROSS"]
    assert "200.00" in breaches[0].detail


def test_net_delta_catches_a_one_sided_book():
    intent = Intent(
        strategy="momentum",
        legs=(Leg(venue="htx", symbol="btcusdt", side="buy", kind="perp", notional_usd=300.0),),
        expected_edge_bps=5.0,
        horizon_hours=4.0,
        rationale="one leg is not neutral",
    )
    breaches = check(intent, _book(), Limits(max_net_delta_usd=200.0))
    assert [breach.code for breach in breaches] == ["NET_DELTA"]


def test_symbol_and_venue_limits_include_the_existing_book():
    limits = Limits(max_symbol_usd=250.0, max_venue_usd=250.0)
    book = _book(symbol_usd={"btcusdt": 100.0}, venue_usd={"htx": 100.0})
    codes = {breach.code for breach in check(_carry(notional=100.0), book, limits)}
    # 100 existing + 200 proposed on the same symbol/venue is 300 > 250.
    assert {"SYMBOL", "VENUE"} <= codes


def test_leverage_and_cash_are_separate_refusals():
    limits = Limits(max_leverage=2.0, min_free_cash_usd=50.0)
    breaches = check(_carry(notional=100.0), _book(equity_usd=100.0, free_cash_usd=100.0), limits)
    codes = {breach.code for breach in breaches}
    # gross 200 / equity 100 = 2.0x passes; spot margin 100 + reserve 50 > 100 fails.
    assert codes == {"NO_CASH"}

    breaches = check(_carry(notional=100.0), _book(equity_usd=50.0), limits)
    codes = {breach.code for breach in breaches}
    assert "LEVERAGE" in codes


def test_var_refuses_to_guess_from_a_handful_of_days():
    thin = historical_var([0.01, -0.02, 0.03], 1_000.0)
    assert not thin.measured and thin.var_usd is None
    assert "3 observations" in thin.note and "unmeasured" in thin.describe()

    rng = np.random.default_rng(5)
    returns = rng.normal(0.0, 0.02, size=500)
    report = historical_var(returns, 1_000.0, level=0.95)
    assert report.measured and report.var_usd is not None and report.var_usd > 0
    assert report.cvar_usd is not None and report.cvar_usd >= report.var_usd
    assert report.n == 500


def test_var_rejects_a_level_that_is_not_a_tail():
    with pytest.raises(ValueError, match="level"):
        historical_var([0.0] * 100, 1_000.0, level=1.5)
    with pytest.raises(ValueError, match="notional"):
        historical_var([0.0] * 100, 0.0)


def test_a_kill_switch_needs_a_reason_and_a_named_reset():
    switch = KillSwitch()
    with pytest.raises(ValueError, match="reason"):
        switch.trip("   ")
    switch.trip("daily loss 21.30 > 20.00", ts=100.0)
    assert switch.tripped and switch.ts == 100.0
    with pytest.raises(ValueError, match="named operator"):
        switch.reset("")
    switch.reset("architect")
    assert not switch.tripped and "reset by architect" == switch.reason
    assert KillSwitch.from_dict(switch.to_dict()) == switch
