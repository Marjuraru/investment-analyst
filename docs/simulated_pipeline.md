# Simulated vertical pipeline

This step demonstrates a complete, deterministic, and auditable route without connecting any real
provider. It stores fictitious daily bars for Apple (`AAPL`) and Bitcoin (`BTC-USD`) and processes
them through the existing core models and local storage layer.

## Route and traceability

The route is:

```text
simulated market bar
→ RawRecord
→ six NormalizedObservation values
→ MetricResult
→ DiagnosticResult
→ LocalStorage
→ audit summary
```

Each raw record receives a stable UUID5 derived from its source, asset, and UTC timestamp. Each
observation derives its UUID5 from the raw-record identifier and field name. Metric and diagnostic
identifiers are also deterministic. Repeating the run therefore writes identical documents under the
same identifiers and is idempotent.

The simulation creates exactly two assets, one source, six raw records, 36 observations, two metric
definitions, four metric results, and two diagnostics. Its internal audit verifies every reference,
asset association, timestamp boundary, and expected count after reading the objects back from storage.

## Explicit formulas

The two demonstration metrics use the latest two daily bars for each asset:

```text
simple_return = (current_close / previous_close) - 1
volume_ratio = current_volume / previous_volume
```

The diagnostic scoring rules are:

```text
return_score = clamp(50 + simple_return × 1000, 0, 100)
volume_score = clamp(50 + (volume_ratio - 1) × 25, 0, 100)
final_score = return_score × 0.70 + volume_score × 0.30
```

A final score of at least 60 is labeled `POSITIVE`, a score of at most 40 is labeled `NEGATIVE`, and
other values are labeled `NEUTRAL`. These labels are technical demonstration outputs only. They are
not recommendations or real financial analysis.

## Run locally

Use an explicit storage root:

```bash
python scripts/run_simulated_pipeline.py --root /tmp/investment-analyst-simulation
```

The script does not delete existing data and may be run repeatedly against the same root. It prints a
JSON audit summary containing stable identifiers, counts, and the traceability verification result.

All prices, volumes, trade counts, timestamps, assets, and outcomes in this demonstration are
fictitious. There are still no real provider connectors, network calls, order execution, or investment
advice.
