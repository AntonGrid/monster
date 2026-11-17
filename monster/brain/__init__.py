"""The brain: coherence with its null, and the truth check on the data itself.

Two readings, both deliberately modest:

* :mod:`monster.brain.phase` answers "is this cross-section a crowd, beyond what
  a shuffled version of the same series would look like";
* :mod:`monster.brain.truth` answers "may I believe this quote at all".

Neither returns an order.  Their answers come in three flavours, and the cycle
treats them differently on purpose:

* a **finding** (a stale quote, a venue far from the median) vetoes the leg it
  names — `TRUTH_FLAG` in the journal;
* an **unmeasured** symbol (fewer than three venues) does not veto, because a
  perp quoted by one venue is the normal case and a strategy that refuses it
  never trades.  It is recorded and counted, and Phase 1 sets a ceiling on how
  often it may happen before the data layer is considered unfinished;
* a coherence reading whose ``p`` is not small is reported as
  "indistinguishable from its null" and changes nothing.

The distinction matters: "I checked and it is wrong" and "I could not check"
are different sentences, and a system that conflates them will either trade on
lies or never trade at all.
"""
from monster.brain.phase import CoherenceReading, phase_coherence
from monster.brain.truth import Finding, TruthReport, cross_venue_truth

__all__ = [
    "CoherenceReading",
    "phase_coherence",
    "Finding",
    "TruthReport",
    "cross_venue_truth",
]
