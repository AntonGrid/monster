# ENRG Monster — the learning layer

The control plane that joins the three repositories of this workspace into one
loop: **ENRG-AI** (research and the court that judges it), **entrain** (the
mathematics of coherence), **ENRG-HFT** (the hands).  This package is the part
none of them had: a place where a decision is *recorded before it is made*, a
reading is *compared with its own null*, and a loss is *attributable to a
number*.

Version 0.2.0 is the **truth core plus its data layer** — still not a trading
robot:

| Component | File | What it refuses |
|---|---|---|
| Contracts | `monster/contracts.py` | a crossed book, a duplicate quote, a reading from the future, an intent whose id does not match its content |
| Journal | `monster/journal.py` | editing the past: every line is `sha256`-chained to the previous one, and a second writer on the same chain |
| Replay | `monster/replay.py` | a state cache that disagrees with the journal: the book is rebuilt and compared field by field, not averaged |
| Coherence | `monster/brain/phase.py` | calling `R = 0.8` a crowd before asking what `R` looks like when the alignment is destroyed |
| Truth check | `monster/brain/truth.py` | trading a stale quote, or a venue that disagrees with the robust cross-section |
| Risk | `monster/risk/limits.py` | a projected book over the limits, a VaR from nine observations, a kill switch cleared by a process |
| Execution | `monster/exec/paper.py` | mid-price fills, a spot short, a position valued at cost, funding booked as zero when it is unknown |
| Carry | `monster/strategies/carry.py` | a funding rate that does not beat four fills and the basis |
| Autopsy | `monster/autopsy.py` | calling a two-trade sample "profitable" instead of "unresolvable" |
| Data layer | `monster/data/` | a blank price read as zero, a gap read as a bar, a body that re-parses into a different quote than the one stored |
| Probe | `monster/data/probe.py` | calling a `200` an answer: the body is parsed, and a refusal is printed as the parser's sentence |
| Cycle | `monster/cycle.py` | acting on a journal that fails verification |

## Quick start

The package uses the same virtualenv as `ENRG-AI` (it imports that repository's
oscillator and resolution gate, so there is one copy of the arithmetic).  It
finds that checkout as a sibling (`../ENRG-AI` or `../../ENRG-AI`); from
anywhere else, point at it with `MONSTER_ENRG_AI=/path/to/ENRG-AI`.  The
`monster.data` commands (`probe`, `capture`, `audit`, `gaps`, `truth-report`, `derive`) import
neither, so they run on a machine that only has this repository.

```bash
cd monster
../ENRG-AI/.venv/bin/python -m pytest          # 194 tests, no network

# 0. One question per endpoint: does the venue answer the shape we parse, or
#    merely answer?  It writes nothing, so it is safe to run first.
../ENRG-AI/.venv/bin/python -m monster.data probe --fixtures data/raw-fixtures

# 1. A frozen snapshot (the 0.1.0 path): every number is in one file.
../ENRG-AI/.venv/bin/python -m monster.cycle \
  --snapshot data/snapshots/example.json \
  --history  data/snapshots/history.json \
  --journal  var/journal.jsonl \
  --state    var/state.json

# 2. The data layer, offline: write a synthetic tape, replay it into a store,
#    audit the store against its own raw bodies, then let the cycle derive its
#    own snapshot and history.  On the VPS only the capture line changes:
#    `--live --interval 60` instead of `--fixtures ... --at ...`.
#    The collector writes its own chain (two writers on one journal break it) and
#    the cycle links that chain's head as a journal.linked event.
../ENRG-AI/.venv/bin/python -m monster.data tape --root var/tape --store var/data
../ENRG-AI/.venv/bin/python -m monster.data capture --fixtures var/tape --store var/data \
  --count 130 --interval 300 --at 1794873600 --journal var/journal.capture.jsonl
../ENRG-AI/.venv/bin/python -m monster.data audit --store var/data
MONSTER_DATA_JOURNAL=var/journal.capture.jsonl \
  ../ENRG-AI/.venv/bin/python -m monster.cycle --store var/data --interval 300 \
  --journal var/journal.jsonl --state var/state.json

../ENRG-AI/.venv/bin/python -m monster.cycle --verify
# ok (27 events) + links: 1 ok — the second line re-reads the capture chain's
# recorded head, so an edit to the data chain after the decision is named too.
../ENRG-AI/.venv/bin/python -m monster.cycle --replay   # is the cache still the journal's book?
```

On the VPS the same code runs against the venues: `probe --live` first, then
`capture --live --store var/data --interval 60 --count 10080 --journal
var/journal.capture.jsonl --fsync` for a week.  The operator runbook — checks,
tmux and systemd units, daily `audit`, what to send back — is
[`docs/VPS.md`](docs/VPS.md).

The example fixture is invented data.  It exists to make the arithmetic
auditable offline, and the snapshot run prints something like:

```
monster 0.2.0 · paper · journal ok
book: equity 999.85 USD, 2 position(s)
truth: 2 checked, 0 flagged | 2 unmeasured | unmeasured: btc-usdt-swap: 1 venue(s) < 3, ...
coherence R=0.788 null=0.441 p=0.095 excess=+0.347 (indistinguishable from its null)
carry: 1/2 accepted at 168h
  btc htx: accepted: funding +105.00 bps - costs 29.71 bps (4 fills + basis 1.71) = +75.29 bps >= 10.00
  eth htx: refused: funding +42.00 bps - costs 53.02 bps (4 fills + basis 25.02) = -11.02 bps < 10.00
  [ok] filled 7f454f3fda552fdc (carry)
```

The store-driven run is the one with a history behind it: four symbols quoted by
HTX, OKX and Bybit, so the truth check is *measured* rather than unmeasured,
and 128 bars of history feed the coherence read:

```
monster 0.2.0 · paper · journal ok
book: equity 999.84 USD, 2 position(s)
truth: 4 symbol(s) agree across venues
coherence R=0.845 null=0.473 p=0.050 excess=+0.372 (coherent)
carry: 1/2 accepted at 168h
  btc htx: accepted: funding +105.00 bps - costs 29.00 bps (4 fills + basis 1.00) = +76.00 bps >= 10.00
  eth htx: refused: funding +21.00 bps - costs 29.00 bps (4 fills + basis 1.00) = -8.00 bps < 10.00
  [ok] filled a58cbe7a9a07ebc8 (carry)
```

That `(coherent)` is a statement about the generator, not the market: the tape's
assets share a sine, so the null correctly separates them — at `p = 0.050`,
which is the boundary, not a discovery.  Shorten the history and the same code
answers `indistinguishable from its null` (0.728 vs 0.461 at 40 bars).  The
reading is the arithmetic being honest in both directions, which is the only
reason to trust it later.

Run the same cycle twice and the second refuses with `ALREADY_OPEN`: a carry is
a position, not a subscription, and the horizon decides when it ends.  The
journal records both cycles and `--verify` walks the 27-line chain — fifteen
lines for the cycle that traded (two fills, one `position.opened`) and twelve for
the one that refused — including the `journal.linked` line that ties the capture
chain to the book.

Note what the runs say honestly: in the frozen snapshot the perp symbols are
quoted by one venue, so their cross-venue truth is **unmeasured** — "I could not
check" is different from "it agrees".  In the store-driven run the same symbols
are measured against two witnesses, and the coherence reading is reported with
the null it was compared to, not instead of it.

## Configuration

Everything is `MONSTER_*`; defaults are safe for a laptop with no keys and no
network (see `.env.example`).  `MONSTER_MODE` accepts `paper` and nothing else —
live trading is not a flag in this package, because the hands are Rust's and a
Python process that can be switched to live by an environment variable is a
process a prompt injection can switch too.

The data layer adds `MONSTER_DATA_PATH` (where the day files live),
`MONSTER_INTERVAL_S` (the capture cadence the history is aligned to),
`MONSTER_TRUTH_VENUES` (who is allowed to witness the trading venue) and the two
freshness windows, `MONSTER_MAX_QUOTE_AGE_S` and `MONSTER_MAX_FUNDING_AGE_H`.
They are vetoes, not preferences: an old quote is not a price, and an old
funding rate is `unmeasured` rather than zero.  The trading venue must be in
`MONSTER_TRUTH_VENUES` — a venue you trade must be a venue you check.

Two more are about integrity rather than arithmetic: `MONSTER_DATA_JOURNAL`
names the collector's chain, so each cycle can link it by hash (unset means "no
capture chain"), and `MONSTER_MAX_SNAPSHOT_AGE_S` (default 180, three capture
intervals) is how old the freshest pass may be before a cycle refuses to call it
"now".  One shared journal for two writers is refused outright at startup.

```bash
MONSTER_CAPITAL_USD=1000 MONSTER_NOTIONAL_USD=100 MONSTER_MIN_EDGE_BPS=10 \
MONSTER_HORIZON_HOURS=168 MONSTER_FEE_BPS=5 MONSTER_SLIPPAGE_BPS=2 \
../ENRG-AI/.venv/bin/python -m monster.cycle --snapshot ...
```

## What the journal contains

One JSON object per line, in this order for a cycle:

`data.derived` (when `--store` supplied the snapshot) → `journal.linked` (when a
capture chain exists) → `cycle.started` → `funding.accrued` → `day.rolled` →
`equity.mark` → `truth.read` → `coherence.read` → `carry.scan` →
`intent.proposed` → `order.filled` / `intent.refused` → `position.opened` →
`trade.closed` → `autopsy.summary` → `cycle.finished`

`data.derived` is the provenance line: how many quotes and funding rates the
snapshot held, which symbols entered the history, how many bars each one had,
which gaps and coverage numbers the store reported.  A decision can then be
read together with the data it saw, not just the price it used.

`data.captured` belongs to the collector's **own** chain, `var/journal.capture.jsonl`:
every pass, including the endpoints that failed and why.  A feed that breaks
leaves a line, the same way a limit that fires does.  Two writers on one journal
interleave `prev`/`hash` and break it, so the split is a rule rather than a
convention — and `Journal.append` refuses a file that grew behind its back,
which turns a silent fork into an error the same minute.  `journal.linked` is
what keeps the split honest: each cycle records the capture chain's head, and
`--verify` re-reads every recorded head so a capture file edited afterwards no
longer matches what the book says it saw.  `deploy/monster-book.{service,timer}`
runs that check plus `--replay` every hour: a red unit is the alert, and there is
no `SuccessExitStatus=` to soften it.

The state cache is checked, not trusted: `--replay` rebuilds the book from the
journal and compares it field by field with `var/state.json`.  A cycle does the
same before deciding anything:

* they agree — trade, nothing is written;
* the cache is gone — it is refilled from the chain and `state.rebuilt` says so;
* they disagree — the cycle refuses (exit 2) and writes `state.diverged` with
  every field that differs, naming both numbers.  It does not average the two
  books and does not pick the convenient one: a phantom position is a
  disagreement nobody asked about.  `--replay --write-state` adopts the
  journal's book once a human has looked.

A `--store` cycle that means "as of now" also refuses a capture pass older than
`MONSTER_MAX_SNAPSHOT_AGE_S` and writes `cycle.stale`: a decision on a collector
that stopped an hour ago is a decision about the past.

Every refusal carries a machine code (`GROSS`, `NET_DELTA`, `SYMBOL`, `VENUE`,
`LEVERAGE`, `NO_CASH`, `DAILY_LOSS`, `KILLED`, `TRUTH_FLAG`, `ALREADY_OPEN`,
`VENUE`) and a sentence.  The refusals are the dataset the learning loop will
read: a limit that fires is evidence about the limits.

`--verify` walks the chain and names the first line that does not belong; the
cycle refuses to trade when the past has been edited.  It also checks the chains
this one *links*: a `journal.linked` event is a claim that "when this cycle ran,
the capture chain had N lines and line N hashed to H", and `--verify` opens that
file, verifies it as a chain, and compares the recorded line — `ok`, `edited`
(rewritten), `truncated` (cut short), `missing` (deleted), `broken` (the linked
chain does not verify) or `unlinked` (the cycle recorded that it could not read
it).  Growth after the link is normal and is not a mismatch: the claim
is about a moment, not about the tail.  `--no-links` checks one chain only, for
the operator who archived the other file on purpose.

## What this version does not do

* no live trading venue, no credentials, no order router (Rust remains the
  hands); the data layer reads public endpoints and nothing else;
* no cross-venue carry (it would need a transfer model the snapshot does not
  carry), no derivatives beyond HTX's USDT-margined swap;
* no learning yet: v0.2 records what learning will need.  Re-fitting thresholds
  is an offline act with pre-registration, not a loop side effect;
* no Parquet/DuckDB: gzip JSON lines are enough for 1–5 minute data over a few
  weeks, and a columnar store can be added behind `Store` without touching the
  parsers.

Next steps and the gates that guard them are in [`docs/MONSTER.md`](docs/MONSTER.md).

## Layout

```
monster/
  monster/
    contracts.py   the wire: Quote, FundingQuote, Snapshot, Leg, Intent, Fill, ClosedTrade
    journal.py     append-only hash chain: Event, Journal, verify()
    replay.py      the journal rebuilt into a book; the cache compared with it
    enrg.py        the one bridge to ENRG-AI (oscillator, resolution gate)
    instruments.py one canonical name per instrument, and each venue's native id
    brain/phase.py Kuramoto order parameter + circular-shift surrogate null
    brain/truth.py stale quotes and cross-venue disagreement (median/MAD)
    risk/limits.py projected limits, historical VaR, kill switch
    exec/paper.py  fees, spread, slippage, margin, funding, close accounting
    strategies/carry.py  funding minus four fills and the basis, or a refusal
    autopsy.py     per-strategy expectancy with ENRG-AI's resolution gate
    cycle.py       the loop and the CLI
    config.py      MONSTER_* environment, validated
    data/
      raw.py       RawPayload, ShapeError, decimal/timestamp rules
      parsers.py   HTX / OKX / Bybit bodies -> contracts, identity-checked
      capture.py   Transport seam: urllib or recorded files; retry, raw-first
      probe.py     one question per endpoint: answered, parsed, or refused
      store.py     gzip day files, dedupe, conflicts, queries
      quality.py   gaps, coverage, audit(raw vs normalized)
      truth_report.py replay the truth check over a day; classify each finding
      derive.py    as-of snapshot, aligned history, notes
      synthetic.py a synthetic tape, labelled as such
  data/raw-fixtures/  venue-shaped bodies for offline parser/capture tests
  data/snapshots/     invented fixtures for the 0.1.0 frozen-snapshot run
  deploy/             systemd units for the capture week (see docs/VPS.md);
                      monster-book.{service,timer} is the hourly verify+replay audit
  docs/VPS.md         the operator runbook: install, probe, run, check, report
  tests/              194 tests: every refusal has one
  docs/MONSTER.md     charter, roadmap, integration plan, non-negotiables
```
