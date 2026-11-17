# Recorded venue answers (fixtures)

These files are **hand-written to the shape each venue documents**, not captured
from the network: this checkout has no guaranteed access to the exchanges, and a
parser test that needs the internet is a test that fails for the wrong reason.
Values were chosen to exercise the arithmetic — three venues agreeing within a
couple of basis points, a BTC carry that clears the threshold, an ETH perp cheap
enough to make the carry negative.

| File | Venue / endpoint | Shape |
|---|---|---|
| `htx/spot-btc.json` | `api.huobi.pro/market/detail/merged` | `status/ch/ts/tick.bid[0]/tick.ask[0]`, ms |
| `htx/perp-btc.json` | `api.hbdm.com/linear-swap-ex/market/detail/merged` | same, `ch` carries `BTC-USDT` |
| `htx/funding-btc.json` | `linear-swap-api/v1/swap_funding_rate` | `data.funding_rate`, `settle_period` |
| `okx/*.json` | `/api/v5/market/ticker`, `/api/v5/public/funding-rate` | `data[0]`, every decimal a string |
| `bybit/*.json` | `/v5/market/tickers` | `retCode` envelope, `result.list[0]`, strings |

`bybit/funding-*.json` is byte-identical to `bybit/perp-*.json` because Bybit
publishes the funding rate inside the ticker; the capturer fetches that ticker
once and reads it twice.

## Replacing them with real recordings

On the VPS, record a day with the live transport and copy the store or the
answers here:

```bash
python -m monster.data capture --live --store var/data --interval 60   # keeps raw bodies too
python -m monster.data audit --store var/data                          # raw -> normalized, per day
```

The store keeps the raw body of every request, so a fixture can be produced
from it later (`store.raws(day)` → `RawPayload.to_dict()["body"]`) without
asking the venue again.  A parser that only passes against hand-written shapes
is not proven; the audit is what proves it against what the venue actually sent.
