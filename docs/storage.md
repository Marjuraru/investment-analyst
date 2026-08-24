# Local storage

The local storage layer separates three responsibilities:

- **Raw files** contain one canonical JSON representation of each `RawRecord` received by the
  application. They are immutable and indexed in DuckDB.
- **Primary SEC documents** use an additive content-addressed store under
  `data/documents/sha256/`. RawRecords retain only revision metadata and lineage; document bytes
  are written atomically once, verified by SHA-256 on read, and never duplicated in DuckDB or raw
  JSON. The optional directory does not change the DuckDB schema or workspace format.
- **DuckDB** is the operational local catalog for assets, sources, observations, metric definitions,
  metric results, diagnostics, and raw-record metadata.
- **Parquet** is an export and analytical archive format. It is not the operational source of truth.

Raw records are immutable so later analysis can be traced to the exact representation stored by the
application. Reusing a record identifier with different content is treated as a conflict. Canonical
JSON means UTF-8 JSON with sorted keys, compact separators, deterministic model serialization, and no
NaN or Infinity. The SHA-256 checksum covers those stored canonical bytes; it does not claim to cover
the provider's original network bytes.

Assets, source definitions, and metric definitions are explicitly updatable through `upsert`.
Normalized observations, metric results, diagnostic results, and raw records are append-only. Repeated
writes with an identical identifier and document are idempotent, while different content conflicts.

The DuckDB schema is currently version **1**. Initialization is idempotent and rejects an incompatible
version. The first implementation assumes a single writer process. Connections are owned explicitly
and closed by context managers; no global DuckDB connection is used.

Repository protocols isolate the core models from this implementation. A later version can replace
DuckDB with PostgreSQL and raw files with object storage without changing financial contracts. SQLite
is intentionally deferred until the project adds a journal, alerts, and personal preferences.

La capa de storage no realiza cálculos ni diagnósticos: los pipelines de aplicación entregan modelos
ya validados a sus repositorios.

La valoración corporativa no cambia el schema DuckDB ni el formato del workspace. Sus definiciones
usan la categoría `valuation` y sus valores evaluados usan la tabla append-only `metric_results`;
los estados `not_evaluable`/`not_applicable` permanecen en el snapshot de consulta. Un pipeline
guarda métricas una a una para conservar progreso válido si una escritura posterior falla.
