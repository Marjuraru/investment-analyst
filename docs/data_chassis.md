# Ruta DATA-CHASSIS: Chasis de Datos y Almacenamiento

Este documento gobierna la ruta técnica transversal `DATA-CHASSIS`.
Define la arquitectura objetivo, el baseline inicial, las metas provisionales de contención y
eficiencia, la secuencia de etapas y las restricciones operativas permanentes para la transformación
del chasis de almacenamiento de `investment-analyst`, sin degradar la integridad point-in-time, la
trazabilidad append-only ni la evidencia histórica persistida.

## Evidencia viva de la necesidad (verificada el 2026-09-12 sobre `b0c41cd`)

1. **Amplificación de identidades por `known_at` en mercado:**
   `analytics/market/statistics_identity.py:20` incluye `known_at` en el preimage UUID5 de cada
   `MetricResult` de mercado: un corte nuevo sin evidencia nueva produce identidades nuevas.
2. **Amplificación de identidades por `parameters` y lineage O(n) en derivados:**
   `analytics/crypto/derivatives_identity.py` declara excluir `known_at`, pero
   `analytics/crypto/derivatives_engine.py:390` lo inyecta en `parameters`, que sí entra en la
   identidad determinista: mismo efecto por otra vía. Cada `funding.sum_1h` y `funding.mean_1h`
   persiste además su lista completa de `input_observation_ids` (hasta 720 UUIDs) en `document_json`.
3. **Patrón de persistencia no batcheado:**
   `analytics/crypto/derivatives_pipeline.py:74` y `analytics/market/statistics_pipeline.py:115`
   hacen `save` seguido de `get` por fila; `derivatives_pipeline.py:102` vuelve a leer cada resultado
   y cada observación uno a uno para verificar.
4. **Semilla y recurrencia dependientes de la ventana:**
   `analytics/market/statistics_definitions.py:136`: "The seed and recurrence use only bars selected
   by the point-in-time query" — EMA/RSI/ATR/MACD dependen del inicio de la consulta y no son
   estables incrementalmente.
5. **Doble persistencia raw:**
   `storage/raw_records.py` escribe el archivo raw en filesystem **y** `raw_record_index.document_json`
   con el mismo documento completo.
6. **Journal operacional sin rotación acotada:**
   `application/multi_asset_scheduler.py:28,470`: `attempts` validado con `max_length=100_000`;
   alertas/candidatos con `250_000`. Documentos JSON reescritos completos.
7. **Presión sobre el almacenamiento físico del host:**
   Espacio físico en disco `C:` bajo Windows con ~49-55 GB libres, con un VHD de WSL que no devuelve
   espacio a `C:` automáticamente al borrar archivos dentro de Linux.

## Arquitectura objetivo

Proveedores oficiales → evidencia inmutable versionada (raw una vez por contenido) → hechos
normalizados PIT únicos → métricas semánticas únicas (inputs + algoritmo + parámetros, sin duplicar
por corte) → snapshots PIT livianos (`known_at` + hashes + referencias) → features y diagnósticos
(DuckDB + Parquet particionado) → screening/alertas, investigación predictiva y
`ModelExplanationPacket` → LLM opcional (narrativa, citas). El LLM nunca lee tablas arbitrarias,
recalcula, modifica evidencia ni recibe series completas.

## Baseline y metas

Baseline del auditor (referencia inicial, sustituido y medido en bytes exactos en `docs/data_chassis_baseline.md`):
workspace ~7,2 GB; DuckDB ~6,09 GB; métricas 1.119.644 filas; JSON de métricas 4,50 GB; funding
sum+mean 3,75 GB; crecimiento lógico ~168 MB/día; jobs sin evidencia nueva 17–125 s; `C:` libre
64,81 GB.

Unidades normativas: todo baseline y meta se expresa en bytes exactos. Las unidades binarias (GiB) y
decimales (GB) no se mezclan.

Metas provisionales (se fijan como gates definitivos sólo tras la telemetría de `DATA-CHASSIS-1`):
- Workspace activo reconstruido ≤3 GB o −55 %.
- Métricas −90 % en volumen sin pérdida semántica.
- Funding −90 % en bytes.
- Crecimiento ordinario ≤30 MB/día con el universo actual.
- Job sin evidencia nueva −80 % en latencia/duración.
- Cero duplicados causados sólo por `known_at`.
- Incremental idéntico a completo.
- PIT reproducible.
- Backup/restore desde Google Drive probado.
- Interfaces de features y explicación independientes del storage físico.

La meta de ≤3 GB se refiere al workspace **activo**. Como v1 se conserva congelado para rollback, el
consumo físico durante y después de la fase es v1 + v2; la retirada de v1 requiere una decisión HUMAN
separada, fuera de esta ruta. Además, liberar espacio dentro de WSL no devuelve bytes a `C:` sin
compactar el VHD (operación HUMAN en Windows); todo gate de espacio físico se mide en `C:`, no en `df /`.

## Etapas y orden de ejecución

El crecimiento medido está en DuckDB (métricas de mercado y derivados con 0 % de reutilización), no
en los JSON operativos, que hoy son pequeños y tardarían cientos de días en alcanzar el tope de
100.000 intentos. El journal operacional se ubica en la etapa 6 para atacar primero el mayor
consumidor.

| Etapa | Nombre | Objetivo y alcance esencial | Gate esencial | Policy |
| --- | --- | --- | --- | --- |
| 0 | Contención y baseline | Backup OPS-8 verificado y restaurado; baseline en bytes sobre la copia restaurada; espacio físico; copia de auditoría; registro de ruta. | Restore correcto con hashes y conteos; baseline reproducible; margen registrado. | HUMAN |
| 1 | Observabilidad de almacenamiento | Por job: bytes físicos antes/después, bytes lógicos por tabla, filas creadas/reutilizadas, WAL, duración de red/consulta/cálculo/persistencia/verificación; ventanas 7/30 días; nuevos vs revisiones vs derivados; alerta de presupuesto; informe read-only reproducible desde writer o backup. Sin cambios de identidad, borrado, fórmulas, UI ni schema. | Ningún delta desconocido si el transporte puede medirlo; etapas reconciliables con duración total; lógico y físico separados; overhead medido; snapshot diario compacto. | STANDARD/AUTO sólo si la telemetría es un artefacto aditivo separado; HUMAN si añade campos a un contrato persistido existente (p. ej. estado del scheduler). |
| 2 | Persistencia y verificación por lotes | Particionada en dos bloques: `DATA-CHASSIS-4` (capa de acceso y persistencia por lotes con recibos) y `DATA-CHASSIS-5` (adopción en pipelines y migración de dobles). `get_many`; conflictos en memoria; inserción por lotes acotados; recibos de escritura en vez de conteos globales. Se conservan todas las validaciones (activo, fuente, tipos, UTC, Decimal, `available_at <= known_at`, identidad, dependencias, DAG, conflicto por mismo ID con contenido distinto). | Resultados, IDs y diagnósticos idénticos a v1; cero cambio de schema o algoritmo; ≥10× en fixture; un lote fallido no elimina lotes previos válidos. El gate de −80 % real en jobs de reutilización queda reasignado a verificación post-despliegue por decisión humana explícita del 2026-09-16. | STANDARD, strict allowlist (storage). |
| 3 | Identidad de métrica v2 y `AnalysisSnapshot` | `result_id` v2 = UUID5 de activo, `metric_key`, `as_of`, `available_at`, `algorithm_version`, parámetros semánticos, `input_observation_ids`, `input_metric_result_ids`, unidad y calidad; excluye `known_at`, `computed_at`, parámetros de ejecución e identificadores de job. `value` fuera de la identidad: mismo ID con otro valor es conflicto/no determinismo. Snapshot: `snapshot_id`, activo o `universe_snapshot_id`, dominio, `known_at`, `evidence_set_hash`, `metric_result_ids`, `diagnostic_result_ids`, `policy_version`, `created_at`. v1 intacto, adaptador de lectura, sin reasignar IDs. | Mismos inputs → mismo ID; revisión → resultado nuevo; snapshots resuelven sus métricas; PIT reconstruible; referencias desde alertas, diagnósticos, valoración y backtests probadas. El gate "nuevo `known_at` sin evidencia nueva crea cero métricas" aplica a métricas de ventana finita y derivados; para EMA/RSI/ATR/MACD sólo se cumple tras la etapa 5. `known_at` debe salir también de `parameters` (caso Deribit). El PLAN de esta etapa evalúa primero si `DiagnosticResult`, que ya es un manifiesto por corte con `metric_result_ids`, puede extenderse antes de crear un contrato aislado. | HUMAN |
| 4 | Lineage compartido de derivados (`EvidenceSet`) | Primera vertical funding Deribit: `evidence_set_id`, `canonical_hash`, `ordered_input_ids`, `input_count`, primera/última observación, `source_id`, `available_at`; ventanas 24/168/720 almacenadas una vez y compartidas por sum y mean y entre cortes; segmentos o árboles de hashes opcionales si se materializan y verifican todos los inputs; contrato preparado para otras métricas rolling. | Mismos Decimal, `as_of` y `available_at`; lineage completo recuperable; −90 % bytes funding; ningún array de 720 UUID repetido; corrupción detectada por hash; diagnóstico Deribit idéntico. | HUMAN |
| 5 | Indicadores incrementales | Semilla canónica por activo, fuente, frecuencia y versión; estado recursivo persistible; checkpoints con hash del prefijo; continuación desde el último checkpoint válido; corrección histórica invalida checkpoints posteriores; ventanas finitas recalculan sólo el rango afectado; nuevo `algorithm_version`, v1 no se reescribe. Casos: completo vs incremental, distintos `start`, barra nueva, corrección histórica, split/ajuste retroactivo, calendarios irregulares, equity/ETF/cripto, interrupción, Decimal. | Igualdad exacta full vs incremental v2; valor estable sin evidencia nueva; ≤20–30 resultados nuevos por activo/día; revisión recalcula sólo lo dependiente; ningún v1 presentado como v2. El lineage de indicadores recursivos se expresa como checkpoint + delta (reutilizando `EvidenceSet`), no como lista O(n) de todo el prefijo. | HUMAN |
| 6 | Journal operacional acotado | Journal append-only segmentado + snapshot compacto + segmentos históricos con hash + rotación verificable; sin reescrituras completas; retirar el tope terminal de 100.000 intentos; resumen operativo separado de auditoría; lectura de estados v1. | Escritura independiente del tamaño histórico; crash recovery en cada frontera; v1 legible sin reescritura; backup incluye snapshot y segmentos; ningún intento desaparece. | HUMAN |
| 7 | Raw y columnar v2 | Raw una vez por contenido (`raw/sha256/`); índice DuckDB con ruta, hash y columnas consultables; retiro gradual de `raw_record_index.document_json`; observaciones apuntan a raw + coordenada; posición 13F columnar referenciando la revisión documental; Parquet inmutable con manifest SHA-256 de particiones; DuckDB como catálogo e índices y datos calientes; minutario reciente caliente e historia en Parquet, sin destruir datos. Puede dividirse en sub-bloques (raw, 13F, minutario) si el diff supera S3. | Raw lineage completo; 13F sin copias masivas de metadata; lectura PIT idéntica antes y después; partición faltante o corrupta falla explícitamente; independiente del cwd; ningún symlink escapa. | HUMAN, nueva versión de workspace |
| 8 | Rebuild y cutover | Nunca in-place: detener writer, backup verificado, workspace v2 nuevo, importar raw preservando hashes y `available_at`, reconstruir observaciones, métricas v2 y snapshots, comparar v1/v2, backup/restore de v2, activación atómica, v1 congelado. El minutario persistido histórico se excluye formalmente de la reconstrucción hacia v2. | Inventario completo; revisiones preservadas; equivalencia exacta donde no cambió algoritmo; recursivos v2 contra baseline independiente; cortes representativos y fronterizos por activo (la cifra de cortes se re-mide, no se asume); UI/API con contratos o adaptadores; presupuesto cumplido; rebuild repetido con mismos hashes; rollback probado; ≥25 GB físicos libres en `C:` antes de empezar. | HUMAN obligatoria |
| 9 | Capa de acceso analítico | `EvidenceSnapshotQuery`, `FeatureSnapshotQuery`, `MetricSeriesQuery`, `CandidateEvidenceQuery`, `ModelExplanationPacketQuery`; proyección, paginación, feature sets versionados, snapshots Parquet PIT, manifest, universo histórico, ausencia explícita, sin leakage. `FeatureSnapshot` con `feature_set_version`, `known_at`, `universe_snapshot_id`, `rows`, `feature_available_at`, `source_snapshot_ids`, `schema_hash`, `content_hash`. `ModelExplanationPacket` sólo con candidato/predicción, baseline, features, atribuciones, calidad/frescura, calibración/incertidumbre, evidencia y citas, limitaciones y versiones. | Mismo snapshot y feature set → mismo hash; pruebas de leakage; paquete reproducible; límite de tamaño/tokens configurable; funcionamiento completo sin LLM; adaptador desacoplado del proveedor; toda cifra del LLM referencia evidencia estructurada. No incorpora modelos ni proveedor LLM. | HUMAN |
| 10 | Backup cloud y lifecycle | Google Drive como destino de backup, nunca filesystem del DuckDB activo; chunks content-addressed deduplicados, compresión, cifrado antes de salir, manifest hash-bound, upload reanudable, verificación posterior, restauración temporal, retención 7/4/12, sin secretos, alertas de espacio y backup vencido. Cifrado y cliente Drive exigen autorización explícita de dependencias y credenciales. | Restore completo desde Drive en destino vacío; hashes y conteos exactos; pérdida local simulada; incremental no re-sube todo el DuckDB; credenciales aisladas; rollback documentado. | HUMAN |

Después de la fase: `CRYPTO-DATA-DISCOVERY` / `ASSET-EXPANSION` → `PREDICTIVE-RESEARCH` → LLM cualitativo opcional.

## Etapa 1 entregada: instrumento, lectura y cableado (`DATA-CHASSIS-1`, `DATA-CHASSIS-2`, `DATA-CHASSIS-3`)

La etapa 1 queda entregada por tres bloques: `DATA-CHASSIS-1` aportó el instrumento, `DATA-CHASSIS-2`
la lectura y `DATA-CHASSIS-3` el cableado en la composición de producción. El siguiente bloque es
`DATA-CHASSIS-4` (etapa 2 — persistencia y verificación por lotes).
El artefacto es **aditivo y operacional**: cuelga de `state_root` como `storage_observability_v1.jsonl`
y no toca `StoragePaths`, el layout de `storage/`, el schema DuckDB ni ningún contrato persistido
existente más allá de sus propios campos opcionales.

### Instrumento (`DATA-CHASSIS-1`)

`DATA-CHASSIS-1` entrega la mitad medible de la etapa 1: el instrumento, no el informe.

- **Contrato aislado nuevo:** `storage-observability-v1` (registro por intento) y
  `storage-observability-daily-snapshot-v1` (agregado diario compacto), tipados, `frozen`,
  `extra="forbid"` y sin `Any`. La correlación con la historia existente usa `attempt_id` y `job_id`;
  las filas creadas/reutilizadas se copian por referencia desde la ejecución del intento y **no**
  redefinen `created_count`/`reused_count` de `provider-job-telemetry-v1`.
- **Hechos medidos por job:** bytes físicos del DuckDB y del WAL antes y después, medidos sobre el
  sistema de archivos como enteros exactos; bytes lógicos por tabla con el motor abierto en
  `read_only=True` y `octet_length(encode(document_json))` (bytes UTF-8 exactos; el baseline publicó
  `strlen`, equivalente para documentos ASCII); y el desglose de duración en
  red/consulta/cálculo/persistencia/verificación, medido con **un solo reloj** y reconciliado de forma
  exacta con la duración total del ciclo.
- **Cota explícita:** 90 snapshots diarios retenidos. Los registros del día abierto se anexan sin
  reescribir el archivo completo y sólo se pliegan al snapshot compacto cuando el día UTC cierra.
- **No intrusivo:** el colector abre el motor únicamente en `read_only=True`, no introduce una
  segunda conexión de escritura y un fallo suyo nunca degrada ni aborta el job medido: se registra
  como issue operativo del scheduler.
- **Límite de atribución declarado:** `network_ms` mide la ventana de ejecución del callable del job,
  donde ocurre el trabajo de transporte; una llamada opaca no es sub-atribuible desde esta superficie
  sin instrumentar los pipelines, que este bloque no toca.
- **Cableado pendiente:** la allowlist estricta del bloque no incluye la composición de producción
  (`scripts/serve_investment_analyst.py`), de modo que el colector se inyecta explícitamente en el
  scheduler y su cableado operativo queda pendiente. `DATA-CHASSIS-2` cierra la etapa 1 con la lectura
  de ese artefacto, pero tampoco toca la composición: el runtime desplegado sigue por detrás de `main`
  (cuatro merges al publicar este bloque), el artefacto tiene cero instancias persistidas y el cableado
  es una acción operativa separada, no un criterio de esta ruta. **Cerrado por `DATA-CHASSIS-3`:** la
  composición ya inyecta el colector; desplegar el runtime sigue siendo una acción operativa del humano.

### Lectura (`DATA-CHASSIS-2`)

- **Clasificación del crecimiento:** cada registro clasifica las filas creadas por el intento en
  evidencia nueva (`raw_record_index`, `normalized_observations`), derivados (`metric_results`,
  `diagnostic_results`) y revisiones, más las filas observadas fuera de esas dos familias. La partición
  es exacta y verificable: las cuatro cuentas suman las filas creadas que el intento reportó. El
  colector mide el conteo exacto de filas por tabla antes de la ejecución y al cerrarla, con el motor
  en `read_only=True`. Una revisión es una fila creada que reescribió una identidad sin agregar fila
  alguna; por eso se captura al escribir y no se reconstruye después en el informe. Si el intento no
  reporta filas creadas, o si las tablas crecieron más de lo que el intento declara haber creado, la
  clasificación **se omite** en vez de inventarse.
- **Overhead propio medido:** `collector_overhead_ms` registra, en milisegundos y con el mismo reloj
  único, la parte de la ventana medida que consumió el propio instrumento (lectura, compactación y
  verificación), separada de `network_ms`, que mide la ejecución del job. La relación
  `collector_overhead_ms + network_ms == total_ms` se valida en el contrato.
- **Campos opcionales:** la extensión de `storage-observability-v1` es aditiva y con valor por
  defecto; un registro escrito sin ellos sigue parseando sin error. El contrato diario
  `storage-observability-daily-snapshot-v1` conserva exactamente sus campos.
- **Informe read-only:** `storage-observability-report-v1` es tipado, `frozen`, `extra="forbid"`, con
  `schema_version` literal y sin `Any`. Calcula ventanas inclusivas de 7 y 30 días sobre los snapshots
  diarios ya persistidos y declara de forma explícita cada día sin snapshot: nunca interpola, nunca
  rellena con cero y nunca promedia sobre días inexistentes (`mean_daily_bytes_delta` es `None` cuando
  la ventana no tiene días declarados).
- **Alerta de presupuesto operacional:** compara el crecimiento diario medido (bytes físicos del
  DuckDB más WAL) contra un umbral configurable — 30.000.000 bytes por defecto, la meta provisional de
  este documento — sobre los días declarados de la ventana corta, e identifica los días excedidos y el
  pico medido. Es un umbral operacional: no crea candidato, no toca la outbox, no produce señal ni
  recomendación y no alimenta el motor de alertas de producto.
- **CLI read-only:** `scripts/report_storage_observability.py` acepta `--workspace`, `--as-of` y
  `--budget-bytes-per-day`, imprime JSON por stdout, no escribe nada en el workspace y devuelve `0`
  dentro de presupuesto, `3` con presupuesto excedido y `2` ante entrada inválida o artefacto
  inutilizable.
- **Sin motor:** el informe no abre DuckDB en ningún modo; sólo lee el artefacto JSONL bajo el
  `state_root` declarado. Por eso funciona igual contra el `state_root` del writer o contra el de una
  copia restaurada, y no depende del directorio de trabajo.
- **Límites declarados:** el día abierto todavía no es un snapshot diario, de modo que aparece como
  ausencia declarada hasta que cierra, y `unfolded_record_count` informa cuántos registros esperan
  plegarse; la clasificación del crecimiento vive en el registro por intento y **no** se pliega en el
  agregado diario, de modo que su lectura histórica por ventanas queda para un bloque posterior; el
  árbol de decisión de dos familias (`raw_record_index`/`normalized_observations` frente a
  `metric_results`/`diagnostic_results`) es explícito y el resto de las tablas medidas queda en la
  cuenta de filas fuera de familia.
- **Gate de etapa todavía provisional:** con el instrumento, la lectura y el cableado ya entregados,
  las metas de contención siguen sin fijarse como gates definitivos porque el artefacto no tiene
  instancias persistidas en el runtime desplegado; se fijarán con telemetría real, no con supuestos.

### Cableado en la composición de producción (`DATA-CHASSIS-3`)

`DATA-CHASSIS-3` es el bloque de composición que activa el instrumento ya auditado. No modifica el
colector, el informe ni el scheduler: **usa** lo entregado.

- **Punto de inyección existente:** `MultiAssetScheduler(storage_observability=...)` ya existía desde
  `DATA-CHASSIS-1` y permanece opcional e intacto; el bloque sólo lo provee desde la composición.
- **Rutas ya resueltas:** la composición resuelve `WorkspacePaths` una sola vez y propaga a
  `_serve_after_lock` el `state_root` y el `storage_root` que ya obtenía; el `database_path` se deriva
  con `StoragePaths.from_root`, el mismo camino que usan el runtime y el servicio de workspace. No se
  introduce una resolución de rutas nueva ni se duplica el layout en el script.
- **Artefacto:** el colector escribe `storage_observability_v1.jsonl` bajo el `state_root` declarado,
  con ruta absoluta derivada de ese `state_root`, de modo que el destino no depende del directorio de
  trabajo. Un arranque no escribe nada: el registro aparece cuando un job se ejecuta.
- **Sin colector y con colector fallido:** el scheduler compuesto sin colector sigue siendo válido y no
  escribe artefacto, y un fallo del colector no impide el arranque ni altera el resultado del job; se
  registra como issue operativo del scheduler.
- **Alcance estricto:** una sola ruta de `scripts/`, una prueba de integración nueva y los dos
  documentos de ruta. Cero cambios en `src/investment_analyst/`, cero dependencias, cero cambios de
  schema, migraciones, identidades, fórmulas o contratos de salida.
- **Secuencia, no preferencia:** el cableado va antes de la etapa 2 porque el gate `−80 % real en jobs
  de reutilización` exige una línea base medida antes del cambio de persistencia; sin el cableado esa
  línea base no llega a existir. La etapa 2 pasa a `DATA-CHASSIS-4`.

## Etapa 2: Persistencia y verificación por lotes (`DATA-CHASSIS-4`, `DATA-CHASSIS-5` y `DATA-CHASSIS-17`)

La etapa 2 ataca el patrón de persistencia no batcheado identificado en el baseline. Se entrega
particionada en tres bloques por decisión de PLAN verificada en vivo:

1. **`DATA-CHASSIS-4`:** construyó la capa de acceso y persistencia por lotes en la
   capa de almacenamiento (`get_many`, `save_many`, detección de conflictos en memoria y recibos
   tipados de escritura `BatchWriteReceipt`) en `ObservationRepository`, `MetricResultRepository` y
   `DiagnosticResultRepository`, sin modificar ningún llamador ni doble de test y dejando `analytics/**`
   intacto.
2. **`DATA-CHASSIS-5`:** adopción de la API por lotes en `analytics/crypto/derivatives_pipeline.py` y
   `analytics/market/statistics_pipeline.py`, eliminación del `get` posterior a `save`, memoización del
   grafo de dependencias por corrida, verificación profunda sólo de filas nuevas o conflictivas mediante
   `BatchWriteReceipt` y migración de los dobles de prueba en los tests que consumen esos contratos.
3. **`DATA-CHASSIS-17`:** extiende la persistencia por lotes al almacén de registros crudos (`JsonRawRecordRepository.save_many`) con chunks acotados (`_RAW_RECORD_BATCH_CHUNK_SIZE = 1_000`), verificación de existencia en una sola consulta SQL por lote e inserción atómica multi-fila en DuckDB. Adopta esta API en `InstitutionalHoldingsRepository.save_positions` y `sec_institutional_holdings_pipeline.py` para ingesta eficiente de carteras 13F, y corrige el defecto de ordenamiento determinista en `institutional_metric_engine.py` (`_position_sort_key`) ante valores `None` en `put_call`.

### Reasignación del gate `−80 % real`

Por decisión humana explícita del 2026-09-16, el gate de `−80 % real en jobs de reutilización` de la
etapa 2 queda **reasignado a verificación post-despliegue**. Al ser una observación empírica de
producción sobre el scheduler compuesto, ningún diff ni suite de CI puede satisfacerla de forma
aislada en el repositorio; se verificará con `scripts/report_storage_observability.py` comparando
las ventanas antes y después de desplegar `DATA-CHASSIS-5`. La etapa 2 no se declarará cerrada hasta
dicha verificación.

### Extensión a registros crudos y posiciones 13F (`DATA-CHASSIS-17`)

`DATA-CHASSIS-17` (#265) amplía la persistencia por lotes de la Etapa 2 al almacén de registros crudos y al flujo de ingesta de posiciones institucionales 13F, resolviendo además un defecto operacional de ordenamiento en el motor analítico de Cazatiburones:

1. **Persistencia por lotes en `JsonRawRecordRepository` (`src/investment_analyst/storage/raw_records.py`):**
   - Implementa `save_many(records: Sequence[RawRecord]) -> BatchWriteReceipt` con particionamiento en chunks acotados de tamaño fijo (`_RAW_RECORD_BATCH_CHUNK_SIZE = 1_000`).
   - Por cada chunk, ejecuta una única consulta SQL `SELECT` con placeholders acotados para recuperar los registros existentes y compara el payload exacto en memoria, garantizando idempotencia byte a byte.
   - Escribe atómicamente en disco los archivos JSON de los registros nuevos antes de la inserción en base de datos.
   - Inserta las filas nuevas mediante una única sentencia `INSERT INTO raw_records VALUES (?, ...), (?, ...)` por chunk en DuckDB, reduciendo drásticamente los viajes de ida y vuelta a la base de datos y la I/O de disco.
   - Mantiene semántica fail-closed: ante un `RecordConflictError`, preserva el progreso de los chunks previos completados exitosamente e interrumpe la ejecución sin aplicar el chunk conflictivo.

2. **Adopción en `InstitutionalHoldingsRepository` y pipeline 13F:**
   - En `src/investment_analyst/evidence/sec_institutional_holdings/repository.py`, introduce `save_positions(positions: Sequence[InstitutionalPositionRecord]) -> BatchWriteReceipt`, mapeando cada posición a su correspondiente `RawRecord` y delegando en `save_many`. Captura `RecordConflictError` y lo reempaqueta limpiamente en `InstitutionalHoldingsRepositoryError`.
   - En `src/investment_analyst/providers/institutional_holdings/sec_institutional_holdings_pipeline.py`, reemplaza el bucle iterativo de guardado individual de posiciones por la invocación en lote `holdings.save_positions(positions)`, eliminando miles de transacciones individuales durante la ingesta de filings 13F con carteras voluminosas.

3. **Corrección de ordenamiento determinista en `institutional_metric_engine.py`:**
   - Resuelve el defecto operacional `TypeError: '<' not supported between instances of 'str' and 'NoneType'` que ocurría al procesar filings 13F con posiciones donde `put_call` es `None` mezcladas con opciones de compra/venta (`"PUT"` / `"CALL"`).
   - Implementa la clave de ordenamiento `_position_sort_key` que mapea `put_call` a una tupla booleana y cadena `(p is not None, p or "")`, asegurando que `None` se ordene deterministamente antes que cualquier cadena sin comparar tipos incompatibles.

## Etapa 3: Identidad de métrica v2 y adaptador de resolución (`DATA-CHASSIS-6`, `DATA-CHASSIS-7`, `DATA-CHASSIS-8` y `DATA-CHASSIS-9`)

`DATA-CHASSIS-6` (este bloque) abre la etapa 3 definiendo la regla canónica de identidad de métrica v2 y su adaptador de resolución v1/v2 como contrato puro, aislado y disjunto, sin llamador en producción y preservando intactos los siete módulos de identidad vigentes, los motores y los pipelines.

### Evaluación y rechazo de la extensión de `DiagnosticResult`

PLAN evaluó sobre `core/models/diagnostic.py` si `DiagnosticResult` podía extenderse como manifiesto de referencias por corte antes de crear un contrato aislado:
- `DiagnosticResult` impone `final_score: Decimal` en `[0, 100]`, `confidence: Decimal` en `[0, 1]`, `verdict: DiagnosticVerdict` y `summary: NonEmptyStr`, con validadores estrictos que exigen que los pesos sumen 1 y que el score sea la suma ponderada.
- Un snapshot analítico es un manifiesto descriptivo de corte: no emite veredicto ni juicio de scoring agregado. Forzarlo a declarar un veredicto o score compuesto violaría el principio permanente de `AGENTS.md` que prohíbe colapsar análisis en scores agregados arbitrarios.
- La extensión queda rechazada por razones de contrato.

### Separación de `AnalysisSnapshot` por ausencia de runner de migración

`AnalysisSnapshot` requiere una tabla física nueva en DuckDB. La capa de almacenamiento actual (`storage/duckdb_store.py`) define `SCHEMA_VERSION = 1`, apertura fija contra `001_initial.sql` con igualdad estricta y sin runner de migración. Modificar el schema en este momento rompería la apertura del workspace permanente y violaría la restricción 5 de la ruta (no mezclar migración física con cambios de identidad/fórmulas). Por decisión humana explícita del 2026-09-17, el snapshot se separa de esta etapa y se abordará cuando se introduzca un runner de migración o en la versión nueva de workspace de la etapa 7.

### Adaptador de lectura consciente de versión (`DATA-CHASSIS-7`)

`DATA-CHASSIS-7` entrega la mitad de **lectura** de la etapa 3 y no emite una sola identidad v2. Retirar
`known_at` de `parameters` —requisito ineludible de la adopción, porque si no la fila v2 cambia de
contenido entre cortes conservando el mismo identificador— rompe **cinco** puntos de lectura en tres
subsistemas, y dos de ellos fallan **en silencio**:

| # | Sitio | Efecto con una fila v2 |
| --- | --- | --- |
| 1 | `analytics/market/diagnostic_selection.py` | `InvalidMetricContextError`; además es hoy el único deduplicador de revisiones v1. |
| 2 | `analytics/market/diagnostic_models.py` | `ValueError`. |
| 3 | `analytics/market/diagnostic_pipeline.py` | `MarketDiagnosticTraceabilityError`. |
| 4 | `alerts/analytical_monitor.py` | **Silencioso:** la métrica se descarta sin error y la alerta de mercado deja de emitirse. |
| 5 | `alerts/analytical_backtest.py` | **Silencioso:** el conjunto de cortes de mercado queda vacío y el backtest produce cero resultados. |

- **Contrato aislado nuevo:** `analytics/metric_identity_cut.py` responde exactamente una pregunta —dado
  un `MetricResult` y un `known_at`, ¿es elegible para ese corte, y bajo qué versión de identidad?— con
  una función pura, total y sin E/S. `metric_identity_v2.py` se **consume tal cual** y queda fijado por
  hash: el bloque usa la regla v2 auditada, no la redefine.
- **Identidad del dominio:** la versión se resuelve sólo desde el `result_id` (UUID5 → v1, UUID8 → v2) y
  cualquier otro UUID degrada a la regla legada sin lanzar, de modo que ninguna fila histórica de otro
  dominio se vuelve ilegible.
- **Reglas:** v1 conserva `parameters["known_at"]` obligatorio con las mismas clases de error y los
  mismos mensajes; v2 es elegible por `available_at <= known_at` —la regla que el dominio fundamental ya
  aplicaba— y una fila v2 que todavía contenga `known_at` en `parameters` falla cerrado.
- **Desambiguación:** ante una revisión v1 y una v2 del mismo corte y la misma `available_at` se prefiere
  v2 de forma determinista y probada; dos candidatos de la misma versión con la misma `available_at`
  siguen siendo `AmbiguousMetricRevisionError`.
- **Alcance:** cero tablas, índices, migraciones o bump de `SCHEMA_VERSION`; cero reescritura de filas;
  cero cambios en el motor, el pipeline y los módulos de identidad de mercado y cripto; la regla no se
  amplía a cripto, fundamentales, valoración ni cazatiburones.
- **Semántica PIT que habilita:** bajo v2, un corte antiguo `K` puede seleccionar una fila cuya evidencia
  estaba disponible (`available_at <= K`) aunque se calculara después; `AGENTS.md` lo autoriza de forma
  explícita.

El orden no es preferencia: la puerta de un solo sentido —escribir identificadores que no se pueden
reescribir— se cruza con el lector ya preparado y probado, exactamente como el gate de la etapa exige
con «v1 intacto, adaptador de lectura, sin reasignar IDs».

### Adopción de escritura en mercado (`DATA-CHASSIS-8`)

`DATA-CHASSIS-8` cruza esa puerta en mercado: el camino de escritura emite identidades v2 y ningún
lector tuvo que ajustarse a posteriori.

- **Extensión acotada de contrato:** `statistics_identity.py` gana el punto de entrada v2 para
  `MetricCalculation`, que delega en la regla auditada y **falla cerrado** si
  `parameters["source_id"]` no coincide con la fuente de la métrica. Las funciones v1
  (`canonical_identity`, `metric_result_id`) quedan byte a byte idénticas y siguen verificando filas
  históricas.
- **Escritura:** `_common_parameters` deja de inyectar `known_at` y las identidades de dependencia de
  EMA/RSI/ATR/MACD se calculan con la regla semántica. Ninguna fila v1 se reasigna, recalcula ni
  reescribe: el espacio v2 es disjunto por construcción (UUID8 frente a UUID5).
- **Pipeline:** las comprobaciones basadas en `known_at` se reemplazan por comprobaciones de versión de
  identidad y PIT, y una cadena mixta v1/v2 falla cerrado.
- **Sexto lector normalizado:** el digest semántico de `consolidated_diagnostic_service.py` traducía el
  parámetro de corte v1 a `effective_inputs`; ahora lo omite en ambas versiones, de modo que v1 y v2 de
  la misma coordenada comparten identidad y `_select_revision` no lanza
  `AmbiguousStoredDiagnosticRevisionError` el día de la transición. Ninguna clase de equivalencia v1–v1
  cambia: los inputs efectivos ya forman parte del documento.
- **Guarda estrechada:** `test_no_production_caller_invokes_the_v2_rule` pasa de prohibir toda
  referencia de producción a una allowlist exacta de un llamador —`analytics/market/statistics_identity.py`—
  y sigue rechazando cualquier otro, incluidos los cinco lectores de `DATA-CHASSIS-7`.
- **Límite declarado:** EMA/RSI/ATR/MACD dependen de `analytics_start = max(start, end − N días)`, de
  modo que la semilla se desplaza cada día y esas cadenas siguen creando filas nuevas hasta la etapa 5.
  El gate «nuevo `known_at` sin evidencia nueva crea cero métricas» ya se cumple para ventana finita.
- **Precondición operativa (HUMAN, fuera de los gates de BUILD):** el primer despliegue que ejecute
  este código escribe identificadores UUID8 en el workspace permanente y esa escritura es append-only;
  se recomienda un backup verificado OPS-8 inmediatamente antes. El rollback es revertir el código: los
  lectores de `DATA-CHASSIS-7` resuelven ambas versiones y ninguna fila requiere acción.

### Adopción de escritura en derivados Deribit (`DATA-CHASSIS-9`)

`DATA-CHASSIS-9` cierra la adopción de escritura de la etapa para las dos familias que concentran el
99,75 % de `metric_results`.

- **Extensión acotada de contrato:** `derivatives_identity.py` gana un punto de entrada v2 que delega en
  la regla auditada y excluye `known_at`, `computed_at` y `value`. `metric_result_id` (v1) y
  `diagnostic_id` quedan byte a byte idénticos: el v1 sigue resolviendo filas históricas.
- **Escritura:** `_metric_result` deja de inyectar `known_at` en `parameters` de las cuatro familias
  (funding sum/mean, DVOL y spread). El corte ya no fluye al camino de escritura: la elegibilidad PIT se
  decide antes, al seleccionar observaciones.
- **Replay histórico:** el servicio read-only resuelve primero la fila v2 persistida, después la v1 del
  mismo corte —identidad v1 recalculada con el `known_at` de la consulta— y por último el candidato en
  memoria. Un corte anterior al despliegue devuelve exactamente las filas v1 persistidas y conserva su
  `diagnostic_id`; el fallo cerrado por contenido semántico distinto se mantiene.
- **Guarda estrechada:** `test_no_production_caller_invokes_the_v2_rule` admite exactamente dos
  llamadores de producción declarados: `analytics/market/statistics_identity.py` y
  `analytics/crypto/derivatives_identity.py`.
- **Sin cambios:** `derivatives_pipeline.py` se consume tal cual, sin `ALGORITHM_VERSION` nuevo, sin
  tablas, migraciones ni schema, y sin adoptar v2 en cazatiburones, valoración ni fundamentales.
- **Precondición operativa (HUMAN, fuera de los gates de BUILD):** el primer despliegue que ejecute este
  código escribe identificadores UUID8 de derivados en el workspace permanente, de forma append-only; se
  recomienda un backup verificado OPS-8 inmediatamente antes. El rollback es revertir el código: el
  replay resuelve ambas versiones y ninguna fila requiere acción.

Con esta adopción, la etapa 3 queda pendiente de una decisión viva de PLAN entre dos candidatos que la
ruta ya nombra: cerrarla con `AnalysisSnapshot` (separado por falta de runner de migración) o abrir la
etapa 4 con el `EvidenceSet` de funding (−90 % bytes), que retira el lineage de 720 UUID.

## Etapa 4: Lineage compartido de derivados (`EvidenceSet`) (`DATA-CHASSIS-10`)

`DATA-CHASSIS-10` abre la etapa 4 con **la representación** del lineage compartido como contrato puro,
determinista y probado, **sin persistirlo y sin llamador**: el mismo patrón de `DATA-CHASSIS-6`, porque la
representación de lineage es una puerta de un solo sentido.

- **Decisión humana del 2026-09-18:** el bloque entrega `EvidenceSet` **sin persistir**. Los dos
  candidatos que dejó abiertos `DATA-CHASSIS-9` (`AnalysisSnapshot` y `EvidenceSet` persistido) requieren
  almacenamiento nuevo y no existe camino para crearlo: `SCHEMA_VERSION = 1` con una única migración,
  validación por igualdad estricta y ningún runner, además de la restricción de no tocar el workspace
  permanente y la exigencia de no migrar in-place. La persistencia llega con el workspace v2 de la etapa 7;
  este bloque **no reduce bytes reales todavía**.
- **Segmento (`EvidenceSegment`):** bloque inmutable y direccionado por contenido con los 24
  identificadores de un día UTC completo de una serie horaria, en el orden canónico del motor. Sólo existe
  para días completos. La política `evidence-segmentation-v1` (horaria → día UTC) es explícita y
  versionada; cualquier otra frecuencia falla cerrado y el punto de extensión queda declarado, no
  implementado.
- **Conjunto (`EvidenceSet`):** lineage de una ventana como lista ordenada de referencias a segmentos de
  día completo, más un desplazamiento de cabeza dentro del primer segmento y `input_count`. Los bordes no
  se copian: son recortes de segmentos completos. Sólo las observaciones del día UTC final incompleto van
  en línea (≤ 23 identificadores). Declara activo, fuente, campo, primera y última observación,
  `available_at` y `canonical_hash`.
- **Hash e identidades:** `canonical_hash` es SHA-256 de la secuencia ordenada completa y no depende de la
  segmentación; `segment_id` y `evidence_set_id` son UUID8 de dominio propio (`evidence-segment-v1`,
  `evidence-set-v1`). El preimage del conjunto excluye `metric_key` —de modo que suma y media de la misma
  ventana comparten conjunto— y excluye corte, reloj, valor y ventana.
- **Verificación:** resolver exige exactamente la secuencia ordenada original; verificar recalcula cada
  segmento y el hash, y comprueba cardinalidad, orden estricto, unicidad, alcance único, contigüidad
  horaria y `available_at`. Toda discrepancia falla cerrado con error tipado; nunca repara ni infiere.
- **Sin persistencia ni adopción:** cero tablas, índices, vistas, archivos, migraciones o bump de
  `SCHEMA_VERSION`; ningún módulo de producción importa el contrato y ninguna fila cambia. La adopción
  persistida queda **condicionada al workspace v2 de la etapa 7** y no puede adelantarse sin enmendar la
  ruta.
- **Desviación declarada:** la etapa preveía ventanas «almacenadas una vez»; este bloque entrega la
  representación sin almacenarla. La segmentación, marcada como «opcional» en la etapa, pasa a ser
  obligatoria en el contrato: sin ella, compartir sólo suma y media reduce el lineage un 50 % y no el
  −90 % del gate. Sobre 45–60 días horarios la representación ocupa ≈ 5–6 % de los bytes de las listas
  repetidas actuales (la variante con bordes en línea, 9,8 %, se descartó por quedar en el límite).

## Contención operacional de recursos y colector acotado (`DATA-CHASSIS-11`)

El primer ciclo en producción del chasis desplegado (2026-09-19, release `2563fad`) agotó la memoria de la máquina virtual WSL y la reinició (`VmHWM` de 6,76 GB en VM de 7,8 GB). El análisis de código aisló dos causas:

1. **Escaneo completo por intento en el colector:** `StorageObservabilityCollector` abría un motor DuckDB propio sin límites antes y después de cada intento, recorriendo `octet_length(encode(document_json))` en todas las tablas de documentos. En 28 intentos, el colector consumió 566 s frente a 404 s de trabajo real (12–29 s por intento).
2. **DuckDBStore sin límites:** Ni el writer ni el reader declaraban límites de memoria ni de hilos, permitiendo que DuckDB asignara por defecto hasta el 80 % de la RAM del sistema a cada conexión.

`DATA-CHASSIS-11` resuelve ambas causas sin modificar schemas, migraciones, identidades ni contratos públicos:

- **Motor de medición acotado:** `_open_read_only_engine` ejecuta `SET memory_limit = '256MB'` y `SET threads = 1` inmediatamente tras conectar y antes de cualquier consulta.
- **Filas por intento, documentos una vez por día UTC:** Cada intento mide sólo `count(*)` por tabla antes y después; la clasificación del crecimiento (`new_evidence_rows`, `revision_rows`, `derived_rows`, `unclassified_rows`) se calcula en cada intento a partir de esos conteos de filas. El escaneo de bytes de documento (`table_bytes`) se ejecuta como máximo una vez por día UTC, en el primer `complete_attempt` del día cuyo artefacto todavía no contiene un registro del día con `table_bytes` no vacío. El resto de registros del día llevan `table_bytes=()`.
- **DuckDBStore acotado:** `DuckDBStore.open` ejecuta `SET memory_limit = '2GB'` y `SET threads = 2` inmediatamente tras conectar, tanto para el writer como para el store de lectura que comparte proceso, antes de inicializar o validar el schema.
- **Contención externa:** Se mantiene el override de systemd (`MemoryHigh=3G`, `MemoryMax=4G`, `CPUQuota=200%`, `Nice=10`) aplicado fuera de repo por el operador humano como red de seguridad permanente.

## Cadencia diaria única y congelamiento del minutario (`DATA-CHASSIS-12`)

Por decisión humana explícita del 2026-09-20, el producto opera con **un único ciclo diario para todos los activos**. El intradía y cualquier frecuencia mayor quedan pospuestos hasta que existan cálculo incremental (etapa 5) y memoria por trabajo acotada.

La evidencia viva demostró que un solo trabajo intradía (`coinbase:crypto:btc-usd:market-intraday`, `minute_1`, `07:30`) concentraba 62.820 barras crudas (51,4 MB) y **314.099 observaciones (287 MB)**: el 62 % de todas las observaciones y el 61 % de sus bytes en todo el sistema, para un único activo y sin consumo analítico por ningún indicador, alerta ni diagnóstico.

La política de contención establece:

1. **Cadencia diaria única:** todos los activos operan exclusivamente en ciclo diario. Las frecuencias intradía quedan formalmente pospuestas; su condición de reapertura exige el cálculo incremental de la etapa 5 y la contención de memoria por trabajo acotada.
2. **Minutario congelado, no borrado:** el minutario persistido histórico se conserva íntegro en almacenamiento y consultable por el endpoint existente (`/api/market-intraday`). No se borran ni modifican filas. La parada del trabajo programado detiene su crecimiento.
3. **Exclusión de la reconstrucción (etapa 8):** en la etapa 8 (rebuild y cutover a workspace v2), el minutario persistido quedará formalmente excluido de la reconstrucción hacia v2, recuperando el espacio ocupado sin alterar la historia diaria.
4. **Retirada de la superficie del gráfico:** el selector de intervalos del gráfico en la interfaz local (`app-core.js`) ofrece exclusivamente intervalos diarios para todos los activos (`auto`, `1d`, `1w`, `1mo`), y cualquier preferencia intradía previa persistida en el navegador cae al valor diario por defecto (`auto`) sin error y sin emitir peticiones a la API intradía.

## Etapa 6: Journal operacional acotado en scheduler, alertas operacionales y screening analítico (`DATA-CHASSIS-13` y `DATA-CHASSIS-16`)

Por decisión de PLAN y autorización explícita del Work Block `DATA-CHASSIS-13`, la etapa 6 se abre adelantada a la etapa 5 (indicadores incrementales). La justificación de orden se fundamenta en que el scheduler reescribía el historial completo de intentos en cada corrida en disco (`attempts`), con límite hardcodeado de 100.000 entradas y riesgo de amplificación de I/O O(n), mientras que la infraestructura de journal append-only acotado es completamente ortogonal a las fórmulas de indicadores e introduce una base duradera de baja latencia sin reescrituras globales.

`DATA-CHASSIS-13` entrega:

1. **Abstracción reutilizable de journal acotado (`BoundedOperationalJournal`):**
   - Contrato puro, tipado y validado en `application/bounded_journal.py` con modelos Pydantic inmutables (`frozen=True`, `extra="forbid"`, sin `Any`).
   - Formato append-only segmentado (`segment_max_bytes=1_048_576`, `snapshot_interval_records=100`).
   - Manifiesto `journal_manifest_v1` con digest SHA-256 de snapshot y de cada segmento completado (`journal_segment_v1`).
   - Recuperación ante caídas (*crash recovery*) que trunca limpiamente registros trailing incompletos o no delimitados por newline en el segmento activo, pero falla cerrado ante segmentos cerrados con hash alterado o snapshots corruptos.
   - Plegado determinista de snapshots a través de `fold_fn` inyectable por el dominio llamador.
2. **Adopción en el scheduler (`MultiAssetScheduleStateStore`):**
   - El historial de intentos de ejecución migra de reescritura monolítica a persistencia append-only en el journal operacional bajo `state_root / "scheduler_attempts_journal"`.
   - Se mantiene la compatibilidad byte a byte y la API pública (`load`, `write_attempt`, `write_attempt_from_state`).
   - Estado legado `multi-asset-schedule-state-v1` se lee y se pliega en un snapshot inicial sin reescribir ni mutar el archivo legado original.
   - Preservación de la sonda operacional OPS-8: si el archivo legado v1 no existía, el store asegura un archivo base para compatibilidad con verificadores de pre-existencia que inspeccionan rutas fijas.
   - Validación estricta en `load()`: si el archivo v1 existente en disco se corrompe posteriormente, la carga falla cerrado conforme a los invariantes de resiliencia del sistema.

### Adopción en alertas operacionales y screening analítico (`DATA-CHASSIS-16`)

`DATA-CHASSIS-16` completa el trabajo de código de la Etapa 6 extendiendo la persistencia por journal append-only acotado a los dos almacenes restantes de estado operacional con escrituras frecuentes:

1. **`OperationalAlertStateStore` (`application/operational_alerts.py`):**
   - Migra el estado de eventos de alerta, resultados de screening operacional y transiciones de usuario a un journal bajo `<stem>_journal` (`journal_id="operational-alerts"`).
   - Reemplaza la reescritura monolítica de todo el documento JSON en `record()`, `transition()` y `resolve_recovered_job()` por appends en O(1) de entradas categorizadas por colección (`screening`, `event`, `transition`).
   - Mantiene caché sincronizada en memoria del estado activo bajo lock para lecturas instantáneas.
   - Plegado transparente del archivo v1 legado (`_ensure_legacy_v1_folded`): el archivo original se valida en cada `load()` estricto y se conserva byte a byte sin alteración de hash SHA-256 tras nuevos appends.
   - Resiliencia determinista: si ocurre un corte abrupto que deje una línea parcial en el open segment, la recuperación trunca la línea incompleta preservando las transiciones y eventos confirmados.

2. **`AnalyticalScreeningStateStore` (`alerts/analytical_state.py`):**
   - Migra el estado de screening analítico (resultados de reglas, eventos de candidatos, transiciones de candidatos y recibos de intentos) a un journal bajo `<stem>_journal` (`journal_id="analytical-screening"`).
   - Registros de intentos (`record_attempt`) y transiciones (`transition`) appendan deltas en O(1) al open segment sin reserializar la historia completa.
   - Incorpora caché en memoria (`_cached_state`) y conjunto dedicado de identificadores de intentos observados (`_cached_attempt_ids`), permitiendo que `contains_attempt(attempt_id)` responda en O(1) con cero accesos I/O a disco y cero decodificaciones JSON repetidas en caliente.
   - El archivo legado v1 se pliega una sola vez a snapshot inicial, conservándose intacto y permitiendo operación continua incluso ante la eliminación posterior del archivo legado.

Con la adopción en ambos stores concluye la Etapa 6 a nivel de código. La verificación post-despliegue de la reducción de latencia e I/O en estado operacional se realizará en un bloque posterior de telemetría y observabilidad.

## Operación de identidad v2: Contención y selección determinista de revisiones (`DATA-CHASSIS-15`)

El primer ciclo desplegado de la release `7fe76a6` (2026-09-21) reveló dos defectos operacionales:
1. **Fallo de memoria (OOM en DuckDB):** En activos de alta frecuencia/volumen de métricas como BTC y ETH (~350.000 filas de métricas por activo, de las cuales ~280.000 corresponden a funding), `statistics_pipeline` y `derivatives_pipeline` ejecutaban `metric_results.list(asset_id, as_of_from, as_of_to)` para luego buscar en un diccionario si existían candidatos calculados. Esto cargaba innecesariamente cientos de miles de filas JSON de otras familias métricas en memoria, provocando `_duckdb.OutOfMemoryException: failed to pin block...`.
2. **Imposibilidad de arranque del scheduler por ambigüedad en indicadores recursivos/dependientes de la ventana:** Al correr con identidad v2 por segunda vez sobre el mismo workspace, los indicadores dependientes de ventana (`true_range`, `ema`, `atr`, `rsi`, `rsi.average_gain`, `rsi.average_loss`, `macd.line`, `macd.signal`, `macd.histogram`) producían una segunda fila v2 para la misma coordenada `(asset_id, metric_key, as_of, available_at)` debido a la dependencia con `analytics_start = max(start, end - N días)` (límite declarado en `DATA-CHASSIS-8`). Durante el arranque del servicio, `analytical_monitor.reconcile` invocaba `AnalyticalMetricSnapshotSelector.select`, el cual fallaba cerrado con `AmbiguousAnalyticalMetricError: macd_histogram_gt_0: multiple compatible metric revisions exist`, abortando la inicialización del proceso.

`DATA-CHASSIS-15` resuelve ambos defectos de forma determinista y sin alterar fórmulas, identidades ni contratos persistidos:

1. **Lectura por identidad en pipelines:** `statistics_pipeline.run` y `derivatives_pipeline.run` sustituyen el escaneo por activo y rango por consulta directa de los identificadores calculados (`_lookup_existing_metrics` apoyado en `get_many` por lotes y `get` individual sin levantar `RecordNotFoundError` en candidatos no existentes aún). Ninguna fila de otra familia métrica es leída para decidir reutilización.
2. **Política versionada `analytical-window-revision-selection-v1`:** Exclusivamente para las nueve claves dependientes de la ventana (`market.technical.ema`, `market.technical.rsi`, `market.technical.rsi.average_gain`, `market.technical.rsi.average_loss`, `market.technical.macd.line`, `market.technical.macd.signal`, `market.technical.macd.histogram`, `market.technical.atr` y `market.technical.true_range`), cuando existen múltiples revisiones v2 con el mismo `as_of` y máxima `available_at`, el selector elige deterministamente la revisión con mayor `computed_at` entre aquellas que satisfacen `computed_at <= known_at` (la evaluación más reciente conocida en el corte). Si ninguna satisface la condición, o ante revisiones fuera de la política (claves no declaradas o revisiones v1), la ambigüedad se preserva fail-closed.
3. **Aislamiento por intento en el monitor:** Si `AnalyticalScreeningMonitor` detecta un error de evaluación analítica (`AnalyticalScreeningError`, incluyendo `AmbiguousAnalyticalMetricError`), registra el intento como recibo `SKIPPED` con motivo explícito (`screening_error:...`), evitando abortar `reconcile` ni derribar el arranque del servicio.
4. **Reutilización en backtest:** `AnalyticalBacktestService` utiliza `AnalyticalMetricSnapshotSelector` y hereda la política sin necesidad de código duplicado.

## Durabilidad inmediata



Hoy no existe copia durable fuera del equipo y la etapa 10 llega al final. La etapa 0 exige registrar la
independencia física del backup; si comparte disco con `C:`, el humano copia el backup verificado a un
medio externo o a Drive y verifica sus hashes, o acepta el riesgo explícitamente en el receipt HUMAN.

## Restricciones durante toda la fase

1. Sin UI salvo un indicador operativo estrictamente necesario.
2. Sin ampliar activos mientras exista amplificación.
3. Sin re-descargar para reemplazar evidencia PIT.
4. Sin borrar ni modificar el workspace permanente.
5. Sin mezclar migración física con cambio de fórmula en un mismo bloque.
6. Sin cambiar IDs sin adaptador.
7. Sin PostgreSQL, microservicios ni base cloud.
8. Sin modelos ni LLM en ingestión.
9. Sin degradar Decimal, UTC, `available_at`, trazabilidad ni append-only.
10. Sin convertir predicción en recomendación.
11. Un único writer por workspace.

## Definición de cierre de la ruta

- Workspace v2 activo y restaurable.
- v1 preservado como rollback.
- Evidencia original con hashes y disponibilidad intacta.
- Métricas iguales guardadas una sola vez.
- Snapshots que reutilizan resultados.
- Funding sin miles de UUID por corte.
- Indicadores recursivos estables e incrementales.
- Crecimiento diario medido y en presupuesto.
- Jobs sin evidencia baratos.
- Backup en Google Drive restaurado con éxito.
- Feature snapshot PIT reproducible.
- Contrato compacto y read-only para el LLM.
- Producto completo sin LLM.
- El cierre no implica liberar espacio físico hasta la retirada HUMAN de v1 y la compactación del VHD.
