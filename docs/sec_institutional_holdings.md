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
