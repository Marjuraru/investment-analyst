# Ruta DATA-CHASSIS: Chasis de Datos y Almacenamiento

Este documento gobierna la ruta técnica transversal `DATA-CHASSIS`.
Define la arquitectura objetivo, el baseline inicial, las metas provisionales de contención y
eficiencia, la secuencia de etapas y las restricciones operativas permanentes para la transformación
del chasis de almacenamiento de `investment-analyst`, sin degradar la integridad point-in-time, la
trazabilidad append-only ni la evidencia histórica persistida.

## Evidencia histórica de la necesidad (medida el 2026-09-12 sobre `b0c41cd`, fechada)

1. **Amplificación de identidades por `known_at` en mercado:**
   `analytics/market/statistics_identity.py:20` incluía `known_at` en el preimage UUID5 de cada
   `MetricResult` de mercado: un corte nuevo sin evidencia nueva producía identidades nuevas.
   *(Resuelto por `DATA-CHASSIS-7` y `DATA-CHASSIS-8` mediante identidad de métrica v2).*
2. **Amplificación de identidades por `parameters` y lineage O(n) en derivados:**
   `analytics/crypto/derivatives_identity.py` declaraba excluir `known_at`, pero
   `analytics/crypto/derivatives_engine.py:390` lo inyectaba en `parameters`, que sí entraba en la
   identidad determinista: mismo efecto por otra vía. Cada `funding.sum_1h` y `funding.mean_1h`
   persistía además su lista completa de `input_observation_ids` (hasta 720 UUIDs) en `document_json`.
   *(Resuelto por `DATA-CHASSIS-10` con identidad v2 en derivados y `CRYPTO-DERIVATIVES-2` con la retirada del linaje de 720 identificadores).*
3. **Patrón de persistencia no batcheado:**
   `analytics/crypto/derivatives_pipeline.py:74` y `analytics/market/statistics_pipeline.py:115`
   hacían `save` seguido de `get` por fila; `derivatives_pipeline.py:102` volvía a leer cada resultado
   y cada observación uno a uno para verificar.
   *(Resuelto por `DATA-CHASSIS-4` y `DATA-CHASSIS-5` mediante persistencia y verificación en lotes).*
4. **Semilla y recurrencia dependientes de la ventana:**
   `analytics/market/statistics_definitions.py:136`: "The seed and recurrence use only bars selected
   by the point-in-time query" — EMA/RSI/ATR/MACD dependen del inicio de la consulta y no son
   estables incrementalmente. *(Pendiente de resolución en la etapa 5).*
5. **Doble persistencia raw:**
   `storage/raw_records.py` escribía el archivo raw en filesystem **y** `raw_record_index.document_json`
   con el mismo documento completo.
   *(La doble persistencia permanece en el camino productivo v1; los sustratos v2 sin document_json son staging aislado pendientes de migración y cutover).*
6. **Journal operacional sin rotación acotada:**
   `application/multi_asset_scheduler.py:28,470`: `attempts` validado con `max_length=100_000`;
   alertas/candidatos con `250_000`. Documentos JSON reescritos completos.
   *(Resuelto por `DATA-CHASSIS-13` y `DATA-CHASSIS-16` mediante `BoundedOperationalJournal`).*
7. **Presión sobre el almacenamiento físico del host:**
   Espacio físico en disco `C:` bajo Windows con ~49-55 GB libres, con un VHD de WSL que no devuelve
   espacio a `C:` automáticamente al borrar archivos dentro de Linux. *(Pendiente de resolución en la etapa 8).*

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
| 3 | Identidad de métrica v2 y `AnalysisSnapshot` | `result_id` v2 = UUIDv8 derivado por digest SHA-256 de activo, `metric_key`, `as_of`, `available_at`, `algorithm_version`, parámetros semánticos, `input_observation_ids`, `input_metric_result_ids`, unidad y calidad; excluye `known_at`, `computed_at`, parámetros de ejecución e identificadores de job. `value` fuera de la identidad: mismo ID con otro valor es conflicto/no determinismo. Snapshot: `snapshot_id`, activo o `universe_snapshot_id`, dominio, `known_at`, `evidence_set_hash`, `metric_result_ids`, `diagnostic_result_ids`, `policy_version`, `created_at`. v1 intacto, adaptador de lectura, sin reasignar IDs. | Mismos inputs → mismo ID; revisión → resultado nuevo; snapshots resuelven sus métricas; PIT reconstruible; referencias desde alertas, diagnósticos, valoración y backtests probadas. El gate "nuevo `known_at` sin evidencia nueva crea cero métricas" aplica a métricas de ventana finita y derivados; para EMA/RSI/ATR/MACD sólo se cumple tras la etapa 5. `known_at` debe salir también de `parameters` (caso Deribit). El PLAN de esta etapa evalúa primero si `DiagnosticResult`, que ya es un manifiesto por corte con `metric_result_ids`, puede extenderse antes de crear un contrato aislado. | HUMAN |
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
- **Hechos medidos por intento:** bytes físicos del DuckDB y del WAL antes y después, medidos sobre
  el sistema de archivos como enteros exactos; conteos exactos de filas por tabla con el motor
  abierto en `read_only=True`; y el desglose de duración en ejecución del trabajo
  (`job_execution_ms`), consulta (`query_ms`), residuo no atribuido del colector (`collector_unattributed_ms`),
  persistencia (`persistence_ms`) y verificación (`verification_ms`). El reloj del colector mide su ventana y
  reconcilia exactamente las fases con `total_ms`; la duración del job sólo usa timestamps del scheduler si
  encajan cronológicamente dentro de esa ventana.
- **Cota explícita:** 90 snapshots diarios retenidos. Los registros del día abierto se anexan sin
  reescribir el archivo completo y sólo se pliegan al snapshot compacto cuando el día UTC cierra.
- **No intrusivo:** el colector abre el motor únicamente en `read_only=True`, no introduce una
  segunda conexión de escritura y un fallo suyo nunca degrada ni aborta el job medido: se registra
  como issue operativo del scheduler. La fase por intento no recorre `document_json` ni hidrata
  documentos financieros.
- **Límite de atribución declarado y fases honestas (`OBSERVABILITY-1`):** `job_execution_ms`
  (denominado `network_ms` en el contrato v1 original) mide la ventana de ejecución completa del callable del job,
  dentro de la cual el trabajo realiza red, cálculo y persistencia local; una llamada opaca no es sub-atribuible
  desde esta superficie sin instrumentar `providers/http.py` y los pipelines. El colector no estima red ni afirma
  conocer el transporte por separado. Las fases `query_ms`, `persistence_ms` y `verification_ms` son fases del
  propio colector alrededor del trabajo (conteos de filas, compactación y verificación/carga), y
  `collector_unattributed_ms` (denominado `calculation_ms` en v1) es el residuo no atribuido del colector que
  absorbe el redondeo a milisegundos enteros para reconciliar exactamente. En `OBSERVABILITY-1`, el contrato
  evoluciona a `storage-observability-v2` reflejando estos nombres sin romper la lectura de registros v1 históricos.
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
- **Overhead propio medido:** cuando los timestamps del scheduler caben cronológicamente en la ventana
  del colector, `collector_overhead_ms` registra la parte consumida por el instrumento, separada de
  `job_execution_ms`, y el contrato valida que ambas sumen `total_ms` (con compatibilidad para
  `network_ms` en registros v1 históricos). Si los relojes no son compatibles, el registro terminal
  se conserva, `job_execution_ms` queda en cero porque el contrato entero no representa “desconocido”,
  el intervalo queda en `collector_unattributed_ms` y `collector_overhead_ms` es `null`; no se inventa
  una duración ni una atribución entre relojes.
- **Campos opcionales:** la extensión de `storage-observability-v1` es aditiva y con valor por
  defecto; un registro escrito sin ellos sigue parseando sin error. El contrato diario
  `storage-observability-daily-snapshot-v1` conserva exactamente sus campos.
- **Informe read-only:** `storage-observability-report-v1` es tipado, `frozen`, `extra="forbid"`, con
  `schema_version` literal y sin `Any`. Calcula ventanas inclusivas de 7 y 30 días sobre los snapshots
  diarios ya persistidos y declara de forma explícita cada día sin snapshot: nunca interpola, nunca
  rellena con cero y nunca promedia sobre días inexistentes (`mean_daily_bytes_delta` es `None` cuando
  la ventana no tiene días declarados).
- **Política ligera por intento (`DATA-CHASSIS-38`):** todo registro v2 nuevo conserva
  `table_bytes=()` para declarar que los bytes lógicos no se midieron en esa fase. `begin_attempt` y
  `complete_attempt` sólo consultan `stat` de DuckDB/WAL y conteos SQL exactos de filas para medir
  crecimiento y reconciliar tiempos; nunca llaman al escaneo `SUM(octet_length(encode(document_json)))`,
  ni siquiera en el primer job del día, tras un error, un reinicio o un cambio de fecha. Los campos
  `table_bytes` históricos siguen siendo legibles; una ausencia no se reconstruye como cero.
- **Medida lógica de ciclo:** el reporte operacional del ciclo mide una vez los bytes UTF-8 lógicos
  después de que cierre el scheduler; el baseline manual mantiene esa misma lectura explícita. El
  agregado read-only del probe combina sus grupos en una sola consulta y distingue esos bytes del
  crecimiento físico DuckDB/WAL. La CLI pública `storage-observability-report-v1` conserva sus
  campos y no abre DuckDB.
- **Cobertura y causas:** el probe cruza intentos terminales del journal y registros del colector
  por `attempt_id`. Esperado, observado y ausente son conteos distintos; un intento ausente tiene
  coste desconocido, no crecimiento cero. El scheduler conserva su issue seguro legado y expone
  sólo una causa técnica de vocabulario cerrado cuando está disponible. Un motivo vivo sin
  identidad durable no se asigna a un intento antiguo. Duración conocida del colector, ejecución
  de jobs e intervalos entre jobs se presentan por separado; un intervalo entre trabajos queda sin
  atribución.
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
- **Contención externa:** Se mantiene el override de systemd aplicado fuera de repo por el operador humano como red de seguridad permanente. Los valores vigentes en la máquina están versionados como referencia en el repositorio en `deploy/systemd/investment-analyst.service.d/resource-limits.conf`: `MemoryHigh=3500M` (ajustado el 2026-09-23 para evitar estancamiento por presión de memoria), `MemoryMax=4G`, `MemorySwapMax=512M`, `CPUQuota=200%`, `Nice=10` e `IOSchedulingClass=idle`. Ningún código lee, escribe ni aplica este archivo: el operador humano sigue siendo quien lo instala en el host.

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

1. **Lectura por identidad en pipelines:** `statistics_pipeline.run` y `derivatives_pipeline.run` sustituyen el escaneo por activo y rango por consulta directa de los identificadores calculados mediante `metric_results.get_existing` en lotes acotados tolerantes a ausencias, sin levantar `RecordNotFoundError` en candidatos no existentes aún. `DATA-CHASSIS-15` introdujo inicialmente un respaldo fila a fila el 2026-09-21 para candidatos ausentes en el lote; posteriormente `STORAGE-READ-1` retiró ese respaldo fila a fila el 2026-09-23 optimizando la lectura directa en lotes. Ninguna fila de otra familia métrica es leída para decidir reutilización.
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

## Etapa 5: EMA diaria incremental (`DATA-CHASSIS-18`)

`DATA-CHASSIS-18` abre la etapa 5 con una sola recurrencia demostrable: la EMA diaria como
contrato puro, determinista y probado, sin persistirla y sin llamador.

- **Contrato aislado nuevo:** `analytics/market/incremental_ema.py` (`market-ema-incremental-v2-decimal34`,
  `incremental-ema-seed-first-window-v1`). La semilla canónica es la media de la primera ventana de la
  historia PIT completa, nunca del `start` de una consulta de presentación. El checkpoint declara
  activo, fuente, frecuencia `DAY_1`, ventana, semilla, valor Decimal, instantes, longitud del prefijo y
  digest SHA-256 del prefijo ordenado de IDs `close`; su ID determinista incluye la identidad semántica
  y el digest, nunca `known_at`, `computed_at` ni reloj.
- **Transición acotada:** el probe de validación verifica todo el prefijo visible al corte; la
  continuación sobre una cola válida procesa sólo la cola nueva en Decimal explícito (precisión 34),
  sin inferir calendario ni inventar huecos. Una revisión de cualquier ID previo invalida el checkpoint
  y obliga a reconstruir desde la semilla.
- **Sin adopción:** cero cambios en `MarketStatisticsEngine`, pipelines, almacenamiento, identidades
  persistidas, definiciones, API o runtime; ninguna fila v1 se presenta como v2. La etapa 5 queda
  abierta: faltan RSI/ATR/MACD, ventanas finitas, extensión diaria de lineage, persistencia v2 y
  adopción productiva. Ninguna mejora de filas, bytes o RSS se atribuye a este contrato sin un ciclo
  posterior a su adopción.

## Lecturas SEC acotadas para cobertura y valoración (`DATA-CHASSIS-19`)

`DATA-CHASSIS-19` amplía las lecturas acotadas del chasis a los dos lectores SEC que se repiten por
emisor en `universe-coverage-v1`: investigación fundamental y valoración anual consultan sólo filas
potencialmente elegibles con los filtros read-only ya existentes, sin cambiar resultados PIT, conteos
públicos ni detección de evidencia elegible inválida. La matriz completa queda idéntica en dos cortes
y el fixture demuestra la reducción de modelos hidratados; los tiempos del fixture se registran como
observación sin convertir un umbral de máquina en verdad semántica. Este bloque no promete resolver
los ~24 s de la matriz ni el pico de RSS; la ruta queda `DATA-CHASSIS NEXT` con un gate de lectura
real pendiente.

## Estado compacto del scheduler sin reprocesar historial (`DATA-CHASSIS-20`)

`DATA-CHASSIS-20` acelera la lectura repetida del estado operacional del scheduler sin tocar su
journal: `MultiAssetScheduleStateStore` reutiliza un único estado completamente validado cuando la
huella SHA-256 del legado y de todos los archivos del journal coincide, invalida la caché en cada
escritura y relee o falla cerrado ante sustitución, corrupción o borrado externo. `MultiAssetScheduler`
construye un índice efímero por job y día en una sola pasada y conserva el JSON exacto de status, la
fecha local por zona horaria, reintentos, frescura, `next_run` y reconciliación del registro; el reloj
y el registro se recomputan aunque los bytes no cambien. En scratch, `load()` repetido sobre 600
intentos pasa de p50 ~16,5 ms (validación fresca) a p50 ~0,7 ms (huella + reutilización) y `status()`
de 30 jobs queda en p50 ~1,2 ms; el objetivo HTTP p95 <100 ms y el efecto en ciclos reales requieren
medición posterior y no se atribuye ahorro de 13F, Coinbase o derivados a este bloque.

## Lecturas SEC por identidad y familia PIT (`DATA-CHASSIS-21`)

`DATA-CHASSIS-21` acota la vertical SEC de métricas a su propia familia: el pipeline genera por IDs
candidatos con `get_existing`, verifica identidad antes de escribir, persiste sólo ausentes en lotes
explícitos de hasta 256 mediante `save_many` y relee candidatos por lotes con `get_many`; el selector
pide en almacenamiento únicamente el conjunto exacto de claves SEC y conserva PIT, ambigüedad y traza
en `select_from_results`. Los conteos protegidos, el summary, la idempotencia, el conflicto fail-closed
y el progreso de lotes anteriores se conservan. En fixtures scratch, la generación hace cero `list()`
por activo y el selector materializa sólo claves SEC; los tiempos se registran como observación sin
convertir un umbral de máquina en verdad semántica. No cura contención ni RSS por sí solo; memoria y
ciclo real esperan evidencia posterior a las releases recientes.

## Consulta consolidada PIT por referencias (`DATA-CHASSIS-22`)

`DATA-CHASSIS-22` acota la hidratación de la consulta consolidada a referencias de diagnóstico:
lee todos los diagnósticos del activo en su orden actual, reúne los IDs de métricas citados sólo
por diagnósticos de versión vigente visibles al `known_at`, los deduplica y los hidrata con
`get_existing`, sin `metric_results.list(asset_id=...)` ni `get()` fila a fila. La ausencia se
traduce al mismo `MissingReferencedMetricResultError`, la propiedad del activo y la semántica de
revisiones empatadas se conservan, y `metric_results_examined` cuenta referencias hidratadas. En
fixtures scratch, la consulta hace cero `list`/`get` de métricas y conserva el JSON exacto previo
con 80 filas ajenas presentes; los tiempos se registran como observación. No prueba reducción de
RSS global ni resuelve la carga inicial de la interfaz; latencia de endpoint y ciclo esperan una
release que incluya #283–#292 y este bloque.

## Selección PIT 13F antes de hidratar evidencia (`DATA-CHASSIS-23`)

`DATA-CHASSIS-23` elimina el trabajo conocido de las lecturas 13F: el repositorio raw
selecciona IDs por los campos tipados `payload.report.manager_cik`,
`payload.outcome.filing.filer_cik` y `payload.position.report_id` desde un conjunto
cerrado en código, con filtros de fuente, esquema y disponibilidad, orden determinista
y valores parametrizados. `list_reports`, `list_outcomes` y `list_positions` verifican
SHA e hidratan sólo los candidatos en lotes de máximo 512; `report_ids` vacío termina
sin consultar documentos. Firmas, orden, PIT y errores de registros seleccionados
ausentes o corruptos se conservan. En fixtures scratch, el resultado tipado y el orden
son idénticos a la implementación previa en dos cortes, con cero hidrataciones ajenas.
No promete reducción global de RSS; la comparación de ciclo real con carga 13F espera
una release posterior.

## Grafo incremental diario RSI, ATR y MACD (`DATA-CHASSIS-24`)

`DATA-CHASSIS-24` fija el contrato puro del grafo recursivo diario: RSI Wilder con
ganancias/pérdidas medias, true range/ATR Wilder y MACD sobre EMAs de cierre con
semilla SMA de primeras líneas elegibles, en versiones nuevas
`market-rsi-incremental-v2-decimal34`, `market-atr-incremental-v2-decimal34` y
`market-macd-incremental-v2-decimal34`. Cada familia expone semilla canónica,
validación de checkpoint contra el prefijo visible, continuación y cálculo completo
con igualdad Decimal exacta entre completo y reanudado. Prefijo revisado, truncado o
reordenado, checkpoint alterado, evidencia futura y números no finitos fallan cerrado.
El engine y el pipeline productivos no cambian. La etapa 5 sigue abierta: faltan
ventanas finitas, lineage diario compartido, persistencia v2 y adopción.

## Sustrato raw v2 aislado e índice consultable (`DATA-CHASSIS-25`)

`DATA-CHASSIS-25` abre la subfase raw de la etapa 7 con un formato físico aislado
`raw-v2-staging-v1`: destino nuevo y explícito con marcador tipado, blobs
content-addressed por SHA-256 en `raw/sha256/` e índice `raw_v2_index` sin columna
`document_json`, con proyecciones 13F tipadas de gestor y `report_id` extraídas del
`RawRecord` validado al insertar. `save`/`save_many` son idempotentes y rechazan
conflictos de ID; `get`/`get_many` y la selección por fuente, esquema, activo,
`available_at` y proyecciones verifican hash, identidad, metadatos y proyección
frente al archivo. En workspace scratch, IDs, orden y PIT en dos cortes son
idénticos a v1 con una sola copia canónica raw. v1 continúa como único workspace
productivo; importer, backup v2, cutover y rollback quedan para bloques posteriores.

## Mercado diario con corte estable y planificación acotada (`DATA-CHASSIS-26`)

`DATA-CHASSIS-26` corrige el defecto operacional del ciclo diario: tras un fetch
AUTO sin corte manual y sin raw ni observaciones nuevas, Alpaca y Coinbase fijan
`analytics_known_at` en la máxima disponibilidad proyectada y `analytics_end` en
el día siguiente a la última barra válida, con la ventana de 90 días naciendo de
ese fin. La proyección usa agregados SQL parametrizados sin hidratar documentos;
el planificador Coinbase reemplaza su lista histórica por `observed_at_bounds` y
`maximum_available_at` acotados. `analytical_inputs_changed` separa el screening:
el monitor MARKET omite el check sin cambio de mercado aunque existan receipt o
valoración. Estadísticas y diagnósticos recalculan la serie como antes; no se
promete CPU O(delta) ni menos RSS.

## Importación raw v1 a v2 reanudable y verificable (`DATA-CHASSIS-27`)

`DATA-CHASSIS-27` completa la unidad de reconstrucción raw: paginación keyset v1
por `(received_at, record_id)` e inventario v2 por páginas de máximo 256 IDs,
importador que verifica cada página en origen, escribe con `save_many`,
relee lo insertado y confirma un checkpoint atómico con cursor, conteo y digest
acumulado. Cada lote es independiente y la reapertura valida el prefijo
confirmado antes de continuar. Al terminar, ambos inventarios se recorren por
páginas con conjunto exacto de IDs, bytes SHA-256, metadatos, conteos por
fuente/esquema y digest ordenado; PIT v1/v2 coincide en dos cortes. El origen es
una copia v1 restaurada y verificada abierta read-only en scratch; el destino es
disjunto y nuevo. Sin backup v2 completo, observaciones/métricas v2, cutover ni
promesa de reducción de RSS.

## Backup raw v2 portable y verificable (`DATA-CHASSIS-28`)

`DATA-CHASSIS-28` entrega durabilidad del staging raw v2: marcador con
`staging_id` estable en destinos nuevos (los antiguos siguen legibles),
checkpoint de import `raw-v2-import-state-v2` ligado a identidad y fingerprint
en vez de ruta absoluta (v1 conserva su semántica de misma ruta), backup con
inventario ordenado de archivos por streaming SHA-256 e índice DuckDB
file-backed verificado contra la conexión, y restore a destino vacío que
verifica archivos, índice, blobs, checkpoint y prefijo antes de promocionar.
La verificación final del import compara inventarios en streaming por páginas
de máximo 256 IDs sin acumular listas O(N). Sin workspace v2 productivo,
observaciones/métricas v2, cutover, liberación de bytes ni mejora de RSS.

## Observaciones v2 y restauración verificable (`DATA-CHASSIS-29`)

`DATA-CHASSIS-29` amplía el staging aislado a evidencia normalizada sin tocar
el workspace permanente: tabla `normalized_observations_v2` en el mismo índice
DuckDB del staging, sin `document_json`, con todas las coordenadas de
`NormalizedObservation`, Decimal exacto como texto canónico y componentes
tipados de `SourceReference`. `save_observations`/`get_observations` y la
lectura PIT por activo/fuente/frecuencia/tiempo/corte reconstruyen el mismo
modelo y orden que v1; cada lectura confirma ID, contenido y vínculo al raw v2
completo, y la corrupción, ausencia o proyección divergente falla cerrado. El
origen es una copia v1 restaurada y verificada abierta read-only con
paginación keyset adicional por `(available_at, observation_id)` en lotes de
máximo 256 IDs; el importador hidrata y verifica cada lote, exige raw v2
completo y correspondiente, escribe, relee y confirma el checkpoint atómico
`observation-v2-import-state-v1` sólo después. La reapertura valida el prefijo
en streaming acotado y la restauración a otra ruta conserva identidad y
permite reanudar. El backup con observaciones emite
`raw-v2-staging-backup-manifest-v2` y verifica ambos inventarios, ambos
checkpoints y dos cortes PIT antes de la promoción atómica; los backups
raw-only v1 siguen restaurando como antes. Sin Parquet, métricas,
diagnósticos, `EvidenceSet`, cutover, limpieza de v1 ni promesa de reducción
de RSS.

## Métricas v2 y lineage horario compartido restaurable (`DATA-CHASSIS-30`)

`DATA-CHASSIS-30` entrega la representación física verificable de métricas y
lineage sobre las observaciones v2, sin activar rutas productivas: tablas
`metric_results_v2` sin `document_json` con Decimal exacto como texto
canónico, parámetros semánticos como JSON canónico y `computed_at` de la
primera persistencia; links ordenados para inputs de observación y métrica;
y `evidence_segments_v2`/`evidence_sets_v2` que reutilizan el contrato puro
horario byte por byte con identidad de contenido. `save_metrics` recalcula
el UUIDv8 semántico, valida inputs y dependencias en orden topológico con
visibilidad por `available_at` y activo, conserva lotes previos si uno
posterior falla y devuelve recibos created/reused; el rerun con distinto
`computed_at` reutiliza la primera fila y el mismo ID con valor, orden,
parámetros o dependencias distintos falla cerrado. Para lineage HOUR_1
representable, la fila refiere un `EvidenceSet` persistido y compartido sin
repetir sus 24/168/720 IDs; ventanas no representables o con disponibilidad
conservadora tardía usan links explícitos. El backup con familia analítica
emite `raw-v2-staging-backup-manifest-v3` y verifica métricas, DAG y lineage
por páginas de máximo 256 IDs además del hash físico; los manifests v1
raw-only y v2 con observaciones siguen legibles y restaurables sin
reescritura. Sin `AnalysisSnapshot`, diagnósticos v2, checkpoints
EMA/RSI/ATR/MACD, migración de 1,67 M de métricas, adopción productiva,
Parquet, cutover ni promesa de reducción de RSS.

## Lecturas 13F acotadas por gestor y evidencia (`DATA-CHASSIS-31`)

`DATA-CHASSIS-31` acota la cadena 13F al gestor y al objetivo sin cambiar
resultado, calendario ni corpus: el selector raw cerrado gana
`semantics_manager`, `correspondence_artifact` y `correspondence_manager`
con valores parametrizados; el lector de artefactos hidrata sólo las
revisiones PIT del gestor en orden y en lotes de máximo 512, con ausencia o
gestor ajeno en fail-closed; las correspondencias del artefacto y las
observaciones del gestor/activo/corte se leen por identidad y referencias
declaradas en lotes acotados; los pipelines de métricas y pesos consumen
sólo ese subconjunto; el evento consulta la familia versionada antes de
validar versión, gestor y PIT; y la verificación relee sólo los IDs
declarados del candidato. La equivalencia PIT en dos cortes, la hidratación
que escala con el objetivo y el probe scratch de dos cierres con RSS/tiempo
orientativos quedan probados; la reducción de RSS del ciclo real queda para
el probe post-despliegue.

## Diagnósticos y snapshots PIT en staging v2 (`DATA-CHASSIS-32`)

`DATA-CHASSIS-32` entrega la representación física verificable de diagnósticos
y snapshots analíticos point-in-time sobre el staging aislado v2, sin activar
rutas productivas ni migración de histórico:
- **Contrato puro `AnalysisSnapshot`**: modelo inmutable y tipado con
  identidad determinista UUIDv8 derivada directamente de un digest SHA-256 (sin
  namespace UUIDv5), cuya preimagen canónica incluye `asset_id`, `domain`,
  `known_at` normalizado a UTC, `policy_version`, tupla ordenada y deduplicada
  de `metric_ids`, tupla ordenada y deduplicada de `diagnostic_ids` y
  `evidence_set_digest` requerido (con digest canónico vacío cuando no existen
  EvidenceSets). El reloj de creación `created_at` del snapshot queda excluido
  de la identidad de dominio.
- **Tablas `diagnostic_results_v2` en staging**: persistencia física sin
  `document_json`, preservando componentes estructurados, links a métricas y
  evidencia tipada con `evidence_direction`, `weight` Decimal y verificación
  estricta de referencias point-in-time contra `metric_results_v2` (comprobando
  existencia, correspondencia de activo y disponibilidad temporal al corte).
- **Tablas `analysis_snapshots_v2` en staging**: persistencia física sin
  `document_json`, con links relacionales normalizados a métricas y
  diagnósticos, verificación de consistencia temporal (`available_at <= known_at`
  para métricas, diagnósticos y EvidenceSets referenciados), correspondencia de
  activo y coincidencia exacta del `evidence_set_digest` con los hashes
  canónicos resueltos.
- **Backup v4 verificable (`raw-v2-staging-backup-manifest-v4`)**: generado
  automáticamente cuando existen diagnósticos o snapshots en staging, ligando
  los inventarios de crudo, observaciones, métricas, lineage, diagnósticos y
  snapshots con verificación profunda de DAG y enlaces relacionales antes de la
  promoción atómica. Los manifests previos (v1, v2, v3) se preservan legibles y
  restaurables con total compatibilidad hacia atrás.

Sin checkpoints de series temporales (EMA/RSI/ATR/MACD), migración masiva de
analítica v1, cutover productivo de workspace v2, liberación de almacenamiento ni
promesa de reducción de RSS.

## Alcance diario acotado y resumen analítico fiel (`DATA-CHASSIS-33`)

`DATA-CHASSIS-33` acota el horizonte operativo de los refrescos de mercado diario para Alpaca y Coinbase y garantiza la fidelidad de sus resúmenes analíticos:
- **Horizonte operativo acotado a 90 días naturales**: tanto en `resolve_market_daily_cut` para acciones (Alpaca) como en el pipeline de Bitcoin (Coinbase), la ventana de cálculo analítico queda acotada superiormente por `operational_end = requested_end` e inferiormente por `operational_start = max(start, operational_end - timedelta(days=90))`. Si el usuario solicita un intervalo más corto (por ejemplo 10 o 30 días), se respeta la fecha de inicio solicitada; pero si la historia solicitada o existente abarca cientos de días (ej. >600 días), la consulta analítica no hidrata ni procesa la serie completa, manteniéndose en un máximo de 90 días.
- **Fidelidad del resumen (`analytics_end`)**: `ListedMarketRefreshSummary` y `CryptoSpotDailyRefreshSummary` reportan exactamente la ventana ejecutada (`analytics_end` proyectado o acotado en vez del fin solicitado sin evidencia). Si una consulta solicita hasta el día 12 pero la última evidencia disponible llega hasta el día 9 (fin proyectado día 10 a las 00:00 UTC), el resumen reporta fielmente `analytics_end = 2026-07-10T00:00:00Z` coincidiendo exactamente con la query ejecutada en `MarketStatisticsPipeline`.
- **Preflight estricto de timezone**: `resolve_market_daily_cut` valida la presencia de timezone y normaliza a UTC el reloj `effective_known_at` de forma inmediata antes de cualquier ramificación de retorno temprano, garantizando fail-closed ante relojes naive sin importar el modo o la existencia de datos.
- **Pushdown de `source_id` en `HistoricalMarketDataService`**: el filtrado por fuente se traslada al nivel de almacenamiento (`storage.observations.list(source_id=...)`) antes de la hidratación de modelos y la verificación de calidad en memoria, evitando lecturas y conversiones innecesarias de observaciones pertenecientes a fuentes ajenas del mismo activo.

## Integridad y pertenencia de dominio analítico v2 (`DATA-CHASSIS-34`)

`DATA-CHASSIS-34` asegura la integridad referencial, de dominio y de escalado acotado para métricas, diagnósticos, snapshots y EvidenceSets sobre el staging aislado v2:
- **Política pura de pertenencia de dominio (`analytical-domain-membership-v1`)**: formaliza el mapeo canónico y determinista para los cinco dominios analíticos autorizados (`market`, `fundamental`, `valuation`, `derivatives`, `events`). Clasifica prefijos de métricas para decidir su pertenencia como miembros de diagnósticos y snapshots. Una métrica puede depender legítimamente de evidencia de otro dominio (por ejemplo, valoración usa precio y fundamentales); esa dependencia no cambia el dominio de salida ni autoriza mezclarlos en un diagnóstico o snapshot. Los diagnósticos y snapshots fallan cerrado ante miembros de salida incompatibles, métricas sin familia autorizada o modelos mal formados.
- **Resolución transitiva del DAG y EvidenceSets en lotes ($\le 256$) con CTE recursiva**: tanto en escritura (`MetricV2Store.save_many`, `DiagnosticV2Store.save_diagnostics`, `AnalysisSnapshotV2Store.save_snapshots`) como en lectura (`RawV2Staging._resolve_metrics_lineage_batch`, `get_metrics`, `fetch_diagnostics_chunked`, `fetch_snapshots_chunked`), las dependencias métricas externas y sus ancestros se descubren transitivamente mediante consultas CTE recursivas sobre `metric_v2_metric_links` e hidratan en lotes acotados ($\le 256$) sin N+1 ni recursión Python, validando canónicamente la preimagen (`metric_result_id_from_model_v2`), coincidencia de activo, disponibilidad PIT (`dep.available_at <= m.available_at`), aciclicidad (Kahn), existencia y consistencia de observaciones directas para cada métrica del grafo, y existencia de EvidenceSets cuyos miembros coincidan estrictamente con las observaciones de entrada antes de insertar en base de datos. Si una métrica ancestro o su observación es corrompida o eliminada en disco, tanto la escritura como la lectura de la hoja o entidad citante fallan cerrado con `MetricV2Error`, `DiagnosticV2Error` o `AnalysisSnapshotV2Error`.
- **Validación estricta de observaciones al persistir EvidenceSets**: `EvidenceSetV2Store.save_set` y el wrapper `RawV2Staging.save_evidence_set` validan mediante `verify_set_lineage` que todas las observaciones referenciadas por los segmentos del conjunto existan en `normalized_observations_v2`, coincidan exactamente en activo, fuente y campo, y satisfagan `obs.available_at <= set.available_at` antes de insertar o reutilizar el set en base de datos, garantizando idempotencia estricta en reescrituras idénticas y fallando cerrado ante corrupciones.
- **Revalidación semántica completa y compartida en diagnósticos y snapshots**: `save_diagnostics`, `save_snapshots`, `fetch_diagnostics_chunked` y `fetch_snapshots_chunked` usan el validador compartido `verify_metrics_dag_and_lineage`. Dentro de una operación, `AnalyticalV2ValidationContext` comparte los modelos y el lineage de métricas, diagnósticos, EvidenceSets, segmentos y observaciones ya resueltos entre citas directas e indirectas, páginas y la relectura idempotente. No hay caché global: cada operación nueva vuelve a consultar y validar DuckDB, por lo que una corrupción posterior falla cerrado. Los EvidenceSets se validan por hash, miembros, segmentos, observaciones, activo y disponibilidad PIT; los snapshots recalculan `evidence_set_digest` sobre sus métricas citadas. Las rutas de backup y restore usan el mismo validador dentro de su propia operación.
- **Detección estricta de huecos de posición y eliminación de N+1 en `list_metrics`**: `list_metrics` pagina internamente en trozos de 256 elementos vía `get_metrics(chunk)` eliminando consultas N+1 por fila; `EvidenceSetV2Store` carga segmentos en lote y valida que `position` en `evidence_set_v2_members` forme una secuencia contigua 0-indexada estricta (`0..N-1`), rechazando corrupciones o saltos (`[0, 5]`) con `EvidenceSetV2Error`.
- **Verificación reforzada y acotada en restore v4 de backups**: `_verify_restored_metrics` pagina las métricas restauradas en lotes de hasta 256 (`_MAX_BACKUP_PAGE`), las hidrata con `fetch_metrics_chunked` y delega su verificación completa al validador compartido `verify_metrics_dag_and_lineage`, eliminando consultas N+1 por métrica y validando dependencias transitivas, observaciones y EvidenceSets. Para la tabla `evidence_set_v2_members`, comprueba explícitamente en lote que las posiciones por cada set sean contiguas `[0, 1, ..., N-1]`. `_verify_restored_analysis` revalida diagnósticos y snapshots en lotes acotados, rechazando paquetes de backup manipulados, corrupciones semánticas o huecos antes de promover el directorio de destino.
- **Consultas, validaciones e hidratación acotadas en trozos (chunks ≤ 256)**: `MetricV2Store.save_many` y `get_many`, `DiagnosticV2Store.save_diagnostics` y `get_diagnostics`, `AnalysisSnapshotV2Store.save_snapshots` y `get_snapshots`, `EvidenceSetV2Store.save_segments` y `verify_set_lineage`, y el restore de backups usan lotes de máximo 256 y no hacen barridos completos para hidratar el objetivo. En el fixture A3 con $K \in (1, 256, 257, 513)$, cada métrica comparte una observación, un EvidenceSet y un segmento; cada diagnóstico y snapshot cita esa métrica tanto por su referencia analítica como por la directa del snapshot. Los conteos totales observados para `get_metrics` son 16, 16, 19 y 22; para `get_diagnostics`, 21, 21, 28 y 35; para `get_analysis_snapshots`, 23, 23, 33 y 43; y el reuso idempotente de `save_analysis_snapshots` se verifica con K=1, bajo el mismo límite, leyendo una vez cada fila de snapshot y su lineage compartido. Todos quedan bajo `16 + 64 * ceil(K / 256)` y cada lista de parámetros es ≤256. En $K=513$, las tablas paginadas se consultan 3 veces; EvidenceSet, miembros, segmento y la observación se consultan una sola vez por operación. Se leen exactamente $K$ métricas, diagnósticos, snapshots y enlaces principales, $K$ enlaces de observación, un EvidenceSet, un miembro, un segmento y una observación. El reuso idempotente también lee cada fila de snapshot una sola vez. Los cuatro tamaños producen la misma cardinalidad antes y después de añadir 2.048 filas ajenas repartidas en 32 activos: cero filas ajenas hidratadas y cero consultas adicionales por tabla. El test registra duración antes y después sin afirmar latencia constante. El DAG compartido (274 nodos: 2 raíces → 16 intermedios → 256 hojas) se recorre iterativamente en 3 lotes; la cadena de 513 métricas se valida sin recursión y con coste proporcional al DAG alcanzable.
- **Paginación keyset determinista multiactivo**: `list_diagnostics` y `list_analysis_snapshots` implementan paginación keyset acotada basada en cursor temporal UTC + UUID (`cursor_at`, `cursor_id`) con desempate estricto por identificador (`(available_at, diagnostic_id)` y `(known_at, snapshot_id)`), y validación estricta de límite entre 1 y 256 elementos (`MAX_CHUNK_SIZE = 256`, rechazando booleanos, 0 y valores >256). Cuando `limit` se omite (`limit=None`), se ejecuta un recorrido interno paginado en trozos de 256 elementos sin truncar la salida, evitando lecturas no acotadas en base de datos y garantizando el aislamiento estricto por activo sin hidratar datos ajenos.
- **Ordenamiento topológico iterativo sin recursión**: `topological_sort_metrics` implementa el algoritmo iterativo de Kahn sobre grafos acíclicos dirigidos de dependencias de métricas para tolerar grafos profundos (>1000 nodos) sin riesgo de `RecursionError`, detectando ciclos y dependencias circulares o auto-referenciales de forma inmediata y fail-closed.
- **Agregación estricta de EvidenceSets directos e indirectos**: `AnalysisSnapshotV2Store` valida que el `evidence_set_digest` canónico del snapshot contemple no sólo las métricas citadas directamente sino también la totalidad de las métricas referenciadas indirectamente por sus diagnósticos asociados. Las colecciones vacías devuelven un `BatchWriteReceipt()` limpio sin fallar ni truncar transacciones previas.
- **Coste computacional acotado O(salida + grafo + lineage)**: la persistencia y lectura en staging v2 escala estrictamente en función del volumen de entidades emitidas y del grafo local de dependencias y segmentos referenciados, manteniendo límites de defensa acotados. Excluyendo consultas de introspección de esquemas, los conteos observados de filas leídas por tabla (`table_rows`) y modelos hidratados coinciden exactamente con la cardinalidad del objetivo, sin escaneos completos de tablas ni hidratación de historia ajena.
- **Aclaración del estado del chasis**: la persistencia v2 permanece confinada al staging aislado y no reduce bytes productivos en el DuckDB v1 ni en el host mientras no se ejecute el cutover del workspace; el camino productivo raw v1 conserva su doble persistencia (archivos en disco + `document_json`). La ruta `DATA-CHASSIS` no se considera finalizada y continúa su curso hacia la contención y reemplazo de v1.

## Persistencia por lotes y validación acotada (`DATA-CHASSIS-35`)

`DATA-CHASSIS-35` reduce el trabajo de persistencia e hidratación sin cambiar
identidades, contratos de workspace, schema, fórmulas ni semántica point-in-time:

- **A1 — transporte de inserción acotado**: un registro fijo de tablas y columnas
  de v1/v2 convierte filas tipadas en una sola entrada JSON por ejecución de
  hasta 256 filas. Valida el lote antes de escribir, conserva el orden, texto
  Decimal exacto y UTC, rechaza `float` y fechas naive, y mantiene la atomicidad
  de cada unidad sin apropiarse de una transacción activa del llamador.
- **A2 — ingesta de mercado v1**: Alpaca y Coinbase leen primero las identidades
  existentes, conservan el raw original y su `retrieved_at`, insertan raws y
  observaciones ausentes mediante `save_many` y verifican en lote con `get_many`.
  Los recibos vacíos e idempotentes conservan su comportamiento y la cobertura
  IEX sigue explícitamente limitada a una bolsa.
- **A3 — persistencia analítica v2**: métricas y enlaces, diagnósticos y
  componentes/evidencias, snapshots y referencias, EvidenceSets e índice raw
  usan inserciones acotadas. Las lecturas de IDs y referencias también se
  dividen en páginas de hasta 256; las validaciones de tipos, identidad, DAG,
  activo, fuente, `available_at`, conflictos e integridad siguen siendo
  estrictas.
- **A4 — contexto de validación por operación**: la resolución conserva una
  sola copia de los modelos y referencias alcanzables y reutiliza mapas como
  `ChainMap` durante esa operación. Una operación posterior inicia contexto
  vacío y vuelve a consultar DuckDB, por lo que una corrupción posterior sigue
  fallando cerrado. La búsqueda de fuente y campo para observaciones es
  obligatoria; errores SQL o de esquema no se convierten en una omisión.

El smoke comparativo se hizo en dos repeticiones por perfil, en un único proceso
Python 3.12.3 / DuckDB 1.5.4, con el orden base → candidato → candidato → base
y base exacta `b8664d5be2767f28a52adc5b548e38b4cf6a2531`, con un
workspace scratch nuevo para cada repetición y 257 entidades enlazadas. Separó
preparación, persistencia raw/observaciones/métricas/diagnósticos/snapshots,
validación del DAG y lecturas. Los hashes canónicos de raws, observaciones,
métricas, diagnósticos y snapshots fueron idénticos en los cuatro recorridos.

| Fase | Ejecuciones lógicas base → candidato | Mediana base → candidato | Cociente de duración base/candidato |
| --- | ---: | ---: | ---: |
| Raw v1, escritura inicial | 2 → 7 | 271 ms → 85 ms | 3,20× |
| Observaciones v1, escritura inicial | 2 → 7 | 219 ms → 45 ms | 4,84× |
| Raw v2, escritura inicial | 2 → 7 | 345 ms → 153 ms | 2,26× |
| Observaciones v2 | 260 → 11 | 512 ms → 74 ms | 6,96× |
| Métricas v2, escritura y validación | 791 → 28 | 1.045 ms → 116 ms | 9,05× |
| Diagnósticos v2, escritura y validación | 1.063 → 45 | 1.268 ms → 201 ms | 6,31× |
| Snapshots v2, escritura y validación | 816 → 53 | 1.085 ms → 415 ms | 2,62× |
| Validación del DAG alcanzable | 8 → 8 | 108 ms → 106 ms | 1,02× |
| Lectura conjunta v2 | 326 → 72 | 1.026 ms → 905 ms | 1,13× |

Las ejecuciones lógicas cuentan una ejecución por `execute` y una por cada
parámetro de `executemany`; no son mediciones de latencia de producción. El RSS
se muestreó dentro de cada fase: los deltas no bajaron uniformemente (por
ejemplo, raw v1 pasó de 5.392 a 14.044 KiB y snapshots v2 de 11.562 a 12.892
KiB), así que este smoke no afirma una reducción general de RSS. Los tests de
límites usan además K=513 y la validación usa una cadena de 1.024 métricas.
Todo el almacenamiento del smoke fue temporal; el staging v2 sigue aislado y
el workspace permanente no se migró ni modificó.

## Cierre operativo durable, causas tipadas y medición comparable (DATA-CHASSIS-36 / #323)

Este candidato añade observabilidad operativa local para decidir el siguiente trabajo de contención. No cambia los contratos financieros, la BD productiva, los límites del servicio ni el planificador de adquisición.

- **A1 — resultado terminal durable primero**: cada intento guarda su resultado terminal con el mismo attempt_id, estado, telemetría y completed_at antes de notificar observadores o cerrar la medición. Si esa escritura falla, el scheduler conserva su recuperación vigente y no simula un resultado durable. Un fallo de observación no repite el proveedor ni cambia el éxito o fallo ya guardado. El colector recibe los instantes de terminación del job y persistencia terminal para que el coste posterior no se cuente como duración de adquisición.
- **A2 — observadores independientes**: se intenta entregar a todos los observadores en orden incluso si fallan anteriores. El scheduler agrupa los fallos con un mensaje seguro y reintenta sólo los índices que fallaron en ese intento dentro del proceso; elimina el seguimiento cuando concluye la entrega. No hay recibo durable por observador ni garantía exactly-once auxiliar ante SIGKILL. La recuperación durable y el progreso financiero mantienen sus contratos actuales.
- **A3 — causas cerradas**: Alpaca diario y SEC Submissions/documentos exponen reason_code desde puntos de fallo revisados. La interfaz sólo acepta códigos de tipos conocidos dentro del vocabulario cerrado; no analiza mensajes ni atributos arbitrarios. Preserva tipo, causa interna y prioridad de HTTP, credenciales y transporte. Los registros antiguos con reason_code=None siguen siendo legibles y no se reescriben.
- **A4 — muestras locales v2**: scripts/memory_sampler.py lee una lista fija de propiedades systemd, /proc y cgroup v2; no lee DuckDB, el workspace ni variables de entorno. El JSON mantiene RSS/HWM y aliases legados y añade memory.current/peak/stat/events, swap, PSI, límites y una identidad de proceso/cgroup. Los valores ausentes son null con un motivo acotado; release_sha distingue known, unknown e incoherent. La cadencia predeterminada es 5 segundos y la retención 14 días por fecha America/Lima; sólo se podan archivos mem-YYYY-MM-DD.jsonl reconocidos.
- **Sonda de ciclo**: scripts/cycle_probe.py mantiene legibles muestras v1 y v2, normaliza aliases, separa intentos por attempt_id y elimina duplicados entre snapshots y segmentos del journal. El día se evalúa en America/Lima. Sólo declara cierre cuando el overview indica scheduler habilitado, todos los jobs contabilizados, cero en ejecución y ningún scheduled_next_run_at o scheduled_next_retry_at dentro del día. Timeout o overview incompleto queda como parcial. Conserva intentos sin muestra e intervalos no atribuidos; una coincidencia temporal no se presenta como causalidad.
- **Comparabilidad de memoria**: pico, neto, rango y deltas por intento se calculan sobre muestras dentro del intervalo entre el inicio terminal válido más temprano y el cierre más tardío del ciclo. Muestras adyacentes se pueden cargar para resolver fronteras, pero no entran en el conteo ni en identidad, release o comparabilidad. Sólo una identidad completa coincidente (PID, process_starttime_ticks, boot_id y cgroup_generation) permite deltas; una ruptura interna, reset o campo ausente conserva su motivo y bloquea la comparación.
- **Compatibilidad y límites**: se preservan los lectores storage-observability-v1/v2, sin migración, schema financiero ni reescritura histórica. Las lecturas DuckDB del colector son read-only, con límite predeterminado de 5 segundos. En intentos nuevos `table_bytes=()` significa «bytes lógicos no medidos»; el hook sólo cuenta filas y consulta stat de DB/WAL. La lectura lógica completa se hace una vez en el reporte post-ciclo o en el baseline explícito. Los artefactos de operación no tienen available_at y no alimentan métricas, diagnósticos ni candidatos.
- **Cobertura enlazada**: el reporte cruza cada intento terminal del journal con su registro por `attempt_id`, muestra esperados, observados y ausentes, y mantiene ausencias como desconocidas. Puede mostrar el último reason_code seguro del scheduler si existe, pero no lo asigna a un intento histórico sin identidad; también separa duración conocida del colector, tiempo de jobs e intervalos entre ellos sin atribuir estos últimos.

### Tabla de emisión de reason_code

| Códigos | Punto de emisión |
| --- | --- |
| alpaca_configuration_invalid, alpaca_credentials_invalid | Validación de configuración/credenciales al construir o configurar el cliente Alpaca, antes de consultar el endpoint. |
| alpaca_symbol_invalid, alpaca_request_range_invalid | Validación del ticker y de los extremos/intervalo antes de crear la solicitud de barras. |
| alpaca_http_transport, alpaca_http_status | Frontera de solicitud HTTP: excepción de transporte frente a respuesta con estado no exitoso. |
| alpaca_response_too_large, alpaca_json_invalid, alpaca_response_structure_invalid | Límites y parseo de respuesta: tamaño, JSON inválido y forma/campos/paginación estructuralmente inválidos. |
| alpaca_symbol_mismatch | Validación de que el símbolo de la respuesta corresponde al solicitado. |
| alpaca_pagination_invalid | Cursor/token repetido, inválido o incoherente entre páginas. |
| alpaca_bar_invalid | Validación de cada barra: timestamp, Decimal, campos requeridos y coherencia OHLCV. |
| sec_submissions_fetch_failed | Frontera de descarga de Submissions cuando no existe una causa SEC más específica. |
| sec_submissions_snapshot_invalid, sec_submissions_snapshot_read_failed, sec_submissions_snapshot_persist_failed, sec_submissions_snapshot_conflict | Validación del snapshot, lectura/repositorio, escritura/verificación genérica o conflicto de identidad determinista, respectivamente. |
| sec_document_request_invalid, sec_document_selection_missing, sec_document_selection_invalid | Validación de la solicitud documental y selección de filing/documento objetivo. |
| sec_document_snapshot_missing, sec_document_snapshot_read_failed, sec_document_snapshot_invalid, sec_document_snapshot_ambiguous | Lectura del snapshot SEC: ausencia, error de lectura, contrato inválido o más de una revisión elegible. |
| sec_document_fetch_failed | Descarga del documento SEC después de validar solicitud, snapshot y selección. |
| sec_document_revision_read_failed, sec_document_revision_ambiguous, sec_document_revision_conflict, sec_document_revision_verify_failed | Búsqueda de revisiones existentes, selección ambigua, identidad incompatible o relectura/verificación posterior a persistir. |
| sec_document_blob_persist_failed, sec_document_revision_persist_failed | Escritura del blob del documento y persistencia de metadata de revisión, respectivamente. |
| sec_primary_document_refresh_failed | Fallback de la fachada de refresh sólo cuando no se conserva un código interno más específico. |

Los códigos se refieren a la etapa conocida; un StorageError genérico no se transforma en checksum/conflicto inventado. La fachada SEC conserva la causa más específica conocida. Los reason_code de SMV/FRED y otras rutas ya soportadas continúan intactos.

### Benchmark ABBA del colector

Se comparó la base exacta d54de57f07c70769baf66eb6e046b784ae14c298 con el candidato cuyo colector tiene SHA-256 c3e95ae0ca0a394e315924025d356dd91c09430472e09bff7879d3f76bbec6c6, en Python 3.12.3 / DuckDB 1.5.4 y orden A(base) → B(candidato) → B(candidato) → A(base). Las cuatro ejecuciones usaron la misma BD scratch de SHA-256 fdf6e5771f402d7e3a4f01a2e646c7f4ea7020ff38356eb3734fbf89121afc01: cuatro tablas documentales, 16 filas deterministas por tabla. Cada ejecución usó un state_root nuevo. «Frío» significa primer cierre que mide document_bytes; «caliente», segundo intento del mismo día que omite esa medición de bytes. No significa caché de páginas fría/caliente del sistema operativo.

Tiempos de pared externos por fase, en milisegundos; las consultas SELECT se cuentan sin los dos SET de configuración por conexión:

| Ejecución | Frío begin / complete | Caliente begin / complete | SELECT por fase: frío begin / complete; caliente begin / complete |
| --- | ---: | ---: | ---: |
| A1 base | 17,014 / 21,040 | 14,521 / 15,646 | 5 / 5; 5 / 5 |
| B1 candidato | 14,568 / 17,587 | 15,584 / 17,023 | 5 / 5; 5 / 5 |
| B2 candidato | 15,396 / 16,838 | 15,149 / 16,227 | 5 / 5; 5 / 5 |
| A2 base | 15,179 / 16,735 | 16,221 / 16,416 | 5 / 5; 5 / 5 |

Coste total por muestra (begin + complete, sin callable ni duración simulada del job): A1 38,054/30,167 ms frío/caliente; B1 32,155/32,607 ms; B2 32,234/31,376 ms; A2 31,914/32,637 ms. Es coste del colector por intento, no del ciclo completo con proveedor.

En cada fase, las cinco SELECT son una lectura de information_schema más cuatro lecturas, una por tabla; base y candidato conservan el mismo conteo. El SHA-256 de la serialización de `table_bytes` fue idéntico en las cuatro ejecuciones: 426b79b42dc7632edd480cad5b8626a9976371471c0cb26ca404fa695c232831. El hash de la BD permaneció fdf6e5771f402d7e3a4f01a2e646c7f4ea7020ff38356eb3734fbf89121afc01 antes y después; cada artefacto tuvo dos intentos, y el caliente no volvió a emitir `table_bytes`. Las variaciones de pared se solapan: el experimento no demuestra una mejora de latencia ni menos consultas. La ganancia de este cambio es preservar el resultado ante relojes de ciclo incompatibles sin atribuirles duraciones falsas, no una aceleración medida.

### Probe read-only del runtime observado

El 2026-10-04T22:34:24Z, runtime-identity observó investment-analyst con PID 293, process_starttime_ticks 262, release SHA e276526871bd2efa7ddae3a4efc35d0d769b2c71 y cgroup /user.slice/user-1000.slice/user@1000.service/app.slice/investment-analyst.service, generación 534ac143-f8fc-4f4c-a094-26ffd12f31c5:92823602a55d47ca86e2bdf5238d0f0c:25:2591. El release vivo no coincide con la base del BUILD; el candidato no está desplegado. La comparación con cycle-2026-10-04.json quedó inconclusa porque ese reporte aún no incluye runtime_identity. El probe informó workspace_accessed=false, database_accessed=false y writes_performed=false. Esto verifica identificación segura, no adopción ni mejora de memoria.

### Cola de ruta después de DATA-CHASSIS-36

DATA-CHASSIS permanece NEXT; este candidato ADVANCES sin completar la ruta:

1. Próximo scope desde main: checkpoints persistibles de series recursivas diarias y adopción incremental/lecturas que compartan esa frontera, reordenados con evidencia viva y sin heredar autorización.
2. Migración analítica v1 → staging v2 verificable y reanudable por lotes.
3. Parquet sólo si un benchmark nuevo demuestra necesidad y coste aceptable; no es un objetivo automático.
4. Activación/cutover con verificación bidireccional y recuperación probada; cualquier limpieza física requiere inventario y autorización estrecha posterior.

No hay checkpoints productivos, migración masiva, adopción de staging v2, despliegue del sampler, cambios de unidades, cutover ni liberación de almacenamiento del host en este candidato.

### Procesamiento incremental diario persistido en staging (`DATA-CHASSIS-37` / #327)

El candidato añade `IncrementalMarketService` sobre un `RawV2Staging` ya abierto, sin cambiar el
pipeline v1, el scheduler ni el runtime productivo. `HistoricalMarketDataV2Service.iter_pages()`
selecciona revisiones visibles al corte por páginas de hasta 256; el estado canónico empieza en la
primera historia elegible, aunque la presentación solicite un `start` posterior. Las llamadas
full, backfill y continuación recorren la misma transición por barra y preservan timestamps
irregulares.

La evidencia diaria `market-daily-evidence-prefix-v1` comparte nodos append-only de cierre y
high/low/cierre entre las recurrencias. `MarketRecursiveCheckpoint` persiste estados Decimal34 de
EMA, RSI, ATR y MACD; incorpora warm-up, parámetros, `seed_start`, disponibilidad y referencia al
prefijo. Las métricas conservan sus dependencias directas y referencias a prefix/checkpoint. Una
revisión visible en el corte crea una rama de prefijos y checkpoints nuevos; el corte anterior
mantiene su historia y las ventanas finitas se recalculan en su halo dependiente. Los links de
checkpoint a métricas se pueden reparar tras una interrupción entre la escritura durable de la
métrica y la actualización del link.

El recibo adicional informa filas candidatas/seleccionadas, prefijos y checkpoints creados o
reutilizados, pasos de recurrencia, resultados, tamaños de lote, modelos de barra hidratados,
ventana finita y duraciones separadas para selección, verificación/persistencia de lineage, lookup,
transición, cálculo y validación DAG/persistencia. Estos contadores describen una ejecución; no
prometen complejidad total O(delta) porque la verificación PIT del metadata histórico continúa
siendo lineal y acotada en memoria.

El backup staging pasa a `raw-v2-staging-backup-manifest-v5` cuando hay prefijos/checkpoints y liga
sus conteos y digests al inventario previo. Los manifests v1–v4 conservan su lectura/restauración.
La prueba scratch interrumpe el cálculo tras una página durable, crea backup v5, rechaza una copia
corrupta antes de promover destino y reanuda con los mismos IDs/valores que la corrida continua.
La compatibilidad observada fue Python 3.12.3 / DuckDB 1.5.4.

El smoke scratch `scripts/smoke_market_incremental_v2.py` registra tres perfiles: equivalencia,
revisión y cortes; backup/restore/reanudación; y escala/aislamiento. Sobre IEX sintético ejecuta
N=257 y N=1537, deltas 0/1/3, 32 activos ajenos (2048 barras), contadores SQL, bytes lógicos/físicos,
tiempos de fase y RSS. Para N=1537 el pase inicial seleccionó 1537 barras, hidrató 1556 modelos
(incluidos 19 modelos repetidos como halo entre páginas), creó 9222 checkpoints en siete páginas,
emitió 840 métricas para las últimas 40 fechas solicitadas y midió ~8,8 s de servicio en este
entorno. El delta=0 creó cero filas; delta=1 hidrató 20 barras (un dato nuevo más el halo finito de
19), y delta=3 hidrató 22. Tras sumar los 2048 registros ajenos, la repetición siguió hidratando
cero barras objetivo. Son observaciones scratch sin umbrales operacionales; no demuestran mejora del
ciclo productivo, reducción de RSS del servicio desplegado ni cutover.

La cobertura de integración también ejecuta ETF IEX y Coinbase BTC-USD diaria con calendario
irregular y compara los valores finitos contra el motor Decimal canónico. La etapa 5 avanza con un
servicio persistido funcional y verificable en staging, pero permanece abierta: no hay migración
analítica masiva, adopción de consumidores, activación/cutover productivo, backup Drive restaurado,
features desacopladas ni evidencia operacional nueva. Ninguna fila v1 se rebautiza como v2 y los
datos anteriores siguen siendo append-only.

Transición propuesta: `DATA-CHASSIS` permanece `NEXT`, `route_effect: ADVANCES`. Tras integrar este
candidato, la próxima frontera es la migración analítica v1 → staging v2 verificable y reanudable;
la activación/cutover con recuperación bidireccional permanece posterior y requiere su autorización.
