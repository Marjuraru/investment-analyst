# SEC EDGAR issuer foundation

This layer establishes the raw, auditable foundation for one configured SEC issuer. It uses the U.S.
Securities and Exchange Commission's public EDGAR data service and does not require an API key.
Asset, ticker, CIK, source IDs, name, exchange, and currency are explicit typed configuration.
Apple CIK `0000320193` and its historical source identities remain the default compatibility scope.

## Imported documents

- **Submissions** preserves issuer metadata and filing-history structures.
- **Company Facts** aggregates XBRL concepts reported across multiple filings, taxonomies, units,
  and periods.

Ambos documentos deben conservar exactamente el CIK configurado y Submissions debe incluir el
ticker esperado. Los nombres visibles de la entidad pueden diferir en Unicode, mayúsculas, espacios,
puntuación, abreviaturas legales estándar como `Corp`/`Corporation` o un marcador de jurisdicción
posterior al sufijo como `/DE/`. Las dos cadenas raw se preservan y un cambio material de palabras
sigue fallando. El nombre no sustituye al CIK como identidad.

The importer makes exactly two sequential HTTPS requests to `data.sec.gov`, with at least 0.5
seconds between them. SEC access requires a declared `User-Agent` containing an application name
and contact email. It is identification for responsible access, not a secret or authentication key.

## Raw snapshots and traceability

Each exact response body receives a SHA-256 checksum before parsing. JSON numbers are preserved as
strings, and the complete parsed document is stored inside a `RawRecord`. The checksum in the source
reference is the checksum of the exact HTTP response body, while local raw-file storage also protects
its canonical `RawRecord` representation.

The record UUID is UUID5-derived from source, document type, CIK, response checksum, and schema
version. Therefore identical content is idempotent even when fetched later, while changed content
creates a new immutable version and leaves earlier snapshots available.

`retrieved_at` is used conservatively for `event_time`, `available_at`, and `received_at`. It states
when this system knew the snapshot; it does not claim that every fact inside Company Facts became
public at that moment.

This step does not select accounting concepts, resolve periods or amendments, create normalized
observations, calculate ratios, or produce fundamental diagnostics.

## Running the importer

Set a descriptive SEC identity without treating it as a secret:

```bash
export SEC_USER_AGENT="Investment Analyst contact@example.com"
```

The compatibility CLI remains the fixed Apple raw-only composition:

```bash
python scripts/fetch_sec_aapl_fundamentals.py --root data/sec-aapl
```

The catalog-backed command runs the complete independent fundamental refresh for a configured
issuer:

```bash
.venv/bin/python scripts/refresh_sec_fundamentals.py \
  --workspace ~/.local/share/investment-analyst/workspaces/default \
  --asset-id equity:us:amd \
  --frequency annual
```

Both commands print only compact summaries. They do not print documents or the configured
User-Agent. The generic command additionally normalizes observations, persists five transparent
metrics and one independent fundamental diagnostic. No orders are executed, and the stored
documents do not constitute financial advice.
