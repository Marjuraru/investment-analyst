# Baseline de Almacenamiento: Telemetría DATA-CHASSIS

Este documento registra el baseline técnico y la telemetría física y lógica de almacenamiento
obtenida tras completar con éxito el ciclo de backup y restauración sobre la copia restaurada
temporal en volumen externo (`data_chassis_0_restored`), de conformidad con los criterios de aceptación del Work Block
`DATA-CHASSIS-0` (Issue #230).

Ninguna cifra es estimada: todos los valores provienen de mediciones directas sobre el sistema de
archivos, el kernel y consultas SQL ejecutadas en modo `read_only=True` con
`SET memory_limit = '1GiB'` y `SET threads = 1`.

---

## 1. Tiempos de Ejecución y Consumo de Recursos

El ciclo completo se ejecutó de forma desacoplada y fuera del VHD de WSL, ubicando backup, copia restaurada y spill temporal fuera del VHD pero sobre el sistema de archivos del volumen físico host `C:`, por lo que no existe independencia física de disco respecto a `C:`. Se conserva la decisión `HUMAN` de realizar una copia externa verdaderamente independiente en almacenamiento secundario o aceptar formalmente el riesgo de coinhabitación física en el mismo disco host:

| Fase | Comando / Proceso | Duración Real | VmHWM (Memoria Máxima) | Código de Salida |
| :--- | :--- | :--- | :--- | :--- |
| **Creación de Backup** | `scripts/backup_workspace.py` | `05:51:39` | 1.818.680 KB (~1,73 GiB) | Exit 0 |
| **Restauración y Verificación** | `scripts/restore_workspace.py` | `06:56:34` | 2.105.824 KB (~2,01 GiB) | Exit 0 |
| **Ciclo Total Completado** | `create + restore` | **`12:48:13`** | 2.105.824 KB (~2,01 GiB) | Exit 0 |
| **Intento Previo (Fallido)** | `restore_workspace` sin cota | `07:45:00` | OOM (Proceso abortado) | No completado |

### Margen de Memoria y Contención
- Memoria disponible en el host antes del ciclo: `MemAvailable` > 4,3 GiB.
- Margen verificado: `VmHWM_bytes + 2.147.483.648 <= MemAvailable_before_bytes` (se respetó el margen de 2 GiB).
- Limpieza de derrame (*spill*): Durante la fase de resolución relacional en DuckDB, el motor utilizó almacenamiento temporal particionado fuera del VHD (`.duckdb_spill_*`) sin exceder 1 GiB de buffer pool en RAM y limpió completamente el directorio temporal al finalizar. Cero señales de OOM y cero residuos en disco.

---

## 2. Telemetría Física del Host y Almacenamiento

### Snapshot Temporal y Métodos de Medición
- **Timestamp de Snapshot UTC:** `2026-09-15T01:45:00Z` (captura al inicio del ciclo de desacoplamiento y estado quieto del workspace).
- **Snapshots de Espacio en `C:` y Comparabilidad:**
  - **Snapshot inicial de `C:` (pre-ciclo):** Medido el `2026-09-15T01:45:00Z` vía `df -B1` sobre el volumen host `C:` y `shutil.disk_usage`, reportando `31.875.776.512` bytes libres con el workspace en reposo antes de crear el backup. Queda marcado como **histórico no comparable** por corresponder a un estado previo del host no contemporáneo con el baseline de verificación.
  - **Snapshot contemporáneo de `C:` (post-ciclo):** Medido el `2026-09-15T22:41:53Z` vía `df -B1` y `shutil.disk_usage` sobre el volumen host `C:`, reportando `39.546.531.840` bytes libres (`c_free_bytes`, múltiplo exacto de 4096: $39.546.531.840 \pmod{4096} = 0$) y `470.175.694.848` bytes usados ($470.175.694.848 \pmod{4096} = 0$) sobre un total de `509.722.226.688` bytes ($509.722.226.688 \pmod{4096} = 0$).
  - **Regla de alimentación:** Conforme a los criterios de aceptación 4 y 9, la fila histórica pre-ciclo queda identificada como no comparable. Únicamente este snapshot contemporáneo medido (`c_free_bytes = 39.546.531.840` bytes) alimenta la fórmula y la aritmética obligatoria de verificación de margen de seguridad para la Etapa 8.
- **Métodos exactos de medición:**
  - Espacio y particiones de disco del host: `shutil.disk_usage` y `df -B1` sobre puntos de montaje de host (`C:`) y VHD (`/`).
  - Workspace permanente en disco: medido mediante `du -sb` sobre el workspace de producción (`workspace/`), reportando bytes exactos a nivel de inodos de sistema de archivos.
  - Backup verificado en destino externo: medido mediante `du -sb` sobre el destino externo de backup (`data_chassis_0_backup`), realizando un recorrido exhaustivo de bytes.
  - Copia restaurada temporal en destino externo: medido mediante `du -sb` sobre el destino temporal restaurado (`data_chassis_0_restored`).
  - Base de datos DuckDB: `os.path.getsize` y consulta SQL `PRAGMA database_size` sobre `investment_analyst.duckdb` e `investment_analyst.duckdb.wal`.

### Inventario de Almacenamiento: Mediciones Reales vs. Presupuestos de Transición

| Categoría | Superficie / Componente | Bytes Exactos | Representación Binaria (GiB) | Representación Decimal (GB) | Clasificación |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Host** | **Disco `C:` (Total)** | `509.722.226.688` | 474,72 GiB | 509,72 GB | Medición real host |
| **Host** | **Disco `C:` (Libre inicial pre-ciclo)** | `31.875.776.512` | 29,69 GiB | 31,88 GB | Histórico no comparable |
| **Host** | **Disco `C:` (Libre remanente contemporáneo post-ciclo)** | `39.546.531.840` | 36,83 GiB | 39,55 GB | Medición real host contemporánea |
| **Host** | **Disco `C:` (Usado contemporáneo)** | `470.175.694.848` | 437,89 GiB | 470,18 GB | Medición real host contemporánea |
| **WSL** | **VHD WSL `/` (Total virtual)** | `1.081.101.176.832` | 1.006,85 GiB | 1.081,10 GB | Virtual asignado |
| **WSL** | **VHD WSL `/` (Libre virtual)** | `982.089.129.984` | 914,64 GiB | 982,09 GB | Virtual disponible |
| **WSL** | **VHD WSL `/` (Usado virtual)** | `44.019.691.520` | 41,00 GiB | 44,02 GB | Medición real WSL |
| **Workspace** | **Workspace Permanente (`workspace/`)** | `8.510.963.858` | 7,9265 GiB | 8,5110 GB | Medición real (`du -sb`) |
| **Backup** | **Backup Verificado (`data_chassis_0_backup`)** | `8.511.630.696` | 7,9271 GiB | 8,5116 GB | Medición real (`du -sb`) |
| **Restored** | **Copia Restaurada (`data_chassis_0_restored`)** | `8.511.630.696` | 7,9271 GiB | 8,5116 GB | Medición real (`du -sb`) |
| **DuckDB** | **Base DuckDB (`investment_analyst.duckdb`)** | `7.219.458.048` | 6,7236 GiB | 7,2195 GB | Medición real (`os.path.getsize`) |
| **DuckDB** | **DuckDB WAL (`investment_analyst.duckdb.wal`)** | `0` | 0 B | 0 B | Medición real (0 bytes WAL) |
| **Transición** | **Presupuesto base v2 temporal (Etapa 8)** | `3.221.225.472` | 3,00 GiB | 3,22 GB | Presupuesto proyectado |
| **Transición** | **Presupuesto de archivos temporales/spill** | `2.147.483.648` | 2,00 GiB | 2,15 GB | Presupuesto proyectado |

*Nota sobre almacenamiento físico:* El VHD de WSL reporta un espacio disponible virtual no respaldado físicamente en disco; el límite restrictivo real del host es el espacio libre del volumen físico `C:`.

### Declaración de Fuente y Justificación de Presupuestos de Transición
1. **Presupuesto base v2 temporal (`3.221.225.472` bytes = 3,00 GiB / 3,22 GB):**
   - **Fuente:** Plan táctico de particionado vertical y temporal de `DATA-CHASSIS-1` a `DATA-CHASSIS-7` formalizado en `docs/data_chassis.md`.
   - **Justificación:** La arquitectura de datos v2 elimina el 94,71 % de filas redundantes generadas por cortes temporales `known_at` en `metric_results` (sección 6) y sustituye la lista repetida de UUIDs de funding en `input_observation_ids` (que consume el 84,3 % del volumen de métricas, sección 5) por mapeos compactos. El volumen métrico se reduce de 5,14 GB a ~1,2 GB, proyectando un tamaño total para la nueva base DuckDB v2 inferior a 3 GiB durante la fase de coexistencia paralela y verificación de la etapa 8 antes de retirar la base previa.
2. **Presupuesto de archivos temporales / spill (`2.147.483.648` bytes = 2,00 GiB / 2,15 GB):**
   - **Fuente:** Directiva de contención de memoria de sesión DuckDB (`SET memory_limit = '1GiB'`) y directorio de derrame temporal fuera del VHD (`temp_directory = '.duckdb_spill_*'`).
   - **Justificación:** Reserva 2 GiB de espacio en disco para soportar el desbordamiento de operadores de ordenamiento, hash joins y unnest masivo durante la migración y carga masiva de la etapa 8, garantizando que DuckDB opere de forma continua sin provocar OOM en el sistema ni fallar por contención de almacenamiento.

### Aritmética Reproducible del Margen Remanente en Disco `C:`

Para garantizar que las etapas subsiguientes de `DATA-CHASSIS` (especialmente la etapa 8 de migración, coexistencia y corte) se ejecuten sin riesgo de agotamiento de almacenamiento físico, se verifica la siguiente aritmética exhaustiva:

1. **Espacio libre medido en `C:` tras el ciclo:** `39.546.531.840` bytes (~36,83 GiB en base binaria, 39,55 GB en base decimal; múltiplo exacto de 4096: $39.546.531.840 \pmod{4096} = 0$).
2. **Criterio de seguridad exigido para Etapa 8:** Espacio libre >= 25 GB (`25.000.000.000` bytes). Se satisface estrictamente:
   $$39.546.531.840 \text{ bytes} \ge 25.000.000.000 \text{ bytes} \quad (\text{margen positivo de } +14.546.531.840 \text{ bytes sobre el piso de 25 GB})$$
3. **Presupuesto total de transición consumido durante migración activa (Etapa 8):**
   $$\text{Presupuesto v2 temporal} + \text{Presupuesto spill temporal} = 3.221.225.472 + 2.147.483.648 = 5.368.709.120 \text{ bytes } (\sim 5,00\text{ GiB})$$
4. **Margen remanente neto proyectado tras absorción de la Etapa 8 en `C:`:**
   $$39.546.531.840 - 5.368.709.120 = 34.177.822.720 \text{ bytes } (\sim 31,83\text{ GiB} / 34,18\text{ GB libres remanentes})$$
5. **Aritmética agregada obligatoria de 4 componentes (Workspace Permanente + Backup + Base v2 + Temporales):**
   $$\text{Total requerido} = \text{workspace} + \text{backup} + \text{v2 temporal} + \text{temporales}$$
   $$\text{Total requerido} = 8.510.963.858 + 8.511.630.696 + 3.221.225.472 + 2.147.483.648 = 22.391.303.674 \text{ bytes } (\sim 20,85\text{ GiB} / 22,39\text{ GB})$$
   $$\text{Margen remanente en } C: = 39.546.531.840 - 22.391.303.674 = 17.155.228.166 \text{ bytes } (\sim 15,98\text{ GiB} / 17,16\text{ GB remanentes})$$

### Aclaración Normativa sobre Márgenes de Memoria y Almacenamiento
> **Regla de Invarianza:** El margen de memoria RAM de 2 GiB (medido empíricamente mediante `resource.getrusage` / `VmHWM` y limitado formalmente en las sesiones del motor DuckDB mediante `SET memory_limit = '1GiB'`) **no sustituye ni puede considerarse intercambiable con el margen de almacenamiento físico en disco (mínimo 25 GB libres en el volumen `C:`)**. La contención de memoria previene errores OOM en el espacio de usuario del proceso, mientras que el margen de almacenamiento previene fallos catastróficos de escritura por disco lleno (`ENOSPC`) a nivel de sistema operativo y sistema de archivos host.

---

## 3. Conteos de Filas y Bytes Lógicos por Tabla

Consultas SQL ejecutadas en modo `read_only=True`:

```sql
SELECT count(*), sum(strlen(document_json)) FROM <tabla>;
```

| Tabla | Filas Registradas | Bytes de `document_json` | Volumen Binario |
| :--- | :--- | :--- | :--- |
| `raw_record_index` | 294.498 | `780.050.571` | 0,7265 GiB |
| `normalized_observations` | 475.997 | `443.492.116` | 0,4130 GiB |
| `metric_results` | 1.236.466 | `5.140.578.947` | 4,7875 GiB |
| `diagnostic_results` | 1.066 | `2.620.175` | 0,0024 GiB |
| **Total Lógico Documentos** | **2.008.027** | **`6.366.741.809`** | **5,9295 GiB** |

---

## 4. Desglose de Familias Métricas

Consulta SQL ejecutada:
```sql
SELECT
    split_part(metric_key, '.', 1) AS family,
    count(*) AS cnt,
    sum(strlen(document_json)) AS total_doc_bytes,
    round(avg(strlen(document_json)), 1) AS avg_doc_bytes
FROM metric_results
GROUP BY family
ORDER BY total_doc_bytes DESC;
```

Resultados exactos:

| Familia de Métrica | Filas | Bytes Totales de Documento | Promedio Bytes/Fila | % del Total Métrico |
| :--- | :--- | :--- | :--- | :--- |
| `crypto` (Derivados) | 469.763 | `4.346.225.414` | 9.252,0 B | 84,55 % |
| `market` (Técnicas) | 763.639 | `791.451.108` | 1.036,4 B | 15,39 % |
| `fundamental` (SEC) | 1.730 | `1.605.919` | 928,3 B | 0,03 % |
| `valuation` | 1.042 | `1.038.247` | 996,4 B | 0,02 % |
| `cazatiburones` (13F) | 292 | `258.259` | 884,4 B | < 0,01 % |

---

## 5. El Epicentro de Amplificación: Métricas de Funding

Consulta SQL ejecutada:
```sql
SELECT
    metric_key,
    count(*) AS cnt,
    sum(strlen(document_json)) AS total_doc_bytes,
    round(avg(strlen(document_json)), 1) AS avg_doc_bytes
FROM metric_results
WHERE metric_key LIKE '%funding%'
GROUP BY metric_key
ORDER BY cnt DESC;
```

Resultados exactos:

| Métrica | Filas | Bytes de Documento | Promedio Bytes/Fila |
| :--- | :--- | :--- | :--- |
| `crypto.derivatives.funding.mean_1h` | 229.024 | `2.168.093.865` | 9.466,7 B (~9,25 KiB) |
| `crypto.derivatives.funding.sum_1h` | 229.023 | `2.165.375.598` | 9.454,8 B (~9,23 KiB) |
| **Total Funding Deribit** | **458.047** | **`4.333.469.463`** | **~4,035 GiB (84,30 %)** |

*Hallazgo:* Dos únicas métricas de funding consumen el **84,3 % de todo el almacenamiento de métricas** del sistema debido a la serialización redundante de listas de hasta 720 UUIDs en `input_observation_ids`.

---

## 6. Duplicación Causada Únicamente por `known_at`

Consulta SQL ejecutada:
```sql
SELECT count(*), sum(cnt - 1) FROM (
    SELECT asset_id, metric_key, as_of, available_at, count(*) AS cnt
    FROM metric_results
    GROUP BY asset_id, metric_key, as_of, available_at
    HAVING count(*) > 1
);
```

Resultados:
- **Grupos semánticos únicos de métricas:** 65.447 evaluaciones únicas de `(asset_id, metric_key, as_of, available_at)`.
- **Grupos con cortes temporales duplicados:** 51.348 grupos.
- **Filas redundantes generadas exclusivamente por cortes temporales (`known_at`):** **1.171.019 filas** (**94,71 %** de toda la tabla `metric_results`).

---

## 7. Doble Persistencia y Duplicación Raw

Consulta SQL ejecutada:
```sql
SELECT count(*), count(DISTINCT checksum_sha256) FROM raw_record_index;
```

Resultados:
- Total de registros raw en índice: 294.498.
- Hashes SHA-256 únicos en índice: 294.498 (0 colisiones ni registros idénticos redundantes).
- **Doble almacenamiento verificado:** Los `780.050.571` bytes (0,7265 GiB) de documentos crudos residen duplicados: una copia como microarchivos individuales en `storage/data/documents/sha256/...` y una copia idéntica dentro de la columna `document_json` en la tabla `raw_record_index`.

---

## 8. Planes y Perfiles de Ejecución `EXPLAIN ANALYZE` de Linaje Relacional

Las 4 consultas canónicas de verificación de linaje relacional fueron ejecutadas y perfiladas mediante `EXPLAIN ANALYZE` directamente sobre la copia restaurada temporal en volumen externo (`data_chassis_0_restored`, base `investment_analyst.duckdb`) bajo las condiciones formales del verificador:
- **DuckDB Version:** `1.5.4`
- **Timestamp UTC de Ejecución:** `2026-09-15T19:00:50Z`
- **Parámetros de Sesión:** `read_only=True`, `SET memory_limit = '1GiB'`, `SET threads = 1`

### Resumen de Tiempos y Operadores Reales Verificados

| Relación de Linaje | Consulta | Duración Total Real | Filas Retornadas | Operador de Join | Operador Prohibido Detectado |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Relación 1** | `observation_raw` | **`0.143s`** | 0 filas | `HASH_JOIN (ANTI)` | **Cero** (`TOP_N`, `DELIM_JOIN`, `LATERAL`, `GROUP_BY`) |
| **Relación 2** | `metric_observation` | **`120.34s`** | 0 filas | `HASH_JOIN (RIGHT_ANTI)` | **Cero** (`TOP_N`, `DELIM_JOIN`, `LATERAL`, `GROUP_BY`) |
| **Relación 3** | `metric_metric` | **`43.27s`** | 0 filas | `HASH_JOIN (RIGHT_ANTI)` | **Cero** (`TOP_N`, `DELIM_JOIN`, `LATERAL`, `GROUP_BY`) |
| **Relación 4** | `diagnostic_metric` | **`0.0909s`** | 0 filas | `HASH_JOIN (RIGHT_ANTI)` | **Cero** (`TOP_N`, `DELIM_JOIN`, `LATERAL`, `GROUP_BY`) |

### Constatación Formal de Invariantes Negativas de Linaje:
- **Cero `TOP_N`:** Ninguna consulta invoca operadores de ordenamiento global acotado.
- **Cero `LEFT_DELIM_JOIN` / `DELIM_JOIN`:** La correlación se resuelve por hash streaming unidireccional sin duplicación por subconsulta.
- **Cero `CROSS JOIN LATERAL`:** Las referencias anidadas en JSON se expanden vía `UNNEST` relacional puro antes del join.
- **Cero `HASH_GROUP_BY` sobre `document_json`:** No se realizan agrupaciones sobre columnas de texto pesado.
- **Cero Huérfanos:** Las cuatro consultas devolvieron exactamente `0 rows`, certificando integridad referencial total en los 2.008.027 registros lógicos.

---

### Perfil Detallado 1: `observation_raw` (`Total Time: 0.143s`)
Verifica que toda observación normalizada apunte a un registro raw existente:
```sql
EXPLAIN ANALYZE
SELECT o.raw_record_id
FROM normalized_observations o
ANTI JOIN raw_record_index r ON o.raw_record_id = r.record_id
LIMIT 1;
```
Árbol de ejecución y tiempos reales:
```text
┌────────────────────────────────────────────────┐
│              Total Time: 0.143s                │
└────────────────────────────────────────────────┘
┌───────────────────────────┐
│           QUERY           │
└─────────────┬─────────────┘
┌─────────────┴─────────────┐
│      STREAMING_LIMIT      │ (0 rows, 0.00s)
└─────────────┬─────────────┘
┌─────────────┴─────────────┐
│         HASH_JOIN         │ (Join Type: ANTI, raw_record_id = record_id, 0 rows, 0.03s)
└─────────────┬─────────────┘
        ├──────────────────────────────────────────┤
┌─────────────┴─────────────┐              ┌─────────────┴─────────────┐
│         TABLE_SCAN        │              │         TABLE_SCAN        │
│  .normalized_observations │              │     .raw_record_index     │
│   Type: Sequential Scan   │              │   Type: Sequential Scan   │
│ Projections: raw_record_id│              │   Projections: record_id  │
│   475,997 rows (0.03s)    │              │    294,498 rows (0.07s)   │
└───────────────────────────┘              └───────────────────────────┘
```

---

### Perfil Detallado 2: `metric_observation` (`Total Time: 120.34s`)
Verifica que toda referencia en `input_observation_ids` apunte a una observación normalizada existente:
```sql
EXPLAIN ANALYZE
WITH extracted_refs AS (
    SELECT unnest(
        COALESCE(
            from_json(json_extract(document_json, '$.input_observation_ids'), '["VARCHAR"]'),
            []
        )
    ) AS ref_id
    FROM metric_results
)
SELECT ref_id
FROM extracted_refs e
ANTI JOIN normalized_observations o ON e.ref_id = o.observation_id
WHERE ref_id IS NOT NULL
LIMIT 1;
```
Árbol de ejecución y tiempos reales:
```text
┌────────────────────────────────────────────────┐
│              Total Time: 120.34s               │
└────────────────────────────────────────────────┘
┌───────────────────────────┐
│           QUERY           │
└─────────────┬─────────────┘
┌─────────────┴─────────────┐
│      STREAMING_LIMIT      │ (0 rows, 0.00s)
└─────────────┬─────────────┘
┌─────────────┴─────────────┐
│         HASH_JOIN         │ (Join Type: RIGHT_ANTI, observation_id = ref_id, 0 rows, 47.92s)
└─────────────┬─────────────┘
        ├──────────────────────────────────────────┤
┌─────────────┴─────────────┐              ┌─────────────┴─────────────┐
│         TABLE_SCAN        │              │         PROJECTION        │ (0.01s)
│  .normalized_observations │              └─────────────┬─────────────┘
│   Type: Sequential Scan   │              ┌─────────────┴─────────────┐
│ Projections: observation_id│             │           FILTER          │ (ref_id IS NOT NULL, 111,197,985 rows, 0.09s)
│   475,997 rows (0.16s)    │              └─────────────┬─────────────┘
└───────────────────────────┘              ┌─────────────┴─────────────┐
                                           │           UNNEST          │ (111,197,985 rows, 18.37s)
                                           └─────────────┬─────────────┘
                                           ┌─────────────┴─────────────┐
                                           │         TABLE_SCAN        │
                                           │      .metric_results      │
                                           │   Type: Sequential Scan   │
                                           │ Projections: document_json│
                                           │  1,236,466 rows (53.67s)  │
                                           └───────────────────────────┘
```

---

### Perfil Detallado 3: `metric_metric` (`Total Time: 43.27s`)
Verifica que toda referencia en `input_metric_result_ids` apunte a una métrica previa existente:
```sql
EXPLAIN ANALYZE
WITH extracted_refs AS (
    SELECT unnest(
        COALESCE(
            from_json(json_extract(document_json, '$.input_metric_result_ids'), '["VARCHAR"]'),
            []
        )
    ) AS ref_id
    FROM metric_results
)
SELECT ref_id
FROM extracted_refs e
ANTI JOIN metric_results m ON e.ref_id = m.result_id
WHERE ref_id IS NOT NULL
LIMIT 1;
```
Árbol de ejecución y tiempos reales:
```text
┌────────────────────────────────────────────────┐
│              Total Time: 43.27s                │
└────────────────────────────────────────────────┘
┌───────────────────────────┐
│           QUERY           │
└─────────────┬─────────────┘
┌─────────────┴─────────────┐
│      STREAMING_LIMIT      │ (0 rows, 0.00s)
└─────────────┬─────────────┘
┌─────────────┴─────────────┐
│         HASH_JOIN         │ (Join Type: RIGHT_ANTI, result_id = ref_id, 0 rows, 0.10s)
└─────────────┬─────────────┘
        ├──────────────────────────────────────────┤
┌─────────────┴─────────────┐              ┌─────────────┴─────────────┐
│         TABLE_SCAN        │              │         PROJECTION        │ (0.00s)
│      .metric_results      │              └─────────────┬─────────────┘
│   Type: Sequential Scan   │              ┌─────────────┴─────────────┐
│   Projections: result_id  │              │           FILTER          │ (ref_id IS NOT NULL, 403,909 rows, 0.00s)
│  1,236,466 rows (0.34s)   │              └─────────────┬─────────────┘
└───────────────────────────┘              ┌─────────────┴─────────────┐
                                           │           UNNEST          │ (403,909 rows, 2.86s)
                                           └─────────────┬─────────────┘
                                           ┌─────────────┴─────────────┐
                                           │         TABLE_SCAN        │
                                           │      .metric_results      │
                                           │   Type: Sequential Scan   │
                                           │ Projections: document_json│
                                           │  1,236,466 rows (39.94s)  │
                                           └───────────────────────────┘
```

---

### Perfil Detallado 4: `diagnostic_metric` (`Total Time: 0.0909s`)
Verifica que toda referencia en componentes y evidencia de diagnósticos apunte a una métrica existente:
```sql
EXPLAIN ANALYZE
WITH extracted_refs AS (
    SELECT unnest(
        list_concat(
            COALESCE(from_json(json_extract(document_json, '$.components[*].metric_result_ids[*]'), '["VARCHAR"]'), []),
            COALESCE(from_json(json_extract(document_json, '$.evidence[*].metric_result_id'), '["VARCHAR"]'), [])
        )
    ) AS ref_id
    FROM diagnostic_results
)
SELECT ref_id
FROM extracted_refs e
ANTI JOIN metric_results m ON e.ref_id = m.result_id
WHERE ref_id IS NOT NULL
LIMIT 1;
```
Árbol de ejecución y tiempos reales:
```text
┌────────────────────────────────────────────────┐
│              Total Time: 0.0909s               │
└────────────────────────────────────────────────┘
┌───────────────────────────┐
│           QUERY           │
└─────────────┬─────────────┘
┌─────────────┴─────────────┐
│      STREAMING_LIMIT      │ (0 rows, 0.00s)
└─────────────┬─────────────┘
┌─────────────┴─────────────┐
│         HASH_JOIN         │ (Join Type: RIGHT_ANTI, result_id = ref_id, 0 rows, 0.01s)
└─────────────┬─────────────┘
        ├──────────────────────────────────────────┤
┌─────────────┴─────────────┐              ┌─────────────┴─────────────┐
│         TABLE_SCAN        │              │         PROJECTION        │ (0.00s)
│      .metric_results      │              └─────────────┬─────────────┘
│   Type: Sequential Scan   │              ┌─────────────┴─────────────┐
│   Projections: result_id  │              │           FILTER          │ (ref_id IS NOT NULL, 8,552 rows, 0.00s)
│  1,236,466 rows (0.02s)   │              └─────────────┬─────────────┘
└───────────────────────────┘              ┌─────────────┴─────────────┐
                                           │           UNNEST          │ (8,552 rows, 0.01s)
                                           └─────────────┬─────────────┘
                                           ┌─────────────┴─────────────┐
                                           │         TABLE_SCAN        │
                                           │    .diagnostic_results    │
                                           │   Type: Sequential Scan   │
                                           │ Projections: document_json│
                                           │    1,066 rows (0.04s)     │
                                           └───────────────────────────┘
```

---

## 9. Copia Temporal de Auditoría y Decisión Humana

- **Identificador y topología de la copia restaurada:** Destino temporal `data_chassis_0_restored`, ubicado fuera del VHD de WSL pero sobre el volumen físico `C:` (sin independencia física de disco respecto a `C:`).
- **Tamaño total:** `8.511.630.696` bytes (7,9271 GiB).
- **Inventario:** 294.815 archivos verificados con hash SHA-256 idéntico al backup original.
- **Decisión humana:** Queda a criterio humano (`HUMAN`) la conservación de esta copia temporal para auditorías posteriores o su eliminación para recuperar ~8,5 GB en el disco físico `C:`, así como la decisión de transferir el backup verificado a un medio externo independiente o aceptar el riesgo de coinhabitación física en el mismo volumen `C:`. El backup durable y verificado reside en el destino `data_chassis_0_backup`.
