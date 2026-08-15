# Application facade

`InvestmentAnalystApplication` is the stable programmatic entry point for the local application.
It lets later runners and user interfaces invoke supported operations without importing provider,
storage, or analytical pipeline implementations.

The facade exposes the following operation groups:

- `bootstrap_aapl_workspace(...)` initializes or reuses one workspace and executes the complete
  resumable SEC EDGAR and Alpaca Market Data pipeline with one writer connection.
- `query_aapl_diagnostics(...)` opens existing storage in read-only mode and returns the versioned
  `AaplDailyDiagnosticReport` from persisted evidence.
- read-only Apple-compatible fundamental queries plus
  `query_sec_fundamental_trend(...)`,
  `query_sec_fundamental_research(...)`,
  `query_sec_fundamental_research_history(...)` and
  `query_sec_fundamental_analysis(...)` for one catalog-backed SEC issuer;
- read-only listed-market, Apple and bounded BTC-USD chart queries;
- `refresh_sec_fundamentals(...)` resolves one catalog-backed corporate issuer, then uses one writer
  connection for SEC snapshots, observations, metrics, and its independent fundamental diagnostic;
- `refresh_fred_vintage(...)` importa una instantánea macro oficial con vintage explícito y
  `query_fred_point_in_time(...)` la reconstruye localmente en un corte de información, sin crear
  activos ni mezclarla con análisis de mercado o fundamentales;
- `refresh_fred_catalog_series(...)` enumera el borde de vintages de una serie configurada, importa
  un lote acotado y deja progreso y cobertura explícitos para reanudación;
- `refresh_bvl_registry(...)` actualiza por lote las razones sociales SMV configuradas y
  `query_bvl_registry(...)` relaciona el catálogo BVL con evidencia registral local en un corte
  explícito, sin atribuir precios ni fundamentales;
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

The generic SEC research methods additionally require one canonical `asset_id`. They resolve its
CIK, ticker, source identities and corporate capabilities from the injected catalog before opening
storage. A missing SEC binding therefore fails without creating a workspace or database. The
historical `query_aapl_*` methods delegate with `equity:us:aapl` and preserve their exact existing
schemas.

The generic SEC refresh requires `SecIssuerFundamentalRefreshRequest`, an existing storage location,
and an explicit `SecEdgarIdentity`. It never accepts or constructs Alpaca credentials. SEC is queried
on every run because filings and Company Facts revisions can appear independently from local market
coverage. A failure identifies its stage and does not roll back successful earlier stages.

La importación macro recibe un `FredApiKey` redactado y rangos de fecha explícitos. La consulta
recibe un `FredPointInTimeQuery` estricto y abre el almacenamiento en modo de solo lectura. Los
scripts FRED/ALFRED son adaptadores de estas operaciones y no construyen almacenamiento, clientes ni
pipelines por su cuenta.

El refresh registral recibe `BvlRegistryRefreshRequest`: sin IDs procesa las seis cotizaciones BVL
en orden estable; con IDs procesa únicamente el subconjunto explícito. Usa una sola conexión writer
y conserva el progreso de emisores anteriores si uno posterior falla. La consulta recibe
`BvlRegistryUniverseRequest`, abre read-only y nunca accede a la red. El transporte de formularios
se inyecta por separado del transporte GET para que las pruebas y futuros conectores no amplíen por
accidente la capacidad de clientes existentes.

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

El dominio macro conserva su propia fuente y `asset_id=None`. La fachada no lo convierte en una
observación de activo ni lo incorpora a un diagnóstico consolidado.

El dominio registral usa fuentes `registry`, conserva `asset_id=None` en las respuestas que abarcan
un emisor y no crea observaciones. `BVN` y `SCCO` BVL mantienen identidades distintas de sus
cotizaciones estadounidenses.

The facade does not catch and flatten domain errors. Typed workspace, storage, provider, bootstrap,
and query errors reach the CLI, runner, or local interface adapter so that each boundary can map
them to an appropriate user-facing status without losing the cause.

## Operational adapter

`AaplDailyRunner` is the stateful one-shot adapter over `bootstrap_aapl_workspace(...)`. It adds a
per-workspace process lock, atomic latest-run state, sanitized operational failures, and read-only
health without moving those concerns into the analytical facade. See
[`operational_runner.md`](operational_runner.md) for its CLI and recovery contract.

The loopback-only web adapter composes the same facade and runner through `AaplLocalController`.
Its SEC trend, research, history, and analysis reads are keyed by `asset_id`, and its independent
fundamental refresh delegates to `refresh_sec_fundamentals(...)` with the same writer mutex. It adds
no storage access or provider logic of its own. See
[`local_interface.md`](local_interface.md) for the UI, scheduler, and persistent-service contract.

## Corporate valuation

La fachada expone `query_corporate_valuation(...)` para reconstrucción local read-only y
`persist_corporate_valuation(...)` para materialización append-only. Ambas resuelven Alpaca/SEC y
el contrato de unidad desde catálogo/runtime. Los refresh de mercado y SEC llaman la misma capa tras
su analítica independiente; no hacen una segunda consulta de proveedor y reportan creados,
reutilizados y no evaluables por separado.

`query_corporate_valuation_history(...)` abre storage `READ_ONLY` para consultar la historia ya
materializada al corte solicitado; no llama proveedores, no reconstruye ausencias y no escribe datos.
`query_corporate_valuation_history_rule(...)` usa esa misma historia y el mismo acceso read-only
para evaluar una regla explícita sin persistir regla, evaluación, candidato ni estado.
