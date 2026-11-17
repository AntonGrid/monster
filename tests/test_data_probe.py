"""Probe: one question per endpoint, no retries, and nothing written."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from monster.data.__main__ import main as data_main
from monster.data.capture import FileTransport
from monster.data.probe import probe

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "raw-fixtures"


def test_probe_asks_all_eighteen_endpoints_and_reads_each_one():
    results = probe(FileTransport(FIXTURES))
    assert len(results) == 18
    assert all(item.ok for item in results)
    assert {item.endpoint.venue for item in results} == {"htx", "okx", "bybit"}
    assert all(item.latency_ms >= 0.0 for item in results)

    spot = next(item for item in results if item.endpoint.venue == "htx" and item.endpoint.name == "spot-btc")
    assert spot.reading == "34999.5 / 35000.5"
    funding = next(item for item in results if item.endpoint.venue == "okx" and item.endpoint.name == "funding-btc")
    assert funding.reading == "+5.000 bps/8h"


def test_one_changed_shape_is_one_failed_line_and_the_others_still_answer(tmp_path):
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixtures)
    path = fixtures / "htx" / "spot-btc.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["tick"]["bid"]
    path.write_text(json.dumps(document), encoding="utf-8")

    results = probe(FileTransport(fixtures))
    assert len(results) == 18
    failed = [item for item in results if not item.ok]
    assert len(failed) == 1
    assert failed[0].endpoint.name == "spot-btc" and "bid" in failed[0].error
    assert failed[0].describe().startswith("  [fail] htx")


def test_an_empty_directory_fails_every_endpoint_without_raising(tmp_path):
    results = probe(FileTransport(tmp_path))
    assert len(results) == 18
    assert all("no recorded answer" in item.error for item in results)


def test_the_probe_command_exits_zero_only_when_every_endpoint_parsed(capsys):
    assert data_main(["probe", "--fixtures", str(FIXTURES)]) == 0
    printed = capsys.readouterr().out
    assert "probe: 18/18 endpoint(s) answered and parsed" in printed
    assert printed.count("[ok]") == 18


def test_the_probe_command_names_what_it_could_not_use(tmp_path, capsys):
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixtures)
    path = fixtures / "htx" / "spot-btc.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["tick"]["bid"]
    path.write_text(json.dumps(document), encoding="utf-8")

    assert data_main(["probe", "--fixtures", str(fixtures)]) == 1
    printed = capsys.readouterr().out
    assert "probe: 17/18" in printed
    assert "not usable: htx spot-btc" in printed


def test_probe_json_is_machine_readable_and_names_the_endpoint(capsys):
    assert data_main(["probe", "--fixtures", str(FIXTURES), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 18
    bybit = next(item for item in payload if item["venue"] == "bybit" and item["name"] == "funding-btc")
    assert bybit["ok"] is True and bybit["kind"] == "funding" and bybit["reading"].endswith("bps/8h")
    assert bybit["native"] == "BTCUSDT"
