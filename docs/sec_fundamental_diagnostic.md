# Apple SEC fundamental diagnostic

This step creates a descriptive fundamental diagnostic for Apple from the five persisted SEC
fundamental metrics produced in Step 14. It is independent from the market diagnostic and does not
read raw SEC documents, normalized observations, prices, or external services.

## Point-in-time selection

The selector reads the metric repository once, keeps only Apple annual or quarterly fundamental
metrics with `available_at <= known_at`, validates their formulas, algorithm versions, input roles,
and deterministic identities, then resolves revisions by metric name, frequency, and reporting
period. The most recent eligible revision is selected. Equal-time contradictory revisions are
rejected rather than resolved by UUID or storage order.

Without `--as-of`, the newest period containing at least one eligible metric is used. With
`--as-of`, exactly that period is required. Values are never carried from another reporting period.

## Published scores

All arithmetic uses `Decimal` with precision 34.

- Net margin: 0 at 0.00 or below, 100 at 0.25 or above, linear between them.
- Liabilities/assets: 100 at 0.40 or below, 0 at 0.90 or above, inverse linear between them.
- Liabilities/equity: 100 at 0.50 or below, 0 at 5.00 or above, inverse linear between them.
- Revenue YoY growth: 0 at -0.10 or below, 100 at 0.15 or above, linear between them.
- Net-income YoY change rate: 0 at -0.50 or below, 100 at 0.50 or above, linear between them.

These are transparent heuristics for this algorithm version, not accounting standards or
probabilities.

## Weights, coverage, and missing metrics

The base weights are 0.30, 0.25, 0.15, 0.15, and 0.15 in the metric order above. Available weights
are summed as coverage. A scored diagnostic requires net margin, liabilities/assets, and coverage of
at least 0.60. When sufficient, available weights are renormalized to one. Missing metrics are not
assigned artificial neutral or zero scores.

When minimum requirements are not met, the existing `INSUFFICIENT_DATA` verdict is used with zero
score and confidence, no components, and no evidence.

## Verdict, evidence, confidence, and quality

A final score of at least 65 is positive, a score from 40 up to 65 is neutral, and a score below 40
is negative. Each selected metric creates one component and one evidence item. Evidence contribution
is `(component_score - 50) / 50`.

Confidence is `coverage × recency_factor`. Quarterly recency is 1.00 through 150 days and declines
linearly to 0.50 at 365 days. Annual recency is 1.00 through 400 days and declines linearly to 0.50 at
800 days. Confidence summarizes contextual coverage and freshness; it is not a probability.

Quality is `VALID` only with full coverage and recency of at least 0.75. Otherwise a sufficient
result is `PARTIAL`. Insufficient diagnostics also use `PARTIAL`, because the core quality enum does
not add a separate insufficient value.

## Auditability and persistence

The diagnostic stores components and evidence linked to selected `MetricResult` IDs. Its UUID5 uses
Apple, fundamental mode, frequency, target period, algorithm version, and selected metric IDs. It
does not use `known_at`, `computed_at`, current time, or storage order. Identical inputs therefore
reuse the same diagnostic; a revised metric or algorithm version creates a new diagnostic.

The pipeline validates the rules, score, inputs, timestamps, traceability, identity, and existing
conflicts before the first write. Weighted sums use a Decimal tolerance of 0.0001. The process does
not claim database transaction atomicity, but predictable validation failures occur before
persistence.

## Command

```bash
python scripts/compute_sec_aapl_fundamental_diagnostic.py \
  --root /tmp/investment-analyst-sec \
  --known-at 2026-07-14T04:41:55Z \
  --frequency quarterly
```

Use `--as-of YYYY-MM-DD` to require one exact reporting period. The output is compact and omits raw
records, observations, record keys, and complete metric documents.

The diagnostic is descriptive, uses fixed uncalibrated heuristics, is not a recommendation, and does
not execute operations. A unified market-and-fundamental diagnostic is not implemented yet.
