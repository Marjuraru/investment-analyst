# Apple workspace bootstrap

The Apple workspace bootstrap initializes and updates one persistent workspace with the
existing SEC EDGAR and Alpaca Market Data pipelines. It is an application orchestrator:
it does not duplicate provider parsing, normalization, financial formulas, scoring rules,
or persistence behavior.

## Stage order

A run uses one read-write `LocalStorage` context and executes these stages once:

1. Fetch SEC Submissions and Company Facts.
2. Normalize the five supported SEC fundamental facts.
3. Fetch AAPL daily bars from Alpaca IEX.
4. Resolve the point-in-time cutoff.
5. Calculate fundamental metrics.
6. Calculate the fundamental diagnostic.
7. Calculate market statistics.
8. Calculate the market diagnostic.
9. Query the consolidated view.

Market and fundamental diagnostics remain separate. The consolidated view does not add a
combined score, verdict, confidence, quality, recommendation, or ranking.

## Persistent workspace

The command accepts an optional `--workspace`. Without it, `WorkspaceService` applies the
standard workspace precedence and uses the permanent default location. The command does
not accept a legacy `--root` and never defaults to `/tmp`.

The workspace is initialized idempotently and one writer remains open for the complete
run. Data from completed stages is retained if a later stage fails. There is no global
transaction and no manual rollback. Re-running the command safely reuses deterministic
RawRecord, observation, MetricResult, and DiagnosticResult identities.

## Point-in-time cutoff

With no `--known-at`, the bootstrap completes SEC and Alpaca ingestion first, then captures
one UTC timestamp. That single `effective_known_at` is propagated unchanged to both metric
pipelines, both diagnostic pipelines, and the consolidated query.

With an explicit `--known-at`, the requested timestamp is normalized to UTC and is never
changed silently. After ingestion, the bootstrap verifies that the required market and
fundamental observations satisfy `available_at <= known_at`. If the cutoff is too early,
the run stops before metrics and diagnostics, reports the minimum required timestamp, and
keeps the ingested records so the run can be resumed.

`available_at` controls historical eligibility. `computed_at` records when local analysis
was executed and may be later than `known_at` in a valid retrospective reconstruction.

## Credentials and sources

The command uses the existing environment variables:

- `ALPACA_API_KEY`
- `ALPACA_API_SECRET`
- `SEC_USER_AGENT`

It does not read `.env`, persist credentials, or print secrets and authorization headers.
Only SEC EDGAR data APIs and Alpaca Market Data are used. No Trading API is imported or
called.

Alpaca data uses the fixed IEX daily-bar feed. IEX is a single-exchange source and is not
equivalent to consolidated SIP coverage.

## Running the bootstrap

```bash
python scripts/bootstrap_aapl_workspace.py \
  --market-start 2025-01-01 \
  --market-end 2026-01-01 \
  --fundamental-frequency quarterly
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

The JSON output is compact. It contains stage counters and one consolidated view, but not
complete RawRecords, observations, metrics, Company Facts documents, HTTP responses,
headers, record keys, or credentials.

The results are descriptive, do not constitute financial advice, and never execute
operations.
