"""A quote is a claim; the truth check says whether it may be believed."""
from __future__ import annotations

import pytest

from monster.brain.truth import cross_venue_truth
from monster.contracts import Quote, Snapshot


def _quote(venue: str, mid: float, *, ts: float = 100.0, half_spread: float = 0.01) -> Quote:
    return Quote(venue=venue, symbol="btcusdt", bid=mid - half_spread, ask=mid + half_spread, ts=ts)


def test_three_venues_that_agree_are_clean_and_checked():
    snapshot = Snapshot(ts=100.0, quotes=(_quote("htx", 100.0), _quote("okx", 100.02), _quote("bybit", 99.99)))
    report = cross_venue_truth(snapshot)
    assert report.clean
    assert report.checked == ("btcusdt",)
    assert report.unmeasured == ()


def test_one_venue_far_from_the_median_is_flagged():
    snapshot = Snapshot(ts=100.0, quotes=(_quote("htx", 100.0), _quote("okx", 100.02), _quote("bad", 105.0)))
    report = cross_venue_truth(snapshot)
    finding = report.flagged("bad", "btcusdt")
    assert finding is not None
    assert finding.deviation_bps is not None and finding.deviation_bps > 400
    assert finding.z is not None and abs(finding.z) > 4.0
    assert report.flagged("htx", "btcusdt") is None
    assert not report.clean


def test_two_venues_is_unmeasured_not_clean():
    snapshot = Snapshot(ts=100.0, quotes=(_quote("htx", 100.0), _quote("okx", 130.0)))
    report = cross_venue_truth(snapshot)
    assert report.findings == ()
    assert report.checked == ()
    assert len(report.unmeasured) == 1 and "2 venue(s)" in report.unmeasured[0]


def test_asking_for_two_venues_is_refused_rather_than_honoured():
    """The median of two mids is one of them; a "disagreement" found that way is a
    coin flip wearing the word "measured".  Weaker checks need a commit, not a flag."""
    snapshot = Snapshot(ts=100.0, quotes=(_quote("htx", 100.0), _quote("okx", 130.0)))
    with pytest.raises(ValueError, match="MIN_VENUES=3"):
        cross_venue_truth(snapshot, min_venues=2)
    assert cross_venue_truth(snapshot, min_venues=3).checked == ()


def test_identical_books_and_one_outlier_are_flagged_without_a_scale():
    snapshot = Snapshot(ts=100.0, quotes=(_quote("a", 100.0), _quote("b", 100.0), _quote("c", 100.0), _quote("d", 102.0)))
    report = cross_venue_truth(snapshot)
    finding = report.flagged("d", "btcusdt")
    assert finding is not None
    assert finding.z is None
    assert "no scale" in finding.reason


def test_a_stale_quote_is_flagged_even_when_its_price_agrees():
    snapshot = Snapshot(
        ts=200.0,
        quotes=(_quote("htx", 100.0, ts=100.0), _quote("okx", 100.0, ts=199.9), _quote("bybit", 100.0, ts=199.9)),
    )
    report = cross_venue_truth(snapshot, max_age_s=5.0)
    finding = report.flagged("htx", "btcusdt")
    assert finding is not None and "stale" in finding.reason


def test_a_quote_from_the_future_is_a_clock_skew():
    snapshot = Snapshot(ts=100.0, quotes=(_quote("htx", 100.0), _quote("okx", 100.0), _quote("bybit", 100.0)))
    report = cross_venue_truth(snapshot, now=90.0)
    finding = report.flagged("htx", "btcusdt")
    assert finding is not None and "future" in finding.reason


def test_the_report_is_json_shaped_for_the_journal():
    snapshot = Snapshot(ts=100.0, quotes=(_quote("htx", 100.0), _quote("okx", 100.0), _quote("bybit", 100.0)))
    payload = cross_venue_truth(snapshot).to_dict()
    assert payload["clean"] is True
    assert payload["checked"] == ["btcusdt"]
    assert set(payload) == {"ts", "checked", "findings", "unmeasured", "clean"}
