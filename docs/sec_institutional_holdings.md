# Posiciones institucionales SEC Form 13F

Esta vertical conserva evidencia descriptiva de `13F-HR` y `13F-HR/A` presentada por gestores
institucionales. El gestor se declara explícitamente mediante CIK en la CLI y no se incorpora al
catálogo de activos. Ningún contrato de reporte o posición contiene `asset_id`; todos sus
`RawRecord` persisten `asset_id=null`.

El importador consulta las Submissions oficiales de `data.sec.gov` en cada ejecución y limita el
descubrimiento a `filings.recent`. Para cada accession seleccionado consulta el manifest oficial de
SEC Archives y acepta únicamente dos XML de nivel superior: el documento primario declarado como
portada y el segundo XML como information table. Cero, uno, tres o más XML, HTML, texto, DTD,
`ENTITY` y raíces inesperadas producen outcomes rechazados explícitos; no se reparan ni se extraen
heurísticamente.

La revisión documental usa `sec-filer-document-revision-v1`. Está vinculada al CIK del declarante,
no a un activo, y tiene namespaces UUID5 separados de `sec-document-revision-v1/v2`. Para ambas
familias se comparten `SecFiling` y `SecLogicalDocument`, pero una revisión de declarante nunca se
convierte en una revisión asociada a activo. Los bytes exactos se direccionan por SHA-256 en el
almacén documental y el backup verifica contenido, metadata y lineage al snapshot Submissions.

## Semántica point-in-time

`available_at` deriva exactamente de `SecFiling.accepted_at`; `retrieved_at` conserva la recepción
local y `parsed_at` la materialización determinista. Un filing sin `acceptanceDateTime` utilizable
falla cerrado. `periodOfReport` ausente se conserva como `report_date=None`: nunca se sustituye por
`filingDate`, `accepted_at`, cero ni una fecha sintética. La consulta aplica
`available_at <= known_at` antes de devolver reportes y posiciones.

Las posiciones preservan el CUSIP, el título de clase y el nombre del emisor declarados, además de
cantidad, tipo de cantidad, valor, discreción de inversión y autoridad de voto cuando están
presentes. Los importes usan `Decimal` exacto. La suma de valores se compara con el total de la
portada; una discrepancia queda visible y no corrige, redondea ni descarta posiciones. Los valores
13F se conservan en la unidad declarada por SEC, sin conversión monetaria.

## Operación acotada

Configura la identidad SEC sólo en el proceso y usa un workspace temporal o aprobado:

```bash
export SEC_USER_AGENT="Investment Analyst contact@example.com"
python scripts/import_sec_institutional_holdings.py --workspace /tmp/sec-13f --filer-cik 1067983 --form 13F-HR
python scripts/query_sec_institutional_holdings.py --workspace /tmp/sec-13f --filer-cik 1067983 --known-at 2026-01-01T00:00:00Z
```

La consulta es local y de solo lectura. Esta etapa no resuelve CUSIP a `asset_id`, ticker, ISIN o
instrumento; tampoco calcula variaciones, entradas, salidas, concentración, score, señal, candidato
ni recomendación. El backfill fuera de `filings.recent`, los formularios `13F-NT` y la agregación
entre gestores quedan fuera del contrato.

## Enriquecimiento semántico v2

El paso explícito posterior conserva el XML 13F as-filed sin fusionar filas ni interpretar una
enmienda como cartera efectiva. Véase [Semántica institucional 13F as-filed](sec_institutional_semantics.md)
para el contrato de identidad, PIT, unidades no resueltas, consulta paginada y verificación de backup.

## Proyección por activo mediante evidencia declarada

`scripts/query_institutional_holdings_by_asset.py` expone una proyección local de sólo lectura que
conecta posiciones Form 13F con correspondencia CUSIP explícitamente declarada. Conserva el
`manager_cik` y `known_at` requeridos por el corpus, no modifica reportes ni posiciones y informa
sin enlace los períodos faltantes o las correspondencias ambiguas.

## Universo oficial y acotado de gestores Form 13F

La selección de gestores institucionales no se realiza a partir de listas arbitrarias o tickers libres.
`SEC-CORPUS-27` introduce el artefacto reproducible `sec-13f-manager-universe-v1` y la política
`sec-13f-manager-universe-selection-v1`, documentados en detalle en
[Universo oficial de gestores SEC Form 13F](sec_institutional_manager_universe.md).

Este pipeline procesa los Form 13F Data Sets trimestrales publicados oficialmente por la SEC en
`https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets`, conserva el archivo ZIP exacto
en el almacén documental por SHA-256 (`documents/sha256/`), y filtra determinísticamente las tablas
as-filed contra los CUSIP activos del catálogo de activos (`equity:us:aapl` → `037833100`).

Para el período de reporte más reciente del dataset, selecciona hasta 25 gestores por activo ordenados
por valor as-filed descendente con desempate determinista por CIK y accession ascendentes, seleccionando
para cada gestor su accession más reciente por `(filing_date, accession)`. La
disponibilidad point-in-time conserva `available_at = retrieved_at`, dado que el dataset carece de
marcas de tiempo de aceptación. Este artefacto sirve exclusivamente como cola de descubrimiento y
adquisición delimitada para la posterior ingesta dirigida de filings XML por gestor en `SEC-CORPUS-28`;
no genera observaciones de holdings, no infiere clases de acciones y no calcula métricas.

## Adquisición dirigida y reanudable desde el universo

`SEC-CORPUS-28` conecta el universo persistido con la evidencia 13F real. El contrato de request es
`sec-institutional-holdings-directed-refresh-v1` bajo la política
`sec-institutional-holdings-directed-page-v1`; admite exclusivamente `known_at`, `manager_offset >= 0`,
`manager_limit` entre 1 y 25 y `accessions_per_manager` entre 1 y 10. Ningún CIK, activo, CUSIP,
accession, formulario, URL ni período de reporte es payload libre.

La ejecución resuelve el snapshot más reciente del universo disponible en `available_at <= known_at`,
falla cerrado si no existe o si el linaje con su revisión de dataset no verifica —sin descargar ni
refrescar el dataset implícitamente— y considera sólo candidatos `is_selected=true`. El orden es
`(selection_rank, asset_id, manager_cik, report_period)`, la deduplicación por
`(manager_cik, report_period)` conserva la lista de candidate IDs de origen, y `manager_offset` con
`manager_limit` se aplican después: la paginación es un presupuesto operativo, no un ranking analítico.

Para cada gestor de la página se realiza exactamente un GET fresco a
`data.sec.gov/submissions/CIK##########.json`. La lista de filings y sus marcas de aceptación se
resuelven siempre desde ese Submissions oficial: el accession y el `accession_lineage` del dataset son
pistas de descubrimiento y nunca autoridad de importación. Sólo son elegibles `13F-HR` y `13F-HR/A` con
`report_date == report_period` y `accepted_at <= known_at`, importados en orden
`(accepted_at, accession)` para leer base y enmiendas en secuencia. Un accession duplicado incompatible,
un `reportDate` ausente o conflictivo, una aceptación sin zona horaria o una identidad CIK divergente
fallan cerrado; `filing_date` nunca sustituye la disponibilidad.

Antes de llamar a SEC Archives se consultan los reportes ya persistidos del mismo gestor y corte. Un
accession materializado se devuelve como `reused` y no genera ningún GET a Archives; de los pendientes
se importan como máximo `accessions_per_manager`. El resultado declara `pending_before`, `attempted`,
`created`, `reused`, `rejected` y `backlog_after`, de modo que un límite insuficiente es visible en
lugar de silencioso. Los outcomes rechazados conservan sus bytes e identidad y pueden reevaluarse si el
proveedor corrige los documentos; un fallo posterior no borra el progreso ya persistido y la ejecución
continúa con el gestor siguiente.

Tras cada intento se reúnen los reportes del período objetivo visibles al corte y se llama al
`InstitutionalHoldingsSemanticsService` integrado en lotes de hasta veinte IDs por gestor, reutilizando
artefactos ya enriquecidos sin cambiar parser, identidad ni repositorio semántico. El resumen estricto
incluye snapshot y revisión usados, corte efectivo, página de gestores, lineage de candidatos,
CIK/nombre/período, llamadas Submissions y Archives, accessions descubiertos, creados, reutilizados y
fallidos, backlog y resultados semánticos. No persiste score, veredicto, señal ni recomendación, y no
crea correspondencias CUSIP↔activo.

```bash
export SEC_USER_AGENT="Investment Analyst contact@example.com"
python scripts/refresh_sec_institutional_holdings_from_universe.py --workspace /tmp/sec-13f \
  --known-at 2026-09-01T00:00:00Z --manager-offset 0 --manager-limit 1 --accessions-per-manager 5
```

El comando no refresca el dataset del universo: si falta un snapshot falla con una instrucción
operacional que nombra `scripts/refresh_sec_institutional_manager_universe.py`. La salida JSON es
compacta y el exit es distinto de cero sólo ante un fallo global.

## Correspondencia verificable por fila

`SEC-CORPUS-29` cierra la costura evidencia 13F→activo→observación con el contrato aislado
`sec-institutional-row-correspondence-v1`, bajo el source ID
`sec-edgar:institutional-row-correspondence` y la política
`sec-institutional-row-correspondence-policy-v1`. No sustituye la declaración humana
`instrument-correspondence-v1`, que permanece intacta como camino compatible.

Un claim prueba que el CUSIP exacto de **una fila** as-filed coincide con el CUSIP del candidato de
universo ya derivado del catálogo, para el mismo período reportado, gestor y artefacto semántico. Su
vigencia es cerrada y no configurable (`effective_from == report_period`,
`effective_to == report_period + 1 día`), `available_at` es exactamente
`max(snapshot.available_at, artifact.available_at)` y el `event_time` es el inicio UTC del período.
La identidad UUID5 incorpora política/schema, snapshot y candidato, artefacto y fila, activo, CUSIP,
clase, período y `available_at`; `recorded_at` describe cuándo se escribió la prueba y queda fuera de
la identidad, de modo que una rematerialización equivalente reutiliza el claim persistido sin
reescribirlo. Cualquier otra diferencia sí falla cerrado.

Antes de persistir se verifica la cadena completa: snapshot y revisión de dataset existentes con hash
y período coincidentes, candidato embebido y `is_selected=true` con CIK, activo, CUSIP y período
exactos, reporte y artefacto semántico del mismo gestor y período, fila presente por `row_id` con CUSIP
y clase literales, y el binding SEC/CUSIP vivo del catálogo. Si el catálogo ya no coincide, la
operación falla cerrado: no se reconstruyen bindings históricos ni se une por ticker, nombre, FIGI,
ISIN o similitud.

Para una misma fila y activo, varias pruebas equivalentes visibles se resuelven por
`(available_at, correspondence_id)` ascendente. Claims que difieren en activo o en contenido declarado
permanecen ambiguos, se exponen con su razón y **no** producen observaciones; tampoco se recurre al
camino manual para "desempatar" artificialmente. La consulta resuelve el identificador contra ambos
repositorios, exige exactamente un padre y devuelve una unión tipada en `InstitutionalObservationView`.

## Materialización dirigida de observaciones

`sec-institutional-observation-materialization-v1` materializa observaciones PIT desde el universo sin
realizar red y abriendo el workspace una sola vez. El request sólo admite `known_at`,
`manager_offset >= 0` y `manager_limit` entre 1 y 25; ningún CIK, activo, CUSIP, accession, report ID,
row ID, URL o período es payload libre.

La página de gestores es la misma que ya integra `SEC-CORPUS-28` —orden
`(selection_rank, asset_id, manager_cik, report_period)` y deduplicación por
`(manager_cik, report_period)`— pero conservando la asociación exacta de cada `candidate_id` con su
propio `(asset_id, cusip, manager_cik, report_period)`, sin formar productos cartesianos. Para cada
candidato se leen únicamente reportes y semántica ya persistidos y visibles del mismo gestor y período;
cada fila con CUSIP exacto crea o reutiliza un claim row-scoped. La ausencia de reporte, de semántica o
de fila coincidente se reporta como estado explícito (`missing_report`, `not_enriched`,
`missing_rows`), nunca como cero holdings. Un fallo por gestor se registra con su código, conserva el
progreso ya persistido y no introduce rollback global.

```bash
python scripts/materialize_sec_institutional_observations_from_universe.py --workspace /tmp/sec-13f \
  --known-at 2026-09-11T00:00:00Z --manager-offset 0 --manager-limit 1
```

## Ciclo institucional programado y reanudable

`SEC-CORPUS-30` integra las tres capas previas (#27 universo, #28 adquisición dirigida y #29
correspondencia/observaciones) en una sola operación atómica y reanudable, bajo el contrato
`sec-institutional-scheduled-cycle-v1` y la política `sec-institutional-cycle-policy-v1`.

### Arquitectura y presupuesto de red

1. **Una sola conexión writer**: El ciclo abre el workspace en modo `READ_WRITE` exactamente una vez,
   compartiendo la conexión entre el universo, la adquisición y la materialización.
2. **Presupuesto acotado de red**:
   - Sondeo diario de catálogo oficial HTML (`fetch_catalog_page`, ~50 KB).
   - Descarga de ZIP oficial (~15 MB) condicional: únicamente si no existe snapshot activo, si el
     catálogo publica un nuevo período/URL o si han transcurrido más de 7 días desde la última
     validación. En cache hit ordinario se realizan cero peticiones GET de ZIP.
   - Paginación de un gestor por ejecución con `accessions_per_manager <= 2`.
   - Máximo un GET de Submissions por gestor (`data.sec.gov/submissions/CIK##########.json`).
   - Reutilización estricta de filings ya descargados y de outcomes rechazados verificados.
3. **Persistencia atómica y recuperación**:
   - Archivo de estado `state/sec_institutional_cycle_state_v1.json` con `schema_version = "sec-institutional-cycle-state-v1"`.
   - Escritura atómica vía `os.replace` + `fsync` protegida por checksum SHA-256 del contenido canónico.
   - Si falla la sonda del catálogo o la descarga del ZIP, el estado y el cursor no mutan.
   - Si falla la adquisición o la materialización de un gestor, el cursor no avanza y el siguiente ciclo
     reintenta el mismo gestor sin duplicar identidades previas.
   - Outcomes `rejected` con linaje y período coincidentes son reconocidos como terminales reutilizados,
     impidiendo que errores de formato en filings ajenos bloqueen indefinidamente el cursor.
4. **Integración con el Scheduler**:
   - Job programado `sec:institutional:13f-cycle` (`ScheduledJobDomain.EVENTS`, `data_frequency="daily-check"`,
     `run_at = config.run_at + 105 minutos`, `asset_id=None`).
   - Se incluye automáticamente si algún activo del watchlist expone un binding `sec/cusip`.

```bash
export SEC_USER_AGENT="Investment Analyst contact@example.com"
python scripts/smoke_sec_institutional_cycle.py
```
