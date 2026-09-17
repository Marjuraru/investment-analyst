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
| 8 | Rebuild y cutover | Nunca in-place: detener writer, backup verificado, workspace v2 nuevo, importar raw preservando hashes y `available_at`, reconstruir observaciones, métricas v2 y snapshots, comparar v1/v2, backup/restore de v2, activación atómica, v1 congelado. | Inventario completo; revisiones preservadas; equivalencia exacta donde no cambió algoritmo; recursivos v2 contra baseline independiente; cortes representativos y fronterizos por activo (la cifra de cortes se re-mide, no se asume); UI/API con contratos o adaptadores; presupuesto cumplido; rebuild repetido con mismos hashes; rollback probado; ≥25 GB físicos libres en `C:` antes de empezar. | HUMAN obligatoria |
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

## Etapa 2: Persistencia y verificación por lotes (`DATA-CHASSIS-4` y `DATA-CHASSIS-5`)

La etapa 2 ataca el patrón de persistencia no batcheado identificado en el baseline. Se entrega
particionada en dos bloques por decisión de PLAN verificada en vivo:

1. **`DATA-CHASSIS-4` (este bloque):** construye la capa de acceso y persistencia por lotes en la
   capa de almacenamiento (`get_many`, `save_many`, detección de conflictos en memoria y recibos
   tipados de escritura `BatchWriteReceipt`) en `ObservationRepository`, `MetricResultRepository` y
   `DiagnosticResultRepository`, sin modificar ningún llamador ni doble de test y dejando `analytics/**`
   intacto.
2. **`DATA-CHASSIS-5`:** adopción de la API por lotes en `analytics/crypto/derivatives_pipeline.py` y
   `analytics/market/statistics_pipeline.py`, eliminación del `get` posterior a `save`, memoización del
   grafo de dependencias por corrida, verificación profunda sólo de filas nuevas o conflictivas y
   migración de los dobles de prueba en los tests que consumen esos contratos.

### Reasignación del gate `−80 % real`

Por decisión humana explícita del 2026-09-16, el gate de `−80 % real en jobs de reutilización` de la
etapa 2 queda **reasignado a verificación post-despliegue**. Al ser una observación empírica de
producción sobre el scheduler compuesto, ningún diff ni suite de CI puede satisfacerla de forma
aislada en el repositorio; se verificará con `scripts/report_storage_observability.py` comparando
las ventanas antes y después de desplegar `DATA-CHASSIS-5`. La etapa 2 no se declarará cerrada hasta
dicha verificación.

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
