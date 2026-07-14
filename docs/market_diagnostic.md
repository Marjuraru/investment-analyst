# Market diagnostic

The market diagnostic is the first deterministic diagnostic layer built on top of persisted market
statistics. It does not read provider payloads, download data, or recalculate bars and metrics. The
flow remains explicit:

```text
stored observations and raw records
→ persisted MetricResult values
→ point-in-time metric snapshot
→ fixed diagnostic rules
→ persisted DiagnosticResult
```

## Required metrics

A complete snapshot requires the latest common `as_of` timestamp containing:

- daily simple return;
- a short simple moving average;
- a long simple moving average;
- rolling daily volatility; and
- relative volume.

The selector requires the requested asset, source, windows, algorithms, and `known_at` context to
match. Metrics with `available_at` later than `known_at` are excluded. Revisions are selected by the
latest eligible `available_at`; equal-time revisions are rejected as ambiguous rather than resolved
by UUID.

## Directional rules

The trend spread is:

```text
short_sma / long_sma - 1
```

Its score is `clamp(50 + trend_spread × 1000, 0, 100)` and receives a weight of `0.60`.
The latest simple return is scored as `clamp(50 + return × 1000, 0, 100)` and receives a weight of
`0.40`. A final score of at least 60 is `POSITIVE`, a score of at most 40 is `NEGATIVE`, and values
between them are `NEUTRAL`. These labels describe only the published calculation and are not trading
instructions.

## Contextual confidence

Volatility and relative volume do not change the verdict. They only affect contextual confidence:

```text
participation = clamp(relative_volume / 2, 0, 1)
stability = clamp(1 - rolling_volatility / 0.10, 0, 1)
confidence = (participation + stability) / 2
```

The result is capped by data quality: `VALID` 1.00, `DELAYED` 0.90, `PARTIAL` 0.75, and `SUSPECT`
0.50. Confidence is not a probability that a price will move in a particular direction. Alpaca IEX
metrics remain `PARTIAL`, while volume from Coinbase and IEX is specific to each source and must not
be treated as whole-market activity.

## Evidence and traceability

The diagnostic stores two weighted components and five evidence entries. Every evidence entry points
to a persisted `MetricResult`; every metric retains its input observation IDs; every observation
points to a `RawRecord`. The pipeline verifies this chain, the asset and source, the point-in-time
availability, component weights, score aggregation, and storage round-trips.

A UUID5 identity includes the request range, source, `known_at`, windows, selected metric IDs, rule
version, score, confidence, verdict, and quality. `computed_at` is excluded so repeated runs reuse the
same diagnostic and preserve the original computation timestamp. A different point-in-time context or
metric revision creates a different identity.

If no common complete snapshot exists, the pipeline persists an `INSUFFICIENT_DATA` result with zero
score and confidence, no directional components or evidence, and an explicit list of missing
requirements.

## Running the diagnostic

First import historical data and compute the Step 9 statistics. Then run:

```bash
python scripts/compute_market_diagnostic.py \
  --root /path/to/storage \
  --asset-id crypto:btc-usd \
  --source-id coinbase-exchange:btc-usd:daily-candles \
  --start 2026-06-01 \
  --end 2026-07-01 \
  --known-at 2026-07-02T00:00:00Z
```

The command reads only local persisted metrics. It does not read credentials, call providers,
calculate new statistics, execute operations, or provide financial advice. The fixed thresholds are
an initial transparent rule set and have not been calibrated separately by asset class.
