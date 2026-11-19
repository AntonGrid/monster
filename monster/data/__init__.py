"""The data layer: capture, store, audit, and the as-of view a cycle may see.

The one law
===========

**Raw is truth; normalized is derived.**  Every capture writes the venue's body
exactly as it arrived, next to the request it answered and our receive time.
Quotes and funding rates are then parsed from that body and written beside it.
Nothing can be "fixed" in the normalized rows without the raw rows disagreeing
in the next audit, which is the point: a backtest is a claim about the past, and
the past has to be reproducible from evidence.

The one seam
============

A :class:`monster.data.capture.Transport` is the only thing that knows whether
the answers come from the network or from a file.  ``FileTransport`` replays
recorded bodies (one per endpoint, or a tape of samples); ``UrllibTransport``
queries the three venues.  Everything downstream — retry, raw write, parse,
deduplication, gaps, coverage, as-of snapshots, aligned history — is the same
code on a laptop and on the VPS.  That is why the offline tests mean something.

The three venues
================

HTX is the trading venue (spot and USDT-margined swap); OKX and Bybit are the
witnesses.  A symbol quoted by three venues can be *checked*; a symbol quoted by
one is recorded as ``unmeasured``, because one observation cannot disagree with
itself.  The instrument tables in :mod:`monster.instruments` are what make the
witnesses speak about the same instrument.
"""
from __future__ import annotations

from monster.data.capture import (
    CaptureReport,
    Capturer,
    Endpoint,
    Failure,
    FileTransport,
    Transport,
    TransportError,
    UrllibTransport,
    endpoints,
)
from monster.data.derive import DataError, Derivation, derive, history_window, snapshot_from
from monster.data.doctor import Check, Diagnosis, diagnose
from monster.data.probe import Probe, probe
from monster.data.quality import Audit, Coverage, Gap, audit_day, coverage, find_gaps
from monster.data.raw import RawPayload, ShapeError, decimal, pick, timestamp_s
from monster.data.store import Batch, Conflict, Store, StoreError
from monster.data.truth_report import TruthDay, TruthEvent, truth_day

__all__ = [
    "Audit",
    "Batch",
    "CaptureReport",
    "Capturer",
    "Check",
    "Conflict",
    "Coverage",
    "DataError",
    "Derivation",
    "Diagnosis",
    "Endpoint",
    "Failure",
    "FileTransport",
    "Gap",
    "Probe",
    "RawPayload",
    "ShapeError",
    "Store",
    "StoreError",
    "Transport",
    "TransportError",
    "TruthDay",
    "TruthEvent",
    "UrllibTransport",
    "audit_day",
    "coverage",
    "decimal",
    "derive",
    "diagnose",
    "endpoints",
    "find_gaps",
    "history_window",
    "pick",
    "probe",
    "snapshot_from",
    "timestamp_s",
    "truth_day",
]
