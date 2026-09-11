# Universo oficial y acotado de gestores SEC Form 13F

Este documento define la especificación, el pipeline de adquisición y la semántica point-in-time
del artefacto `sec-13f-manager-universe-v1` y la política de selección
`sec-13f-manager-universe-selection-v1`, implementados en `SEC-CORPUS-27`.

El objetivo exclusivo de este artefacto es proveer una cola de adquisición determinista, auditable y
acotada de gestores institucionales Form 13F para los activos que ya cuentan con un binding CUSIP en
el catálogo (`equity:us:aapl` → `037833100`). No genera observaciones de posiciones, no infiere
correspondencias de clase y no emite señales ni recomendaciones.

## Origen de datos oficial

- **Fuente:** SEC Form 13F Data Sets trimestrales publicados oficialmente por la SEC en
  `https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets`.
- **Acceso:** Cliente HTTPS exclusivo con cabecera `SEC_USER_AGENT` obligatoria.
- **Catálogo:** La URL oficial se inspecciona buscando enlaces relativos o absolutos bajo
  `/files/structureddata/data/form-13f-data-sets/` que cumplan el patrón
  `DDmonYYYY-DDmonYYYY_form13f.zip`. Se selecciona determinísticamente el período más reciente
  (`period_end` descendente, `period_start` descendente).
- **Límites de transporte:** Catálogo limitado a 2 MiB; archivo ZIP limitado a 160 MiB comprimidos.
  Cualquier redirección fuera de `sec.gov`, contenido no ZIP o respuesta no 200 falla cerrado.

## Integridad del archivo y parser seguro

El archivo ZIP se procesa en streaming y en memoria sin dependencias externas:

1. **Límites de archivo:**
   - Máximo 16 miembros en el archivo ZIP.
   - Máximo 1 GiB descomprimido acumulado.
   - Prohibición estricta de cifrado, extensiones ZIP64 no estándar y secuencias de path traversal (`..`, `/`).
2. **Tablas requeridas:**
   - Exige la presencia obligatoria de `SUBMISSION.tsv`, `COVERPAGE.tsv` e `INFOTABLE.tsv`.
   - Se validan encabezados conocidos y se falla cerrado ante columnas faltantes, tipos inválidos o delimitadores ambiguos.
3. **Filtrado por formulario y CUSIP:**
   - Solo se admiten formularios `13F-HR` y `13F-HR/A`. Se descartan expresamente `13F-NT` y `13F-NT/A`.
   - La tabla `INFOTABLE.tsv` se lee en streaming contra el conjunto de CUSIP activos declarados en el catálogo.
   - Se descartan filas cuyo CUSIP no esté en el catálogo. No se realizan uniones por ticker, emisor o similitud de texto.

## Almacenamiento y linaje

- **Document Store:** El ZIP completo e inalterado se almacena en `documents/sha256/<hash>.zip` con
  fsync atómico y verificación inmediata de integridad SHA-256.
- **Revisión RawRecord:** Se persiste bajo `source_id="sec-edgar:form-13f-data-set:manager-universe"` con:
  - `dataset_id`: derivado deterministicamente de URL, período y SHA-256.
  - `period_start` y `period_end`: fechas UTC del período trimestral del dataset.
  - `available_at`: exactamente `retrieved_at` (marca de tiempo UTC de la descarga local).
  - `event_time`: `period_end` a las 00:00:00 UTC.
- **Idempotencia:** Si se reejecuta la adquisición y los bytes del ZIP son idénticos, se reutiliza
  el blob y la revisión sin crear registros duplicados. Si el ZIP fue corregido en origen por la SEC,
  se genera una nueva revisión preservando la histórica.

## Política de selección (`sec-13f-manager-universe-selection-v1`)

Para cada activo con binding CUSIP:

1. Se determina el `PERIODOFREPORT` más reciente presente en el dataset para dicho activo.
2. Se agrupan las posiciones exactas de dicho período por `(asset_id, CIK, accession)`.
3. Se suma el valor reportado (`VALUE`) utilizando aritmética `Decimal` exacta en unidades
   declaradas (`usd_thousands_as_filed`).
4. Para cada gestor, si existen múltiples accessions para el mismo período (por ejemplo, enmiendas),
   se selecciona el accession más reciente por `(filing_date, accession)` como prioridad operativa;
   todos los accessions coincidentes se conservan en el linaje (`accession_lineage`) y se declara si
   existe enmienda (`is_amendment`).
5. Los gestores candidatos se ordenan de forma determinista para la selección:
   - Valor reportado as-filed descendente (`-value_as_filed`).
   - CIK normalizado ascendente (`manager_cik`).
   - Accession number ascendente (`operational_acc`).
6. Se trunca el universo a un máximo de 25 gestores (`max_managers_per_asset = 25`).
7. El snapshot resultante (`sec-13f-manager-universe-v1`) declara `candidate_manager_count`,
   `selected_manager_count`, `unselected_manager_count` y `coverage_complete` (`coverage_complete=False`
   si existe truncación o faltan CUSIP elegibles; `True` únicamente cuando todos los candidatos fueron
   seleccionados y todos los CUSIP elegibles del catálogo están cubiertos).

## Semántica Point-in-Time (PIT)

- Los Form 13F Data Sets de la SEC contienen `FILING_DATE`, pero no contienen `acceptanceDateTime`.
- Por consiguiente, para garantizar la integridad PIT sin sesgo de anticipación, `available_at` es
  estrictamente igual a `retrieved_at`. `FILING_DATE` se registra como metadato as-filed, pero nunca
  sustituye el momento en que la información estuvo disponible localmente.
- Las consultas PIT aplican `available_at <= known_at`.
- Cada consulta verifica la existencia del blob en el document store y la correspondencia exacta de su SHA-256.

## Herramientas CLI

- `scripts/refresh_sec_institutional_manager_universe.py`: Descarga y procesa el dataset oficial más
  reciente de la SEC utilizando la identidad en `SEC_USER_AGENT`, persistiendo la revisión en el
  document store y el snapshot en el almacenamiento append-only del workspace.
- `scripts/query_sec_institutional_manager_universe.py`: Consulta de solo lectura del universo
  disponible a un corte `known_at` específico (con filtros opcionales `--asset-id` y `--cik`),
  verificando la integridad del blob y linaje, y emitiendo la salida en formato JSON estructurado.
- `scripts/smoke_sec_institutional_manager_universe.py`: Verificación integral de extremo a extremo
  en un workspace temporal contra los endpoints oficiales de la SEC, validando límites de red,
  memoria RSS e idempotencia.

## Consumo dirigido del snapshot (`SEC-CORPUS-28`)

`SEC-CORPUS-28` consume este artefacto como cola de adquisición bajo el contrato
`sec-institutional-holdings-directed-refresh-v1` y la política
`sec-institutional-holdings-directed-page-v1`, documentados en
[Posiciones institucionales SEC Form 13F](sec_institutional_holdings.md). El consumo mantiene dos
límites explícitos que este documento no relaja:

- El `accession` y el `accession_lineage` del dataset son **pistas de descubrimiento**, nunca
  autoridad de importación. La lista exacta de filings y sus marcas de aceptación se resuelve desde un
  GET fresco a `data.sec.gov/submissions/CIK##########.json` por gestor procesado.
- La única marca de disponibilidad PIT del dataset sigue siendo `available_at = retrieved_at`, porque
  el ZIP oficial carece de `acceptanceDateTime`. Para la evidencia 13F importada, en cambio, la
  disponibilidad hereda la aceptación SEC exacta del filing.

El artefacto continúa sin generar observaciones de holdings, sin inferir correspondencias de clase y
sin calcular métricas. `SEC-CORPUS-29` consume la misma página determinista de gestores —orden
`(selection_rank, asset_id, manager_cik, report_period)` y deduplicación por
`(manager_cik, report_period)`— para probar la correspondencia row-scoped de cada candidato y
materializar observaciones PIT, conservando la asociación exacta entre `candidate_id` y su propio
`(asset_id, cusip, manager_cik, report_period)` sin productos cartesianos.

Los dos wrappers CLI (`refresh_sec_institutional_manager_universe.py` y
`query_sec_institutional_manager_universe.py`) usan ahora el contrato vivo de runtime y localización:
`ApplicationRuntime.create_default()` con `StorageLocationRequest(workspace=...)` y un workspace
inicializado. Sus flags públicos y su salida JSON no cambian.

## Integración en el ciclo institucional programado (`SEC-CORPUS-30`)

`SEC-CORPUS-30` integra la adquisición del universo dentro del ciclo institucional automático:
- Sondea diariamente el catálogo HTML oficial (`fetch_catalog_page`) para detectar trimestres o URLs nuevas.
- Descarga el archivo ZIP sólo si no existe snapshot activo en el almacenamiento, si el catálogo publica un
  nuevo período/URL, o si expira el tiempo de vida de validación de 7 días.
- Comparte la conexión de escritura `READ_WRITE` con las etapas de adquisición dirigida y materialización,
  eliminando aperturas redundantes del almacenamiento.

## Consulta exacta de la ventana de dos cierres (`SEC-CORPUS-31`)

`SEC-CORPUS-31` reutiliza el universo ya persistido sin volver a descargar evidencia:

- `list_dataset_revisions(period_start, period_end, known_at)` enumera las revisiones persistidas de un
  período oficial exacto disponibles al corte, y `find_snapshot_for_period(period_start, period_end,
  dataset_url, known_at)` resuelve el par revisión/snapshot verificable más reciente que coincide con la
  URL oficial del catálogo.
- El blob debe existir y coincidir con su SHA-256; revisiones competidoras con el mismo instante de
  recuperación y hash distinto, o snapshots competidores del mismo instante, fallan cerrado en lugar de
  elegir arbitrariamente.
- Una URL contradictoria o un período distinto nunca se incorporan a la ventana: la ventana queda
  formada exclusivamente por los dos períodos adyacentes más recientes del catálogo vivo.
