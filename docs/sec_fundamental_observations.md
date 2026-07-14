# Apple SEC fundamental observations

This step converts selected facts from locally stored SEC EDGAR snapshots into typed,
point-in-time `NormalizedObservation` records. It performs no HTTP requests and does not modify the
raw snapshots created by the SEC foundation pipeline.

## Raw snapshots and normalized observations

The Company Facts `RawRecord` preserves the complete parsed SEC snapshot. A normalized observation
contains one selected value, its accounting period, filing availability, unit, quality, source, and
stable lineage identifiers. The observation points to the Company Facts snapshot used to create it.
Its source record key also identifies the matching Submissions snapshot.

Submissions provides filing metadata. Company Facts provides XBRL facts. The normalizer joins them by
SEC accession number and requires `form`, filing date, and report date to agree. It uses
`acceptanceDateTime` as `available_at`; the filing date alone is not precise enough for point-in-time
analysis.

## Explicit concepts

Only these exact `us-gaap` concepts in USD are accepted:

- Revenue from contracts excluding assessed tax.
- Net income or loss.
- Assets.
- Liabilities.
- Stockholders' equity.

Revenue and net income are duration facts. Assets, liabilities, and stockholders' equity are instant
facts. Annual facts must come from an FY 10-K or 10-K/A and span approximately one year. Quarterly
facts must come from Q1, Q2, or Q3 10-Q filings and span approximately one discrete quarter.
Six-month and nine-month year-to-date values, comparative periods, Q4 as an independent quarter, and
alternative tags or units are excluded. The system does not derive quarters by subtracting cumulative
values.

## Amendments, revisions, and idempotence

An amendment has a distinct accession number and therefore produces a distinct observation identity.
A corrected value also produces a new identity. UUID5 excludes the snapshot ID and normalization
clock, so an unchanged fact repeated in a later Company Facts snapshot reuses the existing
observation. Earlier raw snapshots and earlier observation versions remain available for audit.

## Run locally

First store compatible Apple Submissions and Company Facts snapshots with the Step 11 pipeline. Then
run:

```bash
python scripts/normalize_sec_aapl_fundamentals.py --root /path/to/storage-root
```

The command reads only local storage. It does not read `SEC_USER_AGENT`, access the network, print raw
Company Facts documents, calculate ratios, create fundamental diagnostics, execute orders, or provide
financial advice.
