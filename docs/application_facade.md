# Application facade

`InvestmentAnalystApplication` is the stable programmatic entry point for the current Apple MVP.
It lets later runners and user interfaces invoke supported operations without importing provider,
storage, or analytical pipeline implementations.

The facade currently exposes two operations:

- `bootstrap_aapl_workspace(...)` initializes or reuses one workspace and executes the complete
  resumable SEC EDGAR and Alpaca Market Data pipeline with one writer connection.
- `query_aapl_diagnostics(...)` opens existing storage in read-only mode and returns the versioned
  `AaplDailyDiagnosticReport` from persisted evidence.

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
and query errors reach the CLI, runner, or future interface adapter so that each boundary can map
them to an appropriate user-facing status without losing the cause.
