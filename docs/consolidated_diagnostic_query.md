# Consolidated Apple diagnostic query

This read-only layer displays the persisted Apple market diagnostic and persisted Apple
fundamental diagnostic together while keeping their meanings independent. It does not calculate a
combined score, verdict, confidence, quality, ranking, or recommendation.

## Point-in-time selection

A diagnostic is eligible when its source information was available no later than the requested
`known_at`. The service therefore uses `available_at` for point-in-time eligibility. It does not
reject a diagnostic merely because `computed_at` is later than `known_at`: a historical diagnostic
may be reconstructed later from data that was already public. The response exposes this condition
through `computed_after_known_at`.

Only the exact current algorithm version for each mode is eligible. Older or unknown versions remain
stored, are counted as ignored, and are never selected by lexical version ordering. Revisions within
the same mode, algorithm, period, and fundamental frequency are selected by the latest eligible
`available_at`. Equally available contradictory revisions are rejected rather than broken by UUID,
score, or repository order.

## Separate modes and periods

The market and fundamental sections retain their own score, verdict, confidence, quality, period,
algorithm version, components, and evidence. Fundamental frequency is determined from the referenced
`MetricResult` documents, not from free-text summaries. Annual and quarterly metrics are never mixed.
An optional exact period may be requested for either mode; the service does not silently substitute
an earlier period.

The temporal context reports the absolute day difference between the two selected reference dates.
That gap is descriptive only and never changes either diagnostic.

## Result status

- `complete`: both independent diagnostics are available.
- `partial`: exactly one diagnostic is available.
- `unavailable`: neither diagnostic is available.

Partial and unavailable results are valid query outcomes.

## Read-only behavior and performance

The service performs one diagnostics read and one metric-results read for Apple, builds in-memory
indexes, and performs no repository calls inside diagnostic loops. Its target complexity is
approximately `O(D log D + M)`. It never accesses RawRecords or normalized observations, invokes
providers, reruns metrics or diagnostic engines, or persists data.

## Command line

```bash
python scripts/query_aapl_diagnostics.py \
  --root /tmp/investment-analyst \
  --known-at 2026-07-14T04:41:55Z \
  --fundamental-frequency quarterly
```

Exact periods may be requested with `--market-as-of YYYY-MM-DD` and
`--fundamental-as-of YYYY-MM-DD`.

The output is an analytical audit view only. It does not constitute financial advice and does not
execute operations.
