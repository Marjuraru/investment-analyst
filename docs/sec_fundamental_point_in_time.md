# SEC fundamental point-in-time queries

This read-only service turns previously normalized observations for one configured SEC issuer into
an auditable view of what was publicly available at a requested instant. It does not read raw SEC
snapshots, reconstruct Company Facts, contact the internet, persist new data, or calculate ratios
and diagnostics.

## Point-in-time semantics

Selection uses `available_at`, which is the SEC filing `acceptanceDateTime` captured during
normalization. The comparison is inclusive: an observation is eligible when
`available_at <= known_at`. `normalized_at` remains audit metadata and may be later than `known_at`;
it does not determine public availability. This models official filing availability, not exact local
network latency.

For each field, frequency, and period end, the service selects the revision with the latest
`available_at` that does not exceed `known_at`. Amendments therefore replace earlier filings
naturally once their acceptance time is reached. Equal-time duplicates collapse only when their
fundamental content is identical. Contradictory equal-time revisions are rejected rather than being
resolved by UUID, storage order, raw-record ID, or normalization time.

## Period views

The service supports annual and quarterly observations for exactly five fields: revenue, net income,
assets, liabilities, and stockholders' equity. Facts are grouped only by their own period end. Values
are never carried from another period, so a partial period remains partial and reports its missing
fields explicitly. Results can be restricted to an inclusive period range and a limit retains the
most recent periods before restoring chronological output order.

Each service instance is limited to its configured asset, SEC Company Facts source, transformation
version, USD, exact XBRL tags selected in the normalizer, and observations marked `VALID`. A request
for another asset fails instead of falling back to Apple. Filing form, fiscal year and fiscal period
come only from the canonical audit key and are never inferred from dates.

## Performance and auditability

The observation repository is read once for the requested issuer. Revision grouping and sorting are at most
approximately O(N log N). The service never calls `raw_records.get`, `raw_records.list`, metrics,
diagnostics, provider clients, or the SEC normalizer. Each selected fact retains its observation ID,
raw-record ID, source, accession number, XBRL tag, period, availability time, and canonical record
key.

## Command line

```bash
python scripts/query_sec_aapl_fundamentals.py \
  --root /tmp/investment-analyst-statistics \
  --known-at 2026-07-14T04:41:55Z \
  --frequency quarterly \
  --limit 8
```

Optional `--start` and `--end` dates are inclusive. The command is a local analytical query only. It
does not execute operations and does not constitute financial advice.
