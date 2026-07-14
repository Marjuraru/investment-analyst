# Apple SEC fundamental metrics

This step computes descriptive fundamental ratios from the normalized Apple SEC observations already
stored locally. It does not read SEC raw snapshots, rebuild Company Facts, call the internet, or use
market prices.

## Point-in-time source

The pipeline calls the Apple SEC point-in-time service once with the requested `known_at`. Only
observations whose filing acceptance time is at or before that timestamp can enter a calculation.
The service selects the then-current revision for each field and reporting period. The local
`normalized_at` timestamp remains audit metadata and does not decide public availability.

## Metrics

The implementation calculates exactly five ratios:

- `fundamental.net_margin = net_income / revenue`
- `fundamental.liabilities_to_assets = liabilities / assets`
- `fundamental.liabilities_to_equity = liabilities / stockholders_equity`
- `fundamental.revenue_yoy_growth = current_revenue / previous_revenue - 1`
- `fundamental.net_income_yoy_change_rate =
  (current_net_income - previous_net_income) / abs(previous_net_income)`

A ratio value of `0.20` means one fifth, not the formatted percentage `20%`. Presentation layers may
format ratios later, but the engine stores the unrounded Decimal result.

## Decimal arithmetic

All calculations use `Decimal` inside a local context with precision 34. The engine does not convert
through binary floating point, does not silently round, and does not quantize values for display.

## Same-period and year-over-year calculations

Net margin, liabilities to assets, and liabilities to equity use facts from exactly the same
reporting period. Missing fields are not filled from another period.

Year-over-year metrics join periods through frequency, fiscal year, and fiscal period. Annual `FY`
is compared with the preceding fiscal `FY`; quarterly `Q1`, `Q2`, and `Q3` are compared only with the
same fiscal quarter in the preceding fiscal year. The implementation does not assume a fixed 365-day
calendar and does not create a discrete fourth quarter.

Older observations created before fiscal metadata was preserved remain usable for same-period
ratios. Their year-over-year calculations are skipped explicitly as `missing_fiscal_metadata`; the
engine never invents filing metadata.

## Denominators and incomplete periods

Metrics are calculated independently. A period can therefore produce some metrics while others are
skipped. Revenue, assets, equity, and previous revenue must be positive for the formulas that divide
by them. Previous net income may be positive or negative but cannot be zero because the denominator
uses its absolute value.

The summary records skipped reasons instead of substituting zero, carrying values forward, or
creating an invalid `MetricResult`.

## Traceability and identity

Each persisted result contains the ordered observation IDs and their analytical roles. Its
`available_at` is the latest availability timestamp among its inputs, and its `as_of` is the current
reporting period end.

A UUID5 identity is built from the asset, metric, frequency, current period, formula, algorithm
version, input roles, and input observation IDs. It excludes `known_at`, `computed_at`, and local
normalization time. Consequently:

- repeated runs reuse the same result;
- another query time that selects the same observations reuses the same result;
- an amendment or corrected observation produces a new result only for affected calculations;
- the first persisted `computed_at` is preserved when a result is reused.

All predictable candidate and identity checks finish before the first write. This is logical
prevalidation, not a claim of a database transaction spanning the complete pipeline.

## Running the pipeline

```bash
python scripts/compute_sec_aapl_fundamental_metrics.py \
  --root /tmp/investment-analyst-sec \
  --known-at 2026-07-14T04:41:55Z \
  --frequency quarterly \
  --start 2024-01-01 \
  --end 2026-12-31 \
  --limit 8
```

The script reads no API keys and performs no network requests. It prints a compact summary rather
than SEC documents, record keys, complete observations, or large result-ID lists.

This step does not implement valuation, TTM calculations, a fundamental diagnosis, recommendations,
or order execution.
