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

Daily Bollinger Bands v1 use available closing-price windows. The middle value is the existing SMA
for the same window (it is not persisted as a duplicate metric). The upper/lower bands use
population standard deviation (`ddof=0`), with exact Decimal window and multiplier parameters.
Bandwidth is `(upper - lower) / middle`; Percent B is omitted, with an observable zero-denominator
count, when a flat window makes the band width zero. These descriptive values do not fill gaps,
infer a calendar, emit a signal, or constitute investment advice.

EMA MKT-2 is `market.technical.ema` version `1.0.0`, with the exact Decimal recurrence
`ema_t = alpha * close_t + (1 - alpha) * ema_previous` and `alpha = 2 / (window + 1)`. Each request
uses sorted, unique windows from 2 through 400 (default 20). Its first result is the SMA of the
first in-query window; later results reference only the current close observation and the immediately
previous EMA result. Therefore the stored lineage is linear, point-in-time, and does not repeatedly
embed the whole historical prefix.

RSI Wilder, MACD and ATR use Decimal34 and only the bars selected by the point-in-time query. RSI
uses first-N gain/loss seeds and explicit 50/100/0 denominator cases; true range uses high-low on
the first available bar and then the previous available close; ATR uses a first-N true-range seed.
MACD reuses the existing EMA v1 evidence for its fast and slow legs, then persists its line, signal
and histogram dependencies. They are descriptive metrics, not signals or recommendations.

## Warmup, quality, and source scope

Insufficient history is normal and is recorded as warmup, not as an error. Result quality propagates
with the precedence `SUSPECT`, `PARTIAL`, `DELAYED`, then `VALID`. Coinbase volume describes Coinbase
Exchange only. Alpaca IEX volume and trade count are partial and therefore produce `PARTIAL` results.

Every result stores all direct input observation IDs in chronological order; derived results also
store ordered `input_metric_result_ids`. `known_at` remains in the
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
  --sma-window 20 \
  --bollinger-window 20 \
  --bollinger-multiplier 2 \
  --ema-window 20
```

The command reads local storage only. These are descriptive statistics, not signals, scoring,
predictions, recommendations, or financial advice.
