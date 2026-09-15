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

Medición realizada mediante `shutil.disk_usage` y `os.path.getsize`:

| Superficie | Bytes Exactos | Representación Binaria (GiB) | Representación Decimal (GB) |
| :--- | :--- | :--- | :--- |
| **Disco `C:` (Total)** | `509.722.226.688` | 474,72 GiB | 509,72 GB |
| **Disco `C:` (Libre)** | `31.875.776.512` | 29,69 GiB | 31,88 GB |
| **Disco `C:` (Usado)** | `477.846.450.176` | 445,03 GiB | 477,85 GB |
| **VHD WSL `/` (Total)** | `1.081.101.176.832` | 1.006,85 GiB | 1.081,10 GB |
| **VHD WSL `/` (Libre)** | `982.089.129.984` | 914,64 GiB | 982,09 GB |
| **VHD WSL `/` (Usado)** | `44.019.691.520` | 41,00 GiB | 44,02 GB |
| **Workspace Restaurado (Total)** | `8.511.630.696` | 7,9271 GiB | 8,5116 GB |
| **Base DuckDB (`investment_analyst.duckdb`)** | `7.219.458.048` | 6,7236 GiB | 7,2195 GB |
| **DuckDB WAL (`investment_analyst.duckdb.wal`)** | `0` | 0 B | 0 B |

*Nota sobre almacenamiento:* El VHD de WSL reporta un espacio disponible virtual no respaldado físicamente en disco; el límite restrictivo real del host es el espacio libre del volumen `C:`.

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

## 8. Planes de Ejecución `EXPLAIN` de Relaciones de Linaje

Las 4 consultas canónicas de verificación de linaje relacional fueron verificadas mediante `EXPLAIN`. Se constata que ninguna utiliza `TOP_N`, `LEFT_DELIM_JOIN`, `CROSS JOIN LATERAL` ni `HASH_GROUP_BY` sobre `document_json`, manteniendo escaneos acotados y directos:

### Relación 1: `observation_raw`
```sql
EXPLAIN
SELECT o.raw_record_id
FROM normalized_observations o
ANTI JOIN raw_record_index r ON o.raw_record_id = r.record_id
LIMIT 1;
```
Plan:
```text
┌───────────────────────────┐
│      STREAMING_LIMIT      │
└─────────────┬─────────────┘
┌─────────────┴─────────────┐
│         HASH_JOIN         │
│    ────────────────────   │
│   Join Type: RIGHT_ANTI   │
│        Conditions:        │
│    record_id = raw_rec... │
└─────────────┬─────────────┘
```

### Relación 2: `metric_observation`
```sql
EXPLAIN
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
Plan:
```text
┌───────────────────────────┐
│      STREAMING_LIMIT      │
└─────────────┬─────────────┘
┌─────────────┴─────────────┐
│         HASH_JOIN         │
│    ────────────────────   │
│   Join Type: RIGHT_ANTI   │
│        Conditions:        │
│   observation_id = ref_id │
└─────────────┬─────────────┘
        Proyección con UNNEST directo sobre SEQ_SCAN(metric_results)
```

### Relación 3: `metric_metric`
```sql
EXPLAIN
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
Plan:
```text
┌───────────────────────────┐
│      STREAMING_LIMIT      │
└─────────────┬─────────────┘
┌─────────────┴─────────────┐
│         HASH_JOIN         │
│    ────────────────────   │
│   Join Type: RIGHT_ANTI   │
│        Conditions:        │
│     result_id = ref_id    │
└─────────────┬─────────────┘
        Proyección con UNNEST directo sobre SEQ_SCAN(metric_results)
```

### Relación 4: `diagnostic_metric`
```sql
EXPLAIN
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
Plan:
```text
┌───────────────────────────┐
│      STREAMING_LIMIT      │
└─────────────┬─────────────┘
┌─────────────┴─────────────┐
│         HASH_JOIN         │
│    ────────────────────   │
│   Join Type: RIGHT_ANTI   │
│        Conditions:        │
│     result_id = ref_id    │
└─────────────┬─────────────┘
        Proyección con UNNEST directo sobre SEQ_SCAN(diagnostic_results)
```

---

## 9. Copia Temporal de Auditoría y Decisión Humana

- **Identificador y topología de la copia restaurada:** Destino temporal externo `data_chassis_0_restored` (fuera del VHD de WSL)
- **Tamaño total:** `8.511.630.696` bytes (7,9271 GiB).
- **Inventario:** 294.815 archivos verificados con hash SHA-256 idéntico al backup original.
- **Decisión humana:** Queda a criterio humano (`HUMAN`) la conservación de esta copia temporal para auditorías posteriores o su eliminación para recuperar ~8,5 GB en el disco físico. El backup durable y verificado reside en el destino externo `data_chassis_0_backup`.
