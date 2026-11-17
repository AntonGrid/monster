"""ENRG Monster — the learning layer of the trading organism.

What this package is
====================

A *control plane*, not a strategy.  It exists because three repositories each
hold one true thing and none of them holds the loop between the three:

* ``ENRG-AI`` (Python) knows how to judge a hypothesis — pre-registered
  criteria, power, nulls, walk-forward — and it refuses to lie about a number;
* ``ENRG-HFT`` (Rust) knows how to touch a venue — scanner, websocket, orders,
  recovery, exits — and it refuses to open a short on spot;
* ``entrain`` (Julia) knows how to read a medium — Kuramoto coherence, link
  memory, the difference between an anomaly and a crowd — and it refuses to be
  a signature.

The monster is the loop: *observe → record → judge → size → act → autopsy*.
Every step writes to one journal, and every refusal is a first-class event.
A strategy that cannot be replayed from the journal did not happen.

The three laws of this package
==============================

1. **No decision without a record.**  ``monster.journal`` is append-only and
   hash-chained: a line that was edited is a line that fails verification.
   Learning needs a past that cannot be rewritten.
2. **No reading without a null.**  ``monster.brain.phase`` reports coherence
   only beside the distribution of coherence that the same data produces when
   its alignment is destroyed.  ``R = 0.9`` on 4 series is not a crowd; it is
   arithmetic.
3. **No number without its sample.**  ``monster.risk`` prints ``None`` (and
   says *unmeasured*) rather than a VaR from nine observations; the autopsy
   prints ``unmeasurable`` rather than an expectancy from one trade.  This is
   the ``ENRG-AI`` habit, kept deliberately.

What is deliberately absent
===========================

No order router, no venue credentials, no LLM in the execution path.  The Rust
bot remains the hands; this package produces ``Intent`` objects and reads
``Fill`` objects.  A model that can send an order is a model that can be
prompt-injected into sending one.
"""
from __future__ import annotations

__version__ = "0.2.0"

#: Wire/schema version of the contracts in :mod:`monster.contracts` and of the
#: journal lines in :mod:`monster.journal`.  Bumped when a field changes
#: meaning; readers refuse a version they do not understand instead of
#: guessing.
SCHEMA = 1

__all__ = ["SCHEMA", "__version__"]
