# Corpus documental SEC primario

El corpus conserva evidencia documental oficial, no analytics, señales, recomendaciones, decisiones
ni ejecución. Cada filing, documento lógico y revisión tiene una identidad UUID5 distinta. La revisión
referencia bytes completos por SHA-256 en `storage/data/documents/sha256/`; su RawRecord sólo contiene
metadata, URL oficial y lineage al snapshot Submissions que demostró CIK, accession, form y path.

En `sec-document-revision-v1`, `available_at` conserva la semántica histórica de primera
recuperación oficial demostrada y coincide con `retrieved_at`. En
`sec-document-revision-v2`, `available_at` es la disponibilidad pública: deriva exactamente de
`filing.accepted_at`; `retrieved_at` conserva por separado la recepción local. Los replays filtran
`available_at <= known_at` en el índice RawRecord antes de materializar metadata y sólo seleccionan
evidencia v2; el contador `legacy_records_excluded` hace explícita la historia v1 excluida. Una
ausencia devuelve `missing`, nunca cero ni contenido inventado.

`sec-document-revision-v3` representa una corrección de metadata observada en un snapshot oficial
Submissions posterior cuando el mismo CIK, activo, accession y contenido conservan identidad. Sólo
se admite si `accepted_at` cambió y todos los demás campos del documento permanecen iguales. Antes
del append se verifica una vez el documento actual en Archives contra el SHA-256, tamaño y URL ya
persistidos. Una diferencia de contenido, metadata adicional, lineage ausente o ambigüedad falla
cerrado y conserva las revisiones existentes. El nuevo registro guarda `prior_revision_id`, el
digest canónico de metadata y `metadata_observed_at` del snapshot Submissions. Su disponibilidad es
`max(filing.accepted_at, metadata_observed_at, retrieved_at)`, por lo que una corrección nunca se
retrotrae a su fecha oficial corregida. V3 conserva su contrato estrictamente byte-idéntico: nunca
acepta otro hash o tamaño. La identidad y el RawRecord usan namespaces UUID5 propios de v3; las
identidades v1/v2 permanecen estables.

`sec-document-revision-v4` representa una respuesta nueva de Archives sólo cuando los bytes
completos difieren de la respuesta anterior por un único script externo terminal, con una forma muy
acotada, en un filing HTML. El elemento debe ser exactamente
`<script type="text/javascript"  src="PATH"></script>` inmediatamente antes de
`</body></html>` y el mismo whitespace ASCII final. `PATH` debe ser root-relative, tener de 1 a 255
bytes ASCII de `[A-Za-z0-9_/-]`, empezar con una sola `/` y usar segmentos no vacíos sin `.` ni
`..`. No se aceptan otros atributos, scripts inline, varios scripts terminales ni cambios en otros
bytes. El verificador omite sólo ese elemento en vistas efímeras y prueba que el resto de bytes,
tamaño y SHA-256 coincidan. El RawRecord conserva el hash y tamaño completos actuales; el hash
anterior también es explícito y cada respuesta completa tiene su blob. `SecTerminalScriptDifference`
registra el elemento exacto anterior/nuevo, offset y tamaño/hash del core. La prueba no describe el
comportamiento del script ni la equivalencia del DOM renderizado: nunca descarga ni ejecuta PATH y
no crea un tercer blob canónico.

V4 enlaza con un prior v2, v3 o v4 y conserva activo, source URL, filing e identidad documental,
salvo una corrección permitida de `accepted_at`. Su disponibilidad es
`max(filing.accepted_at, metadata_observed_at, retrieved_at, prior.available_at)`. Una v3 posterior
a v4 mantiene el mismo hash y tamaño completos de su prior. El replay valida la historia conectada
sin forks y recalcula cada prueba v4 desde ambos blobs completos después de filtrar
`available_at <= known_at`; antes del append devuelve revisión y bytes previos y después selecciona
la nueva respuesta íntegra. Si metadata no cambia, la repetición valida/reutiliza la última revisión
sin GET Archives ni RawRecord/blob documental equivalente; Submissions sí se vuelve a consultar.

El replay acepta v2, v3 y v4; v1 sigue excluida y contabilizada explícitamente. Los backups de
formatos 1 y 2 conservan priors, snapshots Submissions y los dos blobs completos de v4 y vuelven a
verificar la prueba en restore. Lectores anteriores a v3/v4 no soportan esos schemas; se requiere
una release que los entienda. El formato del workspace y las identidades v1/v2 no cambian.

Los documentos presentados por un declarante que no es un activo del catálogo usan la revisión
hermana `sec-filer-document-revision-v1`. Conservan el mismo `SecFiling` y
`SecLogicalDocument`, pero sustituyen el vínculo `asset_id` por el `filer_cik` ya declarado en el
filing. Sus namespaces UUID5 y su codec son disjuntos: una revisión de declarante no se convierte en
`sec-document-revision-v1/v2`, y esas revisiones históricas permanecen sin cambios. La familia de
declarante nace directamente con `available_at == filing.accepted_at` y no tiene una variante de
disponibilidad basada en recuperación.

La familia financiera v1 se limita a `10-K`, `10-K/A`, `10-Q`, `10-Q/A`, `20-F`, `20-F/A`, `40-F`
y `40-F/A`. El corpus compartido también reconoce las familias documentales de Sección 16,
13D/13G y `13F-HR`/`13F-HR/A`; cada vertical mantiene contratos derivados y source IDs separados.
El único provider es SEC EDGAR oficial: Submissions ya persistido descubre filing/path y
`www.sec.gov/Archives` entrega el documento primario con HTTPS, host, redirect, tamaño y hash
verificados. No hay fallback ni extracción de hechos, fragments, XBRL, métricas o diagnósticos.

## Operación temporal

Configura una identidad SEC no secreta sólo en el proceso y usa un workspace temporal:

```bash
export SEC_USER_AGENT="Investment Analyst contact@example.com"
python scripts/import_sec_document_corpus.py --workspace /tmp/sec-corpus --asset-id equity:us:aapl --form 10-K
python scripts/query_sec_document_corpus.py --workspace /tmp/sec-corpus --asset-id equity:us:aapl --known-at 2026-01-01T00:00:00Z --form 10-K
```

La consulta abre almacenamiento de solo lectura, no crea directorios, no abre writer y no llama a
SEC. `--read-content` verifica y lee explícitamente los bytes, pero nunca los imprime. Backup/restore
conserva IDs, blobs y replay sin una segunda pasada completa: la validación documental se integra en
el escaneo RawRecord paginado existente.

## Refresh incremental de documentos primarios

`sec-primary-document-refresh-v1` es un contrato de aplicación separado de los fundamentales. Su
request estricto sólo admite `asset_id`; hace una comprobación dedicada de Submissions, sin descargar
Company Facts, evalúa en orden los
forms declarados por `SecAssetConfiguration` (incluidos `/A`) y elige el filing más reciente de cada
form compatible. La cobertura sólo significa que cada accession seleccionada tiene una última
revisión v2, v3 o v4 con lineage y hashes de blobs completos verificados, además de prueba byte-exact
para v4; no afirma cubrir toda la historia SEC.

Antes de solicitar Archives, el pipeline busca por `asset_id` y accession. Cero revisiones permite
un GET; metadata sin cambios valida y reutiliza la última revisión compatible sin GET; un cambio de
`accepted_at` requiere un GET actual y crea v3 sólo con bytes idénticos o v4 sólo con diferencia del
script terminal probada. Historias múltiples/forked, metadata contradictoria o blob, lineage o
prueba inválidos fallan cerrado. Las reruns reutilizan las accessions intactas y un snapshot con
accession nueva sólo descarga ese delta. No produce observaciones, métricas, diagnósticos,
screening, texto extraído, embeddings, score ni rutas HTTP/UI.

La comprobación real aislada usa la identidad local, nunca la imprime ni escribe en el workspace
permanente:

```bash
set -a; source .env; set +a
PYTHONPATH=src .venv/bin/python scripts/smoke_sec_document_refresh.py
```

El summary conserva `submissions_checked_at` de cada comprobación fresca separado de
`submissions_record_available_at` del RawRecord que aporta lineage. El primer refresh puede obtener documentos oficiales; el segundo debe informar
`second_document_fetches=0`. El delta sintético equivalente se cubre por la prueba unitaria del
contrato incremental.

## Refresh incremental de actividad declarada

`sec-declared-activity-refresh-v1` comparte el mismo primitivo de snapshot que el refresh
documental: un colaborador tipado persistente y verificado —extraído de la semántica ya integrada por
`SEC-CORPUS-25`— hace exactamente un GET Submissions por activo y ejecución y cero GET a Company
Facts. Sobre ese snapshot, la política `sec-declared-activity-selection-v1` elige la evidencia
declarada pendiente de Forms 3/4/5 y Schedules 13D/13G, importa ambas familias con los pipelines
existentes y completa las capas 2 y 3 al mismo corte.

La selección es incremental y auditable: sin evidencia terminal previa sólo se selecciona el
accession más reciente de cada formulario exacto; con watermark, sólo el delta posterior no procesado,
en orden de aceptación ascendente y limitado a 25 accessions por familia y ejecución. El backlog se
declara (`backlog_count`, `coverage_complete=false`) y la ejecución siguiente continúa sin saltos. Los
rechazos terminales versionados no se vuelven a descargar; el estado parcial, un outcome aceptado sin
statement o un fallo de parser/storage no avanzan el watermark y se reanudan, sin borrar el progreso
ya persistido por accession.

La comprobación real usa un workspace temporal y no imprime ni persiste la identidad SEC:

```bash
set -a; source .env; set +a
PYTHONPATH=src .venv/bin/python scripts/smoke_sec_declared_activity_refresh.py
```

El smoke ejecuta el refresh dos veces sobre un activo SEC, informa un GET Submissions por ejecución,
llamadas Archives acotadas en la primera y `archives_calls=0` en la repetición, y verifica con una
consulta read-only que los statements persistidos son visibles en el corte declarado.

## Línea temporal y búsqueda local

Para la búsqueda transversal, enumeración y ordenación point-in-time de revisiones documentales
persistidas en ambas familias (`asset_document` y `filer_document`), consulta
[Línea temporal y búsqueda local SEC](sec_document_timeline.md).
