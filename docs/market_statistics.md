# Historical market statistics

The market-statistics engine is the first real deterministic analytical layer in the project. It
consumes only the provider-independent `MarketBarSeries` produced by the historical market data
service. It does not inspect Coinbase or Alpaca JSON, contact a provider, infer missing bars, or
execute orders.

## Statistics

The daily simple return is `(close_t / close_previous_available_bar) - 1`. “Previous available bar”
means the immediately preceding stored bar selected by the point-in-time query, even when calendar
 dates are separated.

A simple moving average is `sum(closes in the window) / window` and includes the current bar. Rolling
daily volatility is the sample standard deviation of the latest `N` simple returns, using `N - 1`
degrees of freedom and `Decimal.sqrt()`. It is deliberately not annualized; no square-root-of-252 or
square-root-of-365 factor is applied.

Relative volume is `current volume / mean(previous N available volumes)`. The current volume is
excluded from its baseline. When that historical mean is zero, the engine records a skip rather than
emitting an undefined result.

## Warmup, quality, and source scope

Insufficient history is normal and is recorded as warmup, not as an error. Result quality propagates
with the precedence `SUSPECT`, `PARTIAL`, `DELAYED`, then `VALID`. Coinbase volume describes Coinbase
Exchange only. Alpaca IEX volume and trade count are partial and therefore produce `PARTIAL` results.

Every result stores all input observation IDs in chronological order. `known_at` remains in the
parameters, preserving the exact point-in-time information set. Deterministic UUID5 identifiers make
repeated runs idempotent. `computed_at` records when the result was first persisted and is preserved
when that result is reused.

## Run locally

```bash
python scripts/compute_market_statistics.py \
  --root data/local-market \
  --asset-id crypto:btc-usd \
  --source-id coinbase-exchange:btc-usd:daily-candles \
  --start 2026-06-01 \
  --end 2026-07-01 \
  --known-at 2026-07-02T00:00:00Z \
  --sma-window 5 \
  --sma-window 20
```

The command reads local storage only. These are descriptive statistics, not signals, scoring,
predictions, recommendations, or financial advice.
