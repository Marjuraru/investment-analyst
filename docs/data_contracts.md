# Data contracts

The project separates each processing stage so that later calculations can be inspected and
reconstructed without hiding transformations.

## Data layers

- A **raw record** preserves the original JSON payload received from a provider. It does not
  interpret, rename, aggregate, or calculate fields.
- A **normalized observation** extracts one typed value from a raw record and attaches a unit,
  frequency, period, source reference, quality status, and transformation version.
- A **metric result** is a deterministic calculation over one or more normalized observations. Its
  definition, parameters, input observation identifiers, algorithm version, and timestamps remain
  explicit.

`market-multi-asset-comparison-v1` conserva fechas comunes UTC, cobertura por activo, fuentes, UUID de observaciones de cierre y `available_at`. Las métricas se calculan sobre la misma muestra de 21–1300 fechas con `Decimal` de precisión 34; cuando una varianza es cero, correlación o beta son explícitamente no disponibles.
- A **diagnostic** combines versioned metric results into components and evidence. Scores, weights,
  weighted contributions, confidence, verdict, and explanation are stored as auditable outputs.

## Time semantics and availability

`available_at` is the earliest time at which information could legitimately have been used by the
analysis. It prevents look-ahead bias and is distinct from the time at which the project downloaded
or calculated the data.

- `observed_at` identifies when an instantaneous observation occurred.
- `period_end` identifies the end of a reporting period, such as a fiscal quarter.
- `received_at` identifies when the local system received a raw record.
- `computed_at` identifies when a metric or diagnostic was calculated.

All timestamps must include timezone information. Models normalize timestamps to UTC internally so
values from different providers can be compared consistently.

## Numeric precision

Financial observations, metric values, scores, weights, and contributions use `Decimal` rather than
binary floating-point values. This keeps decimal arithmetic explicit and avoids avoidable rounding
surprises in auditable calculations.

## Traceability

A diagnostic references metric results through its evidence and components. Each metric result lists
its input observation identifiers. Every normalized observation points to a raw record and retains a
source reference. The raw record preserves the original payload and source metadata. This chain makes
it possible to trace a diagnostic back to the records from which it was derived.

These core contracts define data only; persistence, providers, calculations, diagnostics, API and
the local interface remain in their dedicated layers.

## Crypto derivatives evidence

Deribit backfill uses the first local `received_at` as `available_at`; an historical event timestamp
never makes a later retrieval visible early. Funding, daily DVOL and prospective summary snapshots
have separate schemas, sources, units and periods. Complete-interval receipts are raw evidence but
never observations or analytical inputs. The provider field `last` maps explicitly to
`last_price`, while snapshot `current_funding`/`funding_8h` remain distinct from historical
`interest_1h`/`interest_8h`. Metric and read-only diagnostic identities include their exact PIT
inputs and cut. See [`crypto_derivatives.md`](crypto_derivatives.md).

## Corporate valuation snapshot

`corporate-valuation-request-v1` fija activo, `known_at`, fecha y `latest_annual`.
`corporate-valuation-snapshot-v1` publica selección de precio/filing, edad, moneda, contrato de
unidad, inputs con raw/source/observation IDs, definiciones versionadas, resultados y cobertura.
Cada resultado evaluado conserva `result_id`, `available_at` e inputs específicos; una ausencia usa
un reason code y nunca un cero. Véase
[`corporate_valuation_point_in_time.md`](corporate_valuation_point_in_time.md).
