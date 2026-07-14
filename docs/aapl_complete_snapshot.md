# Apple complete snapshot orchestrator

The Step 17 orchestrator updates only Apple's stored market layer and then queries the two existing
diagnostic modes side by side. It does not duplicate provider, normalization, statistics,
diagnostic, storage, or consolidation logic.

## Ordered stages

1. `AlpacaHistoricalPipeline` imports AAPL daily bars from Alpaca Market Data.
2. `MarketStatisticsPipeline` calculates the existing point-in-time market statistics from local
   normalized observations.
3. `MarketDiagnosticPipeline` creates or reuses the existing market diagnostic.
4. The fundamental refresh stage is explicitly skipped. Existing local point-in-time fundamental
   metrics and diagnostics are not recalculated and SEC is not contacted.
5. `AaplConsolidatedDiagnosticService` selects the eligible market and fundamental diagnostics.

The final status is `complete`, `partial`, or `unavailable`. Market and fundamental scores,
verdicts, confidence values, and qualities remain independent. There is no combined score or
verdict.

## Fixed Apple and IEX scope

This step supports only `equity:us:aapl`, the `AAPL` symbol, daily bars, Alpaca Market Data, and the
`iex` feed. IEX is a single-exchange source and does not represent all consolidated U.S. market
volume. It is used because it is available for this first integration. A future change to SIP must
be explicit and must revise the source identity and traceability assumptions; IEX must never be
presented as equivalent to SIP.

Only the Market Data domain is used. The orchestrator has no account, position, order, paper-trading,
or Trading API behavior.

## Point-in-time behavior

`known_at` controls analytical eligibility through stored `available_at` timestamps. A bar,
metric, or diagnostic with `available_at` after `known_at` is excluded by the existing layers.
`computed_at` may be later than `known_at` during a retrospective reconstruction because it records
when the local algorithm ran, not when the source data became public.

The provider request range is half-open in the existing market stack. `market_end` constrains the
request, but it does not replace the point-in-time checks performed by history, statistics,
diagnostic, and consolidated services.

## Idempotence and resumption

The flow is resumable by stage, not one global transaction. Existing deterministic RawRecords,
observations, MetricResults, and DiagnosticResults are reused by their current pipelines. If a later
stage fails, earlier persisted stages remain. Running the same request again is safe and continues
through the same idempotent layers. The orchestrator does not implement rollback and does not claim
global atomicity.

With `require_complete=false`, `partial` and `unavailable` are valid results. With
`require_complete=true`, the pipeline completes its checks and then reports an error when both
sections are not available. Already completed market stages remain available for a safe retry.

## Credentials and execution

The script reads the same variables as the existing Alpaca importer:

```text
ALPACA_API_KEY
ALPACA_API_SECRET
```

They must already exist in the process environment. The script does not load `.env`, print headers,
store credentials, or include secrets in its output.

```bash
python scripts/run_aapl_complete_snapshot.py \
  --root /tmp/investment-analyst-aapl \
  --known-at 2026-07-14T23:59:00Z \
  --market-start 2026-06-01 \
  --market-end 2026-07-01 \
  --fundamental-frequency quarterly
```

Optional exact reference dates are available through `--market-as-of` and
`--fundamental-as-of`. Add `--require-complete` when a missing diagnostic section must make the
command fail.

The output is descriptive analysis, not financial advice. It does not recommend buying, selling,
or holding and does not execute operations.
