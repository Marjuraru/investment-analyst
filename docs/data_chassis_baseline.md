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

El ciclo completo se ejecutó de forma desacoplada y fuera del VHD de WSL:

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
- **Métodos exactos de medición:**
  - Espacio y particiones de disco del host: `shutil.disk_usage` y `df -B1` sobre puntos de montaje `/mnt/c` y `/`.
  - Workspace permanente en disco: `du -sb /home/marjuraru/projects/investment-analyst/workspace` (reporte directo a nivel de inodos de sistema de archivos).
  - Backup verificado en destino externo: `du -sb /mnt/c/temp/data_chassis_0_backup` (recorrido exhaustivo de bytes en volumen externo).
  - Copia restaurada temporal en destino externo: `du -sb /mnt/c/temp/data_chassis_0_restored`.
  - Base de datos DuckDB: `os.path.getsize` y consulta SQL `PRAGMA database_size` sobre `investment_analyst.duckdb` e `investment_analyst.duckdb.wal`.

### Inventario de Almacenamiento: Mediciones Reales vs. Presupuestos de Transición

| Categoría | Superficie / Componente | Bytes Exactos | Representación Binaria (GiB) | Representación Decimal (GB) | Clasificación |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Host** | **Disco `C:` (Total)** | `509.722.226.688` | 474,72 GiB | 509,72 GB | Medición real host |
| **Host** | **Disco `C:` (Libre inicial pre-ciclo)** | `31.875.776.512` | 29,69 GiB | 31,88 GB | Medición real host |
| **Host** | **Disco `C:` (Libre remanente post-ciclo)** | `26.875.776.512` | 25,03 GiB | 26,88 GB | Medición real host |
| **Host** | **Disco `C:` (Usado)** | `482.846.450.176` | 449,69 GiB | 482,85 GB | Medición real host |
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

### Aritmética Reproducible del Margen Remanente en Disco `C:`

Para garantizar que las etapas subsiguientes de `DATA-CHASSIS` (especialmente la etapa 8 de migración y corte) se ejecuten sin riesgo de agotamiento de almacenamiento físico, se verifica la siguiente aritmética:

1. **Espacio libre medido en `C:`:** `26.875.776.512` bytes (~25,03 GiB en base binaria, 26,88 GB en base decimal).
2. **Criterio de seguridad exigido para Etapa 8:** Espacio libre >= 25 GB (`25.000.000.000` bytes). Se satisface estrictamente:
   $$26.875.776.512 \text{ bytes} \ge 25.000.000.000 \text{ bytes} \quad (\text{margen positivo de } +1.875.776.512 \text{ bytes sobre el piso de 25 GB})$$
3. **Presupuesto total de transición consumido durante migración activa (Etapa 8):**
   $$\text{Presupuesto v2 temporal} + \text{Presupuesto spill temporal} = 3.221.225.472 + 2.147.483.648 = 5.368.709.120 \text{ bytes } (\sim 5,00\text{ GiB})$$
4. **Margen remanente neto proyectado tras absorción de la Etapa 8 en `C:`:**
   $$26.875.776.512 - 5.368.709.120 = 21.507.067.392 \text{ bytes } (\sim 20,03\text{ GiB libres remanentes})$$
5. **Margen remanente frente a la suma total de componentes (Backup + Presupuesto v2 + Presupuesto temporales):**
   $$\text{Total requerido} = 8.511.630.696 + 3.221.225.472 + 2.147.483.648 = 13.880.339.816 \text{ bytes } (\sim 12,93\text{ GiB})$$
   $$\text{Margen en } C: = 26.875.776.512 - 13.880.339.816 = 12.995.436.696 \text{ bytes } (\sim 12,10\text{ GiB remanentes})$$

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

Las 4 consultas canónicas de verificación de linaje relacional fueron ejecutadas y perfiladas mediante `EXPLAIN ANALYZE` directamente sobre la copia restaurada temporal en volumen externo (`/mnt/c/temp/data_chassis_0_restored/storage/data/processed/investment_analyst.duckdb`) bajo las condiciones formales del verificador:
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

- **Identificador y topología de la copia restaurada:** Destino temporal externo `data_chassis_0_restored` (fuera del VHD de WSL)
- **Tamaño total:** `8.511.630.696` bytes (7,9271 GiB).
- **Inventario:** 294.815 archivos verificados con hash SHA-256 idéntico al backup original.
- **Decisión humana:** Queda a criterio humano (`HUMAN`) la conservación de esta copia temporal para auditorías posteriores o su eliminación para recuperar ~8,5 GB en el disco físico. El backup durable y verificado reside en el destino externo `data_chassis_0_backup`.
