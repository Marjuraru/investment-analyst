# Línea temporal y búsqueda local point-in-time del corpus documental SEC

Este documento describe el contrato de consulta `sec-document-timeline-v1`, el servicio y el CLI
para la búsqueda y enumeración determinista de documentos oficiales SEC a un corte point-in-time
`known_at`.

## Unidad de dominio y familias documentales

La consulta devuelve revisiones ya persistidas en el almacenamiento local, cubriendo dos familias
disjuntas sin fusionar identidades ni inferir correspondencias entre activo y declarante:

1. **`asset_document`** (`sec-document-revision-v2`): revisiones vinculadas a un activo corporativo
   del catálogo (`asset_id`), donde `available_at == filing.accepted_at` representa la disponibilidad
   pública oficial.
2. **`filer_document`** (`sec-filer-document-revision-v1`): revisiones vinculadas a un declarante
   institucional (`filer_cik`), como filings 13F-HR, sin vínculo a un `asset_id` específico.

Cada entrada (`SecDocumentTimelineEntry`) declara explícitamente su `family`, `revision_id`, `accession`,
`form`, fechas oficiales, tamaño y digest SHA-256 del contenido. El campo `asset_id` está presente si y
sólo si `family == "asset_document"`.

## Semántica point-in-time y rango público inclusivo

- **Corte point-in-time:** Una revisión es elegible exclusivamente si `available_at <= known_at`.
  Cualquier documento con disponibilidad posterior al corte queda excluido. La fecha de recepción local
  `retrieved_at` preserva la auditoría de captura y **nunca** se utiliza como criterio de disponibilidad.
- **Rango público inclusivo:** Los filtros de fecha `available_from` y `available_to` son inclusivos en
  ambos extremos conforme a la regla general del repositorio. Internamente se transforman a un
  intervalo medio-abierto UTC `[from 00:00:00Z, (to + 1 día) 00:00:00Z)`, de modo que una revisión
  publicada a cualquier hora dentro de la fecha final solicitada queda incluida.
- **Historia legacy excluida:** Las revisiones históricas `sec-document-revision-v1` (cuya disponibilidad
  se basaba en el tiempo de recuperación) no se incluyen en los resultados y se contabilizan explícitamente
  en el campo `legacy_records_excluded`.

## Orden determinista y contadores

- **Orden total:** Los resultados se ordenan de forma total y determinista por `(available_at, family, revision_id)`.
  Dos ejecuciones independientes con relojes distintos sobre el mismo almacenamiento devuelven exactamente
  las mismas entradas en el mismo orden.
- **Contadores coherentes:** `matched_count` indica el total de coincidencias elegibles; `returned_count`
  indica las entradas devueltas (afectadas por el parámetro opcional `limit`); `truncated` es verdadero si
  y sólo si `returned_count < matched_count`.
- **Ausencia explícita:** Si no existen coincidencias, el resultado reporta `state: "missing"` con cero
  entradas, evitando listas vacías ambiguas o ceros no tipados.

## Operación, CLI y ruta HTTP local

La consulta se ejecuta en modo estricto de solo lectura (`WorkspaceAccessMode.READ_ONLY`), no altera el
almacenamiento y no requiere red ni credenciales.

### CLI local

Ejemplo de uso del CLI:

```bash
python scripts/query_sec_document_timeline.py \
  --workspace /path/to/workspace \
  --known-at 2026-01-01T00:00:00Z \
  --asset-id equity:us:aapl \
  --filer-cik 0001067983 \
  --form 10-K \
  --form 13F-HR \
  --available-from 2025-01-01 \
  --available-to 2025-12-31 \
  --limit 50
```

### Ruta HTTP local

La aplicación web local expone la consulta bajo demanda mediante:

```text
GET /api/v1/sec-document-timeline?known_at=2026-01-01T00:00:00Z&asset_id=equity:us:aapl&form=10-K&limit=50
```

Parámetros soportados:
- `known_at`: corte point-in-time UTC obligatorio (ISO-8601).
- `asset_id`: opcional múltiple; se valida que pertenezca a un activo corporativo con configuración SEC en el catálogo.
- `filer_cik`: opcional múltiple; CIK de 10 dígitos normalizado.
- `form`: opcional múltiple; Forms oficiales soportados en el corpus v1.
- `accession`: opcional; formato de accession SEC `0000000000-00-000000`.
- `available_from` y `available_to`: opcionales; fechas inclusivas `YYYY-MM-DD`.
- `limit`: opcional; entero entre 1 y 1000.

Cualquier parámetro no soportado o activo sin configuración SEC se rechaza con error acotado 400 (`invalid_request`).

## Fronteras y límites del producto

Esta capa es estrictamente de lectura y evidencia. De forma explícita:

- **No es búsqueda de texto completo:** no indexa texto libre, no realiza extracciones de fragmentos,
  no genera embeddings ni consulta bases vectoriales.
- **No lee ni expone bytes de contenido:** no materializa el cuerpo de los documentos en memoria, no lo
  imprime ni lo retorna; solo expone metadatos verificables (`content_sha256`, `content_size_bytes`).
- **No es señal ni analítica predictiva:** no calcula métricas, scores, rankings, percentiles, señales de
  inversión ni recomendaciones.
- **No modifica ni escribe:** no genera RawRecords, observaciones, métricas, eventos ni archivos en disco.
- **No altera contratos:** la ruta HTTP reutiliza exactamente `SecDocumentTimelineResult` sin añadir ni renombrar campos.
