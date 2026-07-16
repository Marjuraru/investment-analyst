# Apple operational runner

`scripts/run_aapl_daily.py` is the single operational entry point for one complete Apple refresh.
It wraps the stable application facade with a per-workspace process lock, atomic latest-run state,
bounded error output, and a read-only health command. It is suitable for manual execution now and
for an external scheduler or local interface later.

The runner is intentionally one-shot. It does not remain resident, choose a trading-calendar date,
install a scheduler, or retry providers indefinitely. The caller supplies the inclusive market
range for every execution. The separate local interface and calendar scheduler are documented in
[`local_interface.md`](local_interface.md).

## Platform and configuration

Run the command with the repository's Python environment inside WSL. The process lock uses the
Linux `fcntl` contract and is not a native Windows-Python entry point.

The run command requires these inherited environment variables:

- `ALPACA_API_KEY`
- `ALPACA_API_SECRET`
- `SEC_USER_AGENT`

The script does not load `.env`. A trusted, local, Git-ignored `.env` can be exported explicitly in
the invoking shell:

```bash
set -a
source .env
set +a
```

Credentials are passed only to the existing provider clients. They are not written to operational
state or included in JSON and error output.

## Execute one complete run

From the repository root:

```bash
.venv/bin/python scripts/run_aapl_daily.py run \
  --market-start 2025-01-01 \
  --market-end 2026-07-15 \
  --fundamental-frequency quarterly
```

The standard workspace precedence applies. Select another initialized or new permanent workspace
with `--workspace PATH`. `--refresh-mode auto` is the default; `--refresh-mode full` requests the
whole market range without deleting history. Dates are inclusive.

By default, both independent diagnostics are required. `--allow-partial` permits the existing
partial consolidated status when only one side can be produced. It does not combine market and
fundamental results or weaken their independent evidence.

Without `--known-at`, the underlying bootstrap chooses one UTC cutoff after ingestion. For a
reproducible retrospective execution, pass an explicit timezone-aware value:

```bash
.venv/bin/python scripts/run_aapl_daily.py run \
  --market-start 2025-01-01 \
  --market-end 2026-07-15 \
  --fundamental-frequency quarterly \
  --known-at 2026-07-16T15:46:09.048264Z
```

Each invocation still queries SEC Submissions and Company Facts. In automatic refresh mode,
already-covered Alpaca market data is skipped while SEC and analytical stages continue. Existing
deterministic records and results are reused, and successful earlier stages remain persisted if a
later stage fails.

## Lock and atomic state

Only one runner may write a workspace at a time. The advisory lock is retained at:

```text
<workspace>/state/aapl_daily_run.lock
```

The file's PID and run metadata are diagnostic only. The held operating-system lock is the
authority; a retained file after process exit does not block the next run.

The latest bounded state is atomically replaced at:

```text
<workspace>/state/aapl_daily_run_state.json
```

Its versioned `aapl-daily-run-state-v1` contract records the request, timestamps, status, compact
counters, point-in-time cutoff, refresh mode, consolidated completeness, and traceability. It never
contains provider documents, observations, headers, credentials, or tracebacks. This file is the
latest operational summary, not the analytical history; append-only evidence remains in workspace
storage.

The lifecycle is:

- `running`: written before the application facade starts;
- `succeeded`: atomically replaces `running` only after the complete result and traceability check;
- `failed`: atomically replaces `running` with a bounded safe category and message.

An abrupt termination can leave `running` after the operating-system lock is released. Health
reports that combination as degraded and explicitly identifies the run as interrupted. The next
run may safely resume through existing idempotent bootstrap behavior; no workspace cleanup or
manual database editing is required.

## Read-only health

Inspect workspace integrity and the latest run without initializing or modifying the workspace:

```bash
.venv/bin/python scripts/run_aapl_daily.py health
```

Use `--workspace PATH` to inspect another workspace. Health statuses are:

- `ready`: workspace is valid and no failed or interrupted latest run exists;
- `running`: valid workspace with a matching process lock currently held;
- `degraded`: incomplete workspace, failed latest run, or interrupted `running` state.

A valid workspace with no operational state remains `ready` and reports that no run has yet been
recorded. This lets existing workspaces adopt the runner without a migration.

## Exit codes

For `run`:

- `0`: completed successfully;
- `1`: unexpected internal failure with sanitized output;
- `2`: missing configuration or an expected validation, provider, workspace, storage, or state
  failure;
- `3`: the strict bootstrap finished without both required independent diagnostics;
- `4`: another process already holds the workspace run lock.

For `health`, `0` means `ready` or actively `running`, `3` means `degraded`, and `2` means health
could not read valid workspace or operational state.

The runner performs descriptive analysis only. It does not use an LLM, execute orders, manage
money, or produce an investment recommendation.
