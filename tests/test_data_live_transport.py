"""The live transport, exercised over real HTTP against a local server.

The VPS runs ``UrllibTransport``; every other test runs ``FileTransport``.  This
file closes that gap without the internet: a throwaway ``http.server`` on
127.0.0.1 serves the same bodies, so the code path under test is urllib itself —
the request line, the ``User-Agent`` header, the status codes, the JSON decode,
the context manager, the socket timeout.  If this passes and ``probe --live``
passes on the VPS, the only untested thing left is the venues' goodwill.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from monster.data.capture import Capturer, Endpoint, UrllibTransport, endpoints
from monster.data.probe import probe
from monster.data.quality import audit_day
from monster.data.store import Store

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "raw-fixtures"
FIXTURE_TS = 1_794_916_800.0
USER_AGENT = "enrg-monster/0.2 (paper; no credentials)"
#: The refusal HTX sent 64 times on 18.11, and ~390 more times with a status
#: instead of a body.  It is a 429 whose reason lives in the JSON.
THROTTLED = {"err-code": "invalid-parameter", "err-msg": "request limit", "status": "error"}


@pytest.fixture
def venue_server():
    """A local HTTP server that answers like the venues do: one body per path."""
    state = {"seen": [], "fail": set(), "throttle": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the name is the HTTP method
            path = self.path.split("?", 1)[0].strip("/")
            state["seen"].append((path, self.headers.get("User-Agent", "")))
            if state["throttle"].get(path, 0) > 0:
                # HTX's swap host is the model: a 429 with a JSON body that
                # names the reason, and a Retry-After we can honour.
                state["throttle"][path] -= 1
                refusal = json.dumps(THROTTLED).encode("utf-8")
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", "2")
                self.send_header("Content-Length", str(len(refusal)))
                self.end_headers()
                self.wfile.write(refusal)
                return
            if path in state["fail"]:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = (FIXTURES / f"{path}.json").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # keep pytest's output clean
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def _local(port: int) -> tuple:
    """The same endpoint table, pointed at the local server."""
    return tuple(
        replace(endpoint, url=f"http://127.0.0.1:{port}/{endpoint.venue}/{endpoint.name}")
        for endpoint in endpoints()
    )


def test_probe_over_http_reads_all_eighteen_and_sends_our_user_agent(venue_server):
    port, state = venue_server
    results = probe(UrllibTransport(timeout=5.0), _local(port))
    assert [item.endpoint.name for item in results if not item.ok] == []
    assert len(results) == 18
    assert {agent for _, agent in state["seen"]} == {USER_AGENT}
    assert len(state["seen"]) == 18


def test_capture_over_http_writes_what_audit_can_reproduce(tmp_path, venue_server):
    port, state = venue_server
    store = Store(tmp_path / "data")
    report = Capturer(store, UrllibTransport(timeout=5.0)).capture(at=FIXTURE_TS, wanted=_local(port))
    assert report.ok and report.written == 18 and report.raw == 18
    assert len(state["seen"]) == 18

    audit = audit_day(store, Store.day_of(FIXTURE_TS))
    assert audit.ok and audit.matched == 18 and audit.raw == 18


def test_a_500_is_a_transport_error_and_is_retried(tmp_path, venue_server):
    port, state = venue_server
    state["fail"].add("htx/spot-btc")
    wanted = tuple(item for item in _local(port) if item.name == "spot-btc" and item.venue == "htx")
    assert len(wanted) == 1
    store = Store(tmp_path / "data")
    report = Capturer(
        store, UrllibTransport(timeout=5.0), retries=2, sleep=lambda delay: None
    ).capture(at=FIXTURE_TS, wanted=wanted)
    assert not report.ok and report.raw == 0 and report.written == 0
    assert "500" in report.failures[0].reason
    assert "after 3 attempts" in report.failures[0].reason
    assert len([path for path, _ in state["seen"] if path == "htx/spot-btc"]) == 3
    # Nothing was fetched, so nothing could be stored: an absent reading is not
    # a zero reading.
    assert store.days() == ()


def test_a_429_over_http_keeps_its_body_and_is_asked_again(tmp_path, venue_server):
    """The costume that cost us ~390 readings on 18.11.

    ``urllib`` raises on the status, so the body is only reachable through the
    exception; dropping it is what left 64 bodies explaining a hole of 456.
    """
    port, state = venue_server
    state["throttle"]["htx/spot-btc"] = 1
    wanted = tuple(item for item in _local(port) if item.name == "spot-btc" and item.venue == "htx")
    slept: list[float] = []
    store = Store(tmp_path / "data")
    report = Capturer(store, UrllibTransport(timeout=5.0), sleep=slept.append).capture(
        at=FIXTURE_TS, wanted=wanted
    )
    assert report.ok and report.written == 1 and report.retried == 1
    # The venue said "in two seconds" and the venue was believed.
    assert slept == [2.0]
    assert len(list(store.raws())) == 1  # the answer, not the refusal


def test_a_429_that_never_clears_leaves_the_refusal_as_evidence(tmp_path, venue_server):
    port, state = venue_server
    state["throttle"]["htx/spot-btc"] = 10
    wanted = tuple(item for item in _local(port) if item.name == "spot-btc" and item.venue == "htx")
    slept: list[float] = []
    store = Store(tmp_path / "data")
    report = Capturer(store, UrllibTransport(timeout=5.0), sleep=slept.append).capture(
        at=FIXTURE_TS, wanted=wanted
    )
    assert not report.ok and report.raw == 1 and report.written == 0
    reason = report.failures[0].reason
    assert "request limit" in reason and "after 3 attempts" in reason
    # Three tries at the venue's own 2 s pace, and then the pass gives up
    # rather than dragging a reading past the truth check's tolerance.
    assert slept == [2.0, 2.0]
    kept = list(store.raws())
    assert kept and kept[0].body["err-msg"] == "request limit"

