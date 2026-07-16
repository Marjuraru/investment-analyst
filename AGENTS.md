# Repository guidance for Codex

## Scope and communication

- These instructions apply to the entire repository.
- Respond to the user in Spanish. Be direct, precise, technical when useful, and explicit about uncertainty.
- Treat the repository and its current working tree as the only authoritative source for code, contracts, versions, and behavior. Do not implement from memory, stale snippets, old blobs, or previously generated patches.
- When the user must run something, provide exact commands and briefly state the expected result. Do not ask them to repeat commands unnecessarily.
- Do not promise background work. Report only checks and executions that actually completed.

## Product mission and boundaries

`investment-analyst` is a local investment-analysis tool intended to support a human analyst. It collects, normalizes, relates, calculates, and explains investment information with transparent evidence.

The product must remain:

- transparent, auditable, deterministic, reproducible, and point-in-time;
- based on official or explicitly identified data sources;
- local-first, with persistent storage and traceable inputs;
- independent of providers at the analytical core;
- descriptive rather than prescriptive.

The product must not:

- buy, sell, place orders, or use a Trading API;
- manage money or execute automated trading;
- produce personalized financial advice or direct investment recommendations;
- hide formulas, weights, inputs, limitations, or evidence;
- combine market and fundamental analysis into an arbitrary aggregate score, verdict, confidence, quality, recommendation, or ranking;
- require an active LLM or token consumption during normal product execution.

Market and fundamental diagnostics remain independent. A consolidated query may present both, but must not merge their analytical meaning.

## Current canonical data scope

### Apple market data

- Asset ID: `equity:us:aapl`.
- Source ID: `alpaca-market-data:iex:aapl:daily-bars:adjustment-all`.
- Provider: Alpaca Market Data, not Alpaca Trading.
- Feed: `iex`.
- Bars: daily AAPL bars.
- Adjustment: `all`.
- IEX is one-exchange coverage, not consolidated SIP coverage. Preserve this limitation in data quality, documentation, and explanations.

### Apple fundamentals

- Provider: SEC EDGAR official data from `data.sec.gov`.
- Sources: Submissions and Company Facts.
- A compliant, non-secret SEC `User-Agent` is mandatory.
- SEC must be queried on every bootstrap because filings or revisions can appear independently of local market coverage. Do not skip SEC solely because prior data exists.

Credentials and identifying configuration belong in environment variables or approved local configuration. Never commit or print secrets.

## Architectural invariants

- Use typed domain models. Do not use `Any` in production code.
- Keep Pydantic models strict, normally immutable where appropriate, and configured with `extra="forbid"`.
- Use timezone-aware timestamps normalized to UTC.
- Preserve exact `Decimal` semantics for financial values; reject floats where the existing contracts do.
- Keep IDs deterministic and preserve stable identity rules.
- Maintain explicit separation between `RawRecord`, `NormalizedObservation`, `MetricResult`, and `DiagnosticResult`.
- Maintain separation between market and fundamental pipelines.
- Preserve point-in-time behavior using `available_at`; `computed_at` is not a substitute for availability.
- A later reconstruction may have `computed_at > known_at` if it uses only inputs available at the requested cut.
- Use one writer connection per execution and preserve already-persisted progress if a later stage fails. Do not introduce a global rollback that removes successful earlier stages.
- Preserve idempotence, append-only history, auditability, and traceability.
- Use the central asset/provider catalog and `ApplicationRuntime`; do not reintroduce scattered provider configuration or storage construction.
- Application behavior must not depend on the current working directory.

Do not change without an explicit, justified task requirement:

- storage schema or workspace format;
- asset IDs or source IDs;
- deterministic ID algorithms;
- algorithm versions;
- formulas, weights, diagnostic rules, thresholds, or evidence semantics;
- catalog entries, provider endpoints, or feed/adjustment contracts;
- public CLI arguments, output contracts, or stage names.

If a requested change requires one of these, identify the compatibility and migration impact before editing.

## Time-interval and refresh semantics

- Public date ranges are inclusive. Convert them internally to half-open UTC intervals without losing the final requested date.
- Example: public `2026-07-10` through `2026-07-13` maps to `[2026-07-10T00:00:00Z, 2026-07-14T00:00:00Z)`.
- Do not revert the inclusive-final-date correction in commit `2b8e64b`.
- Incremental refresh planning supports `initial`, `incremental`, `already_current`, `backfill`, and `full`.
- Automatic planning detects only missing range edges: a prefix, a suffix, or both. Do not infer internal gaps from weekends, exchange holidays, or absent calendar days without an explicit exchange-calendar design.
- `already_current` skips Alpaca market fetching but still runs SEC and the required analytical/consolidated stages.
- `full` requests the complete range without deleting data or duplicating deterministic identities.

Empty completed Alpaca intervals are valid evidence:

- Accept valid empty-bar responses represented by `bars: []` or `bars: null` under the provider contract.
- Persist the deterministic `alpaca-market-fetch-receipt-v1` only after all pages for an interval complete successfully.
- A receipt is not a bar, does not create observations, and must not affect metrics, diagnostics, or `market_as_of`.
- If any page fails, do not create a receipt claiming complete interval coverage.
- Do not delete or rewrite existing receipts or historical analytical results to make a rerun pass.

## Workspace and persistence safety

- The permanent workspace contains valuable local history. Never delete, recreate, truncate, manually edit, or bulk-clean it unless the user explicitly authorizes a narrowly defined operation.
- Use `WorkspaceService`, `ApplicationRuntime`, and repository abstractions instead of direct ad hoc database or filesystem manipulation.
- Use read-only inspection for diagnostics whenever writes are not required.
- Do not manually remove raw records, observations, metrics, diagnostics, manifests, Parquet data, or database rows.
- Never expose credentials or raw secret values in logs, JSON output, errors, tests, or chat responses.

## Working-tree and Git safety

The working tree may contain important unfinished user work.

Before editing, inspect at minimum:

```bash
git rev-parse HEAD
git status --short
git diff --check
GIT_PAGER=cat git diff --stat
```

Then follow these rules:

- Preserve all pre-existing tracked, intent-to-add, staged, unstaged, and untracked work unless the user explicitly instructs otherwise.
- Do not use `git reset --hard`, `git checkout .`, `git restore .`, destructive clean commands, or equivalent operations.
- Do not stage, commit, amend, rebase, push, or create a pull request unless the user explicitly requests that action.
- Never commit before the required tests and a real execution have been validated when the task requires real-provider behavior.
- Use `GIT_PAGER=cat` or `git --no-pager` so commands never enter `less` during an automated workflow.
- Do not generate or apply a large patch from stale context. Prefer direct, narrow edits against the files currently on disk.
- If a patch is explicitly required, run `git apply --check` first, apply it once, and never retry with `--3way` or `--reject` without new user direction.

## Development environment

- Primary environment: Windows, WSL2, Ubuntu 24.04, Python 3.12, VS Code, Git, Ruff, and Pytest.
- Repository path is normally `~/projects/investment-analyst`.
- The virtual environment is `.venv`.
- Prefer explicit executables such as `.venv/bin/python`, `.venv/bin/ruff`, and `.venv/bin/pytest` in automated commands so behavior does not depend on shell activation.
- Do not add or upgrade dependencies unless the task requires it and the user authorizes the scope.

## Implementation workflow

1. Inspect the working tree and the exact affected models, repositories, services, tests, and documentation.
2. Search for existing contracts and helpers before designing a new abstraction.
3. Make the smallest cohesive change that satisfies the requested behavior while preserving public contracts and historical data.
4. Add or update focused tests for success, failure, point-in-time, idempotence, deterministic identity, and traceability as applicable.
5. Run focused checks during development, followed by repository-wide validation when the change is complete.
6. Review the final diff for accidental scope expansion, secret exposure, semantic changes, and unrelated formatting churn.
7. Leave changes uncommitted unless the user explicitly asks for a commit after validation.

Do not import test helpers from another `test_*.py` module. Use a local helper, an explicit support module, or an appropriate shared fixture.

## Validation and definition of done

Use the commands appropriate to the changed files, then complete the full checks required by the task. The standard final validation is:

```bash
.venv/bin/ruff check <modified-python-files> --fix
.venv/bin/ruff format <modified-python-files>
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
git diff --check
git status --short
```

- Do not claim a command passed unless it was executed successfully in the current working tree.
- If Pytest or a command fails because of the execution environment, separate that failure from a product failure and rerun with a justified diagnostic adjustment.
- Do not parse stdout as JSON after a nonzero exit until confirming that output exists and is valid.
- For real bootstrap validation, preserve the external workspace and record the exact `effective_known_at`, refresh plan, stage statuses, counts, traceability, and idempotence result.
- “Done” means requested behavior is implemented, relevant failure modes remain protected, focused and full validations pass as required, the real workflow passes when required, documentation matches behavior, and remaining risks are explicit.

## Review guidelines

- Treat silent changes to point-in-time selection, deterministic identity, provider scope, units, timestamps, formulas, weights, or evidence as high-risk.
- Confirm that semantically different revisions still fail rather than being selected arbitrarily.
- Confirm that equivalent recomputations do not lose persisted history or traceability.
- Confirm that market and fundamental data never mix across assets, sources, modes, frequencies, or requested cuts.
- Confirm that error messages and outputs do not leak credentials and remain compact enough for CLI use.
- Confirm that tests use different clocks when validating distinct executions; do not hide time-dependent bugs by reusing one clock.

## Final response expectations

After implementation work, summarize only what is useful for review:

- files changed;
- behavior and invariant implemented;
- tests added or changed;
- exact Ruff and Pytest results;
- real-execution result when applicable;
- remaining risks or work not performed;
- Git status, including that no commit was made unless one was explicitly requested.

Do not paste a giant unified diff into the chat.
