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

These contracts define data only. The project still has no persistence layer, database, provider
connection, financial calculation, real diagnostic logic, API, or user interface.
