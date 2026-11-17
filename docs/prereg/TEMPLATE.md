# Pre-registration template

Copy this file to `docs/prereg/YYYY-MM-DD-<slug>.md` **before** running the test
it describes.  A hypothesis that is written after the result is not a
hypothesis, it is a description of the result.

The fields are the ones `ENRG-AI/agent/backtest/criteria.py` and `power.py`
already demand; the point of writing them down here is that changing one later
is visible in `git log`, not in an argument.

---

## Hypothesis

One sentence.  What is claimed about the world, and in which direction.

## Where it came from

The journal events / autopsy numbers that suggested it.  A hypothesis that came
from a chart on a screen is allowed, but it must say so.

## Data

- venue(s) and symbol(s):
- window (from → to), and how it was frozen:
- what could not be bought/observed (L2, funding history, transfer times):

## Metric and decision rule

- metric: (e.g. mean net bps after fees and funding)
- minimum sample: n = ...
- alpha = 0.05, power = 0.9 (or say why not)
- pass: `mean > minimum_edge_bps(n, sigma)` and sign stable across halves
- fail: anything else, including "unresolvable" — which is a fail of the *test*,
  not of the strategy; it means "run longer or wider", never "tune the threshold"

## Costs charged

- fee bps per fill (and how many fills the trade needs):
- slippage assumption:
- funding assumption, including its period:
- what happens if funding is unknown: unmeasured, not zero

## What would change my mind

The observation that would make this hypothesis wrong, stated before seeing it.

## Verdict

Filled in after the test, with the command that produced it and the journal
file.  Never edited afterwards; a correction is a new file.
