# Apple workspace bootstrap

The Apple workspace bootstrap initializes and updates one persistent workspace with the
existing SEC EDGAR and Alpaca Market Data pipelines. It is an application orchestrator:
it does not duplicate provider parsing, normalization, financial formulas, scoring rules,
or persistence behavior.

## Stage order

A run uses one read-write `LocalStorage` context and executes these stages once:

1. Fetch SEC Submissions and Company Facts.
2. Normalize the five supported SEC fundamental facts.
3. Plan missing Apple IEX coverage at the requested range edges.
4. Fetch each planned Alpaca IEX interval, or mark market fetch as skipped.
5. Resolve the point-in-time cutoff.
6. Calculate fundamental metrics.
7. Calculate the fundamental diagnostic.
8. Calculate market statistics.
9. Calculate the market diagnostic.
10. Query the consolidated view.

Market and fundamental diagnostics remain separate. The consolidated view does not add a
combined score, verdict, confidence, quality, recommendation, or ranking.

## Incremental market refresh

The default `--refresh-mode auto` inspects only persisted AAPL IEX daily-bar observations.
It plans at most two user-facing inclusive calendar-date intervals:

- `initial`: no persisted Apple IEX coverage exists, so the complete requested range is fetched.
- `incremental`: only the suffix after the latest persisted market date is fetched.
- `already_current`: the requested range is inside persisted coverage and Alpaca is not called.
- `backfill`: the missing prefix is fetched; a missing suffix may be a second ordered interval.
- `full`: `--refresh-mode full` requests the complete range again without deleting data.

Public intervals remain inclusive. Before each Alpaca call, the existing neutral helper converts
an interval such as `2026-01-01` through `2026-01-09` into the half-open UTC range
`[2026-01-01T00:00:00Z, 2026-01-10T00:00:00Z)`.

Planning detects only a missing prefix, a missing suffix, or complete edge coverage. It does not
infer internal gaps from weekends, exchange holidays, or missing calendar days. Session-calendar
gap detection remains outside this step.

SEC Submissions and Company Facts are fetched on every run because new filings or revisions can
appear independently of local market coverage. Fundamental and market analytics also continue to
run so interrupted or version-updated results can be created or safely reused.

If two Alpaca intervals are planned, they run in order through the same open storage connection.
If the second interval fails, data from the first remains persisted and the next run plans only the
remaining uncovered edge. There is no global transaction and no manual rollback.

## Persistent workspace

The command accepts an optional `--workspace`. Without it, `WorkspaceService` applies the
standard workspace precedence and uses the permanent default location. The command does not
accept a legacy `--root` and never defaults to `/tmp`.

The workspace is initialized idempotently and one writer remains open for the complete run.
Re-running the command safely reuses deterministic RawRecord, observation, MetricResult, and
DiagnosticResult identities. A full refresh does not overwrite or renumber existing identities.

## Point-in-time cutoff

With no `--known-at`, the bootstrap completes SEC ingestion and every required Alpaca interval
first, then captures one UTC timestamp. In `already_current`, the timestamp is captured after SEC
and the skipped market-fetch stage. That single `effective_known_at` is propagated unchanged to
both metric pipelines, both diagnostic pipelines, and the consolidated query.

With an explicit `--known-at`, the requested timestamp is normalized to UTC and is never changed
silently. After ingestion, the bootstrap verifies that the required market and fundamental
observations satisfy `available_at <= known_at`. If the cutoff is too early, the run stops before
metrics and diagnostics, reports the minimum required timestamp, and keeps ingested records so the
run can be resumed.

`available_at` controls historical eligibility. `computed_at` records when local analysis was
executed and may be later than `known_at` in a valid retrospective reconstruction.

## Credentials and sources

The command uses the existing environment variables:

- `ALPACA_API_KEY`
- `ALPACA_API_SECRET`
- `SEC_USER_AGENT`

It does not read `.env`, persist credentials, or print secrets and authorization headers. Only SEC
EDGAR data APIs and Alpaca Market Data are used. No Trading API is imported or called.

Alpaca data uses the fixed IEX daily-bar feed with adjustment `all`. IEX is a single-exchange source
and is not equivalent to consolidated SIP coverage. Existing pagination remains unchanged.

## Running the bootstrap

```bash
python scripts/bootstrap_aapl_workspace.py \
  --market-start 2025-01-01 \
  --market-end 2026-01-01 \
  --fundamental-frequency quarterly
```

Request the complete market interval again without deleting persisted data:

```bash
python scripts/bootstrap_aapl_workspace.py \
  --market-start 2025-01-01 \
  --market-end 2026-01-01 \
  --fundamental-frequency quarterly \
  --refresh-mode full
```

Use another permanent workspace:

```bash
python scripts/bootstrap_aapl_workspace.py \
  --workspace "$HOME/investment-data/apple" \
  --market-start 2025-01-01 \
  --market-end 2026-01-01 \
  --fundamental-frequency annual \
  --known-at 2026-07-14T18:00:00Z \
  --require-complete
```

The JSON output contains one top-level `refresh_plan`, compact stage counters, and one consolidated
view. It excludes complete RawRecords, observations, metrics, Company Facts documents, HTTP
responses, headers, record keys, and credentials.

The results are descriptive, do not constitute financial advice, and never execute operations.
