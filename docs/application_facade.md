# Application facade

`InvestmentAnalystApplication` is the stable programmatic entry point for the local application.
It lets later runners and user interfaces invoke supported operations without importing provider,
storage, or analytical pipeline implementations.

The facade exposes the following operation groups:

- `bootstrap_aapl_workspace(...)` initializes or reuses one workspace and executes the complete
  resumable SEC EDGAR and Alpaca Market Data pipeline with one writer connection.
- `query_aapl_diagnostics(...)` opens existing storage in read-only mode and returns the versioned
  `AaplDailyDiagnosticReport` from persisted evidence.
- read-only Apple fundamental and market-chart queries plus the bounded BTC-USD chart query;
- `refresh_btc_market(...)` opens one existing workspace with a single writer, plans missing
  Coinbase daily-candle edges, imports append-only evidence, and persists independent market
  statistics and a diagnostic without invoking SEC or Alpaca.

The existing CLI scripts are adapters over these methods. Their arguments, exit codes, JSON output,
and text output remain unchanged.

## Typed inputs and results

Bootstrap receives an `AaplWorkspaceBootstrapRequest`, explicit redacted credential objects, and an
optional workspace path. It returns `AaplApplicationBootstrapResult`, which keeps the workspace
initialization and `AaplWorkspaceBootstrapSummary` together and verifies that both carry the same
workspace identity.

Query receives a `ConsolidatedDiagnosticRequest` and a mutually exclusive
`StorageLocationRequest`. It returns the existing versioned report model. It never initializes a
workspace, invokes a provider, recomputes analytics, or persists data.

```python
from datetime import UTC, datetime

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticRequest,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.core.models import DataFrequency

application = InvestmentAnalystApplication.create_default()
report = application.query_aapl_diagnostics(
    ConsolidatedDiagnosticRequest(
        known_at=datetime(2026, 7, 16, tzinfo=UTC),
        fundamental_frequency=DataFrequency.QUARTERLY,
    ),
    location=StorageLocationRequest(),
)
```

With an empty `StorageLocationRequest`, the normal workspace precedence remains unchanged. Callers
may instead select an initialized workspace or the compatible legacy root explicitly.

## Composition and safety

The facade uses `ApplicationRuntime` as the only workspace, storage, catalog, and provider-context
composition root. Provider clients remain outside the analytical core, credentials are never
stored in a result, and the query path does not construct an HTTP transport.

All existing invariants remain in the underlying typed services: deterministic identities,
append-only history, point-in-time eligibility through `available_at`, independent market and
fundamental diagnostics, explicit IEX limitations, and preservation of successful earlier stages
when a later bootstrap stage fails.

The facade does not catch and flatten domain errors. Typed workspace, storage, provider, bootstrap,
and query errors reach the CLI, runner, or local interface adapter so that each boundary can map
them to an appropriate user-facing status without losing the cause.

## Operational adapter

`AaplDailyRunner` is the stateful one-shot adapter over `bootstrap_aapl_workspace(...)`. It adds a
per-workspace process lock, atomic latest-run state, sanitized operational failures, and read-only
health without moving those concerns into the analytical facade. See
[`operational_runner.md`](operational_runner.md) for its CLI and recovery contract.

The loopback-only web adapter composes the same facade and runner through `AaplLocalController`.
It adds no storage access or provider logic of its own. See
[`local_interface.md`](local_interface.md) for the UI, scheduler, and persistent-service contract.
