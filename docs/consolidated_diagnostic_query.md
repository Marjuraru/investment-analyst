# Consolidated Apple diagnostic query

This read-only layer displays the persisted Apple market diagnostic and persisted Apple
fundamental diagnostic together while keeping their meanings independent. It does not calculate a
combined score, verdict, confidence, quality, ranking, or recommendation.

The command delegates the read-only operation to
`InvestmentAnalystApplication.query_aapl_diagnostics`, which returns the existing versioned report
model for CLI, automation, and future interface adapters.

## Point-in-time selection

A diagnostic is eligible when its source information was available no later than the requested
`known_at`. The service therefore uses `available_at` for point-in-time eligibility. It does not
reject a diagnostic merely because `computed_at` is later than `known_at`: a historical diagnostic
may be reconstructed later from data that was already public. The response exposes this condition
through `computed_after_known_at`.

Only the exact current algorithm version for each mode is eligible. Older or unknown versions remain
stored, are counted as ignored, and are never selected by lexical version ordering. Revisions within
the same mode, algorithm, period, and fundamental frequency are selected by the latest eligible
`available_at`. Equally available revisions are compared through semantic traceability.
Recomputations that differ only in execution identities or calculation times are treated as
equivalent and selected deterministically by the earliest `computed_at`, then the lowest diagnostic
UUID. Equally available revisions with different analytical content are still rejected.

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

The consolidated selector performs one diagnostics list and one metric-results list for Apple and
builds in-memory indexes. Its normal path is approximately `O(D log D + M)`. Only an equally
available revision tie activates lazy semantic resolution from metric results through normalized
observations to raw records. That resolution verifies existing traceability; it does not recalculate
analytics.

The enriched daily formats read each selected `MetricResult` by ID to expose its value, unit,
formula, parameters, timestamps, quality, algorithm, and traceability. The versioned JSON includes
all observation IDs; the human-readable format shows their count to remain scannable. All formats
open the workspace in read-only mode. They never invoke providers, rerun metrics or diagnostics,
execute operations, or persist data.

## Command line

```bash
python scripts/query_aapl_diagnostics.py \
  --root /tmp/investment-analyst \
  --known-at 2026-07-14T04:41:55Z \
  --fundamental-frequency quarterly \
  --format text
```

Exact periods may be requested with `--market-as-of YYYY-MM-DD` and
`--fundamental-as-of YYYY-MM-DD`.

Three output contracts are available:

- `--format json` is the default and preserves the original compact JSON contract. Omitting
  `--format` produces the same output.
- `--format daily-json` returns the versioned
  `aapl-daily-diagnostic-report-v1` contract. It adds resolved metrics, freshness in calendar days,
  explicit limitations, and the existing components and evidence.
- `--format text` renders the same versioned information for daily human review, including formulas,
  weights, contributions, timestamps, and traceability IDs.

`known_at` remains mandatory so that output is reproducible and point-in-time. `reference_age_days`
and `availability_age_days` are calendar-date differences between `known_at` and the selected
diagnostic timestamps; they are descriptive and do not alter either diagnostic.

For the permanent workspace, use its workspace selector instead of constructing storage directly:

```bash
python scripts/query_aapl_diagnostics.py \
  --workspace /home/marjuraru/.local/share/investment-analyst/workspaces/default \
  --known-at 2026-07-16T06:33:09.274921Z \
  --fundamental-frequency quarterly \
  --format daily-json
```

The output is an analytical audit view only. It does not constitute financial advice and does not
execute operations.
