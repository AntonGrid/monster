"""Capture: retries, one answer for two readings, raw-before-parse, our clock."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from monster.data import capture as capture_module
from monster.data.capture import Capturer, FileTransport, TransportError, UrllibTransport, endpoints
from monster.data.store import Store
from monster.data.synthetic import write_demo_tape

BASE = 1_794_916_800.0
FIXTURES = Path(__file__).resolve().parents[1] / "data" / "raw-fixtures"


def test_the_endpoint_table_covers_three_venues_and_three_readings():
    table = endpoints()
    assert len(table) == 18
    assert {item.venue for item in table} == {"htx", "okx", "bybit"}
    assert {(item.asset, item.kind) for item in table} == {
        (asset, kind) for asset in ("btc", "eth") for kind in ("spot", "perp", "funding")
    }
    bybit_linear = [item for item in table if item.venue == "bybit" and item.kind != "spot"]
    assert all(dict(item.params)["category"] == "linear" for item in bybit_linear)


def test_a_missing_recording_is_a_transport_error(tmp_path):
    with pytest.raises(TransportError, match="no recorded answer"):
        FileTransport(tmp_path).get(endpoints()[0])


def test_a_tape_repeats_its_last_sample_instead_of_inventing_one(tmp_path):
    write_demo_tape(tmp_path, samples=2, interval_s=300.0)
    transport = FileTransport(tmp_path)
    endpoint = next(item for item in endpoints() if item.venue == "htx" and item.name == "spot-btc")
    first, second, third = transport.get(endpoint), transport.get(endpoint), transport.get(endpoint)
    assert first["ts"] != second["ts"]
    assert third == second


def test_capture_writes_raw_and_normalized_for_the_fixtures(tmp_path):
    store = Store(tmp_path)
    report = Capturer(store, FileTransport(FIXTURES)).capture(at=BASE)
    assert report.ok and report.written == 18 and report.raw == 18
    assert len(store.quotes()) == 12 and len(store.fundings()) == 6
    assert {quote.venue for quote in store.quotes()} == {"htx", "okx", "bybit"}
    assert store.days() == ("2026-11-17",)


def test_one_answer_serves_two_readings_on_bybit(tmp_path):
    class Counting(FileTransport):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.calls: list[tuple[str, str]] = []

        def get(self, endpoint):
            self.calls.append((endpoint.venue, endpoint.name))
            return super().get(endpoint)

    transport = Counting(FIXTURES)
    assert Capturer(Store(tmp_path), transport).capture(at=BASE).ok
    bybit_btc = [name for venue, name in transport.calls if venue == "bybit" and name.endswith("-btc")]
    # spot-btc is one request; perp-btc and funding-btc are the same ticker.
    assert bybit_btc == ["funding-btc", "spot-btc"]


def test_a_broken_shape_is_reported_and_writes_no_reading(tmp_path):
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixtures)
    path = fixtures / "okx" / "spot-btc.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["data"][0]["bidPx"] = ""
    path.write_text(json.dumps(document), encoding="utf-8")

    store = Store(tmp_path / "data")
    slept: list[float] = []
    report = Capturer(store, FileTransport(fixtures), sleep=slept.append).capture(at=BASE)
    assert not report.ok and len(report.failures) == 1
    assert "okx spot-btc" in report.failures[0].endpoint
    assert "no book" in report.failures[0].reason
    assert len(store.quotes()) == 11  # the evidence is kept, the reading is not
    # A moved schema is a fact, not a delay: asking the same question again
    # would return the same answer and hide the breakage behind a retry.
    assert slept == [] and report.retried == 0


def test_a_transport_that_fails_once_is_retried(tmp_path):
    class Flaky(FileTransport):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.fail_next = True

        def get(self, endpoint):
            if self.fail_next:
                self.fail_next = False
                raise TransportError("connection reset")
            return super().get(endpoint)

    slept: list[float] = []
    report = Capturer(Store(tmp_path), Flaky(FIXTURES), sleep=slept.append).capture(at=BASE)
    assert report.ok and report.retried == 1
    # One second, not half of one: the old delay landed inside the same
    # one-second limiter window that had just refused us.
    assert slept == [1.0]


def test_a_transport_that_never_answers_fails_after_the_retries(tmp_path):
    class Dead:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, endpoint):
            self.calls += 1
            raise TransportError("timeout")

    transport = Dead()
    slept: list[float] = []
    report = Capturer(Store(tmp_path), transport, retries=2, sleep=slept.append).capture(at=BASE)
    assert not report.ok and len(report.failures) == 18
    # Two Bybit tickers serve two readings each, so 16 requests are attempted
    # three times and the cached failure is reported for the remaining endpoints.
    assert transport.calls == 16 * 3
    # The delays double and stay inside the 4 s budget: a third try would need
    # 4 s more, and a reading that late is not one the truth check can use.
    assert slept == [1.0, 2.0] * 16


def test_received_time_is_ours_and_funding_is_stamped_with_it(tmp_path):
    store = Store(tmp_path)
    at = BASE + 42.0
    Capturer(store, FileTransport(FIXTURES)).capture(at=at)
    assert all(quote.ts == BASE for quote in store.quotes())  # the venue's clock
    assert all(rate.ts == at for rate in store.fundings())  # ours


def test_a_pass_issues_its_requests_together(tmp_path):
    import threading
    import time

    class SlowTransport:
        def __init__(self) -> None:
            self.calls = 0
            self.in_flight = 0
            self.peak = 0
            self.lock = threading.Lock()

        def get(self, endpoint):
            with self.lock:
                self.calls += 1
                self.in_flight += 1
                self.peak = max(self.peak, self.in_flight)
            time.sleep(0.05)
            with self.lock:
                self.in_flight -= 1
            return json.loads((FIXTURES / endpoint.venue / f"{endpoint.name}.json").read_text(encoding="utf-8"))

    together = SlowTransport()
    assert Capturer(Store(tmp_path / "a"), together, workers=8).capture(at=BASE).written == 18
    assert together.calls == 16  # Bybit's three readings are two questions
    assert together.peak > 1  # one pass is one moment, not sixteen moments in a queue

    alone = SlowTransport()
    assert Capturer(Store(tmp_path / "b"), alone, workers=1).capture(at=BASE).written == 18
    assert alone.calls == 16 and alone.peak == 1


def test_the_capture_grid_wakes_early_instead_of_drifting(monkeypatch):
    from monster.data.__main__ import _sleep_until

    slept: list[float] = []

    class Clock:
        def __init__(self) -> None:
            self.now = 100.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()

    def sleep(delay: float) -> None:
        slept.append(delay)
        clock.now += delay

    assert _sleep_until(102.5, clock=clock, sleep=sleep) == pytest.approx(2.5)
    assert _sleep_until(103.0, clock=clock, sleep=sleep) == pytest.approx(0.5)
    # A target already in the past never sleeps: a slow pass must not push the
    # whole week's cadence behind the wall clock.
    assert _sleep_until(100.0, clock=clock, sleep=sleep) == 0.0
    assert slept == [2.5, 0.5]



    seen: list[str] = []

    class Answer:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self) -> bytes:
            return self.body

    def fake(request, timeout):
        seen.append(request.full_url)
        return Answer(b'{"ok": true}')

    monkeypatch.setattr(capture_module, "urlopen", fake)
    endpoint = next(item for item in endpoints() if item.venue == "bybit" and item.name == "perp-btc")
    assert UrllibTransport().get(endpoint) == {"ok": True}
    assert "category=linear" in seen[0] and "symbol=BTCUSDT" in seen[0]

    monkeypatch.setattr(capture_module, "urlopen", lambda request, timeout: Answer(b"<html>nope</html>"))
    with pytest.raises(TransportError, match="not JSON"):
        UrllibTransport().get(endpoint)


#: The exact document HTX's swap host sent on 18.11, 64 times in one day, while
#: the same host also refused ~390 requests with an HTTP status and no body we
#: kept.  Both costumes are the same event, and both must reach the same verdict.
REFUSAL = {
    "err-code": "invalid-parameter",
    "err-msg": "request limit",
    "status": "error",
    "ts": 1_794_960_912_869,
}


def test_a_rate_limit_document_is_asked_again_and_the_reading_survives(tmp_path):
    class Throttled(FileTransport):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.refuse = True

        def get(self, endpoint):
            if self.refuse and endpoint.venue == "htx" and endpoint.name == "perp-btc":
                self.refuse = False
                return REFUSAL
            return super().get(endpoint)

    store = Store(tmp_path)
    slept: list[float] = []
    report = Capturer(store, Throttled(FIXTURES), sleep=slept.append).capture(at=BASE)
    assert report.ok and report.written == 18
    assert report.retried == 1 and slept == [1.0]
    # The refusal was superseded by the answer, so the day keeps 18 bodies and
    # the audit keeps its meaning: a stored body is a body that carried data.
    assert report.raw == 18 and len(list(store.raws())) == 18
    assert len(store.quotes()) == 12


def test_a_refusal_that_never_heals_is_kept_as_evidence_and_named(tmp_path):
    class AlwaysRefused(FileTransport):
        def get(self, endpoint):
            if endpoint.venue == "htx" and endpoint.name == "perp-btc":
                raise TransportError(
                    "htx perp-btc: HTTP 429 Too Many Requests",
                    body=REFUSAL,
                    status=429,
                    retryable=True,
                )
            return super().get(endpoint)

    store = Store(tmp_path)
    slept: list[float] = []
    report = Capturer(store, AlwaysRefused(FIXTURES), sleep=slept.append).capture(at=BASE)
    assert not report.ok and len(report.failures) == 1
    reason = report.failures[0].reason
    # The sentence says both what happened and how hard we tried, so a morning
    # reader can tell one bad minute from a chronic refusal.
    assert "request limit" in reason and "after 3 attempts" in reason
    assert slept == [1.0, 2.0]
    # A 429's JSON body is evidence, not noise: it is stored, counted and
    # re-readable, which is exactly what the 390 missing bodies were not.
    assert report.raw == 18 and report.written == 17
    kept = [item for item in store.raws() if item.venue == "htx" and item.endpoint == "perp-btc"]
    assert kept and kept[0].body["err-msg"] == "request limit"


def test_the_venues_retry_after_is_honoured_when_the_budget_allows(tmp_path):
    class Told(FileTransport):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.refuse = True

        def get(self, endpoint):
            if self.refuse and endpoint.venue == "htx" and endpoint.name == "perp-btc":
                self.refuse = False
                raise TransportError("slow down", status=429, retryable=True, retry_after=3.0)
            return super().get(endpoint)

    slept: list[float] = []
    report = Capturer(Store(tmp_path), Told(FIXTURES), sleep=slept.append).capture(at=BASE)
    assert report.ok and slept == [3.0]


def test_a_retry_after_beyond_the_budget_is_refused_rather_than_waited_out(tmp_path):
    class AlwaysTold(FileTransport):
        def get(self, endpoint):
            if endpoint.venue == "htx" and endpoint.name == "perp-btc":
                raise TransportError("slow down", status=429, retryable=True, retry_after=30.0)
            return super().get(endpoint)

    slept: list[float] = []
    report = Capturer(Store(tmp_path), AlwaysTold(FIXTURES), sleep=slept.append).capture(at=BASE)
    # Waiting 30 s would hand the pass a reading from the next slot and file it
    # under this one: the endpoint fails this pass, and says so.
    assert not report.ok and slept == [] and report.retried == 0


def test_requests_to_one_host_are_spaced_instead_of_bursted(tmp_path):
    wanted = [item for item in endpoints() if item.venue in ("htx", "okx") and item.kind == "spot"]
    slept: list[float] = []
    report = Capturer(Store(tmp_path), FileTransport(FIXTURES), spacing_s=0.25, sleep=slept.append).capture(
        at=BASE, wanted=wanted
    )
    assert report.ok and report.written == 4
    # Two hosts, two requests each: each host's second request waits one
    # spacing, and neither host waits for the other.
    assert sorted(slept) == [0.25, 0.25]

