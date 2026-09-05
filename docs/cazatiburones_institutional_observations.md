# Observaciones institucionales 13F

La normalización es local, append-only y point-in-time. Sólo emite observaciones cuando una fila
as-filed coincide literalmente por CUSIP y clase con una correspondencia efectiva y visible.
Las opciones se conservan como valor/acciones subyacentes declaradas; no son acciones poseídas.
Antes del 2023-01-03 el valor 13F se multiplica exactamente por 1000; desde esa fecha se conserva
en USD. Esta capa no compone enmiendas ni produce métricas, señales o recomendaciones.

```bash
python scripts/normalize_sec_institutional_observations.py --workspace /tmp/sec-13f \
  --asset-id equity:us:aapl --filer-cik 1067983 --report-id <UUID> --known-at <UTC>
python scripts/query_sec_institutional_observations.py --workspace /tmp/sec-13f \
  --asset-id equity:us:aapl --known-at <UTC>
```

## Ruta HTTP local

La consulta point-in-time paginada se expone en modo de solo lectura mediante:

```text
GET /api/v1/cazatiburones/institutional-observations?asset_id=equity:us:aapl&known_at=2026-07-16T15:47:00Z&offset=0&limit=1000
```

Parámetros soportados:
- `asset_id`: obligatorio; identifica un activo corporativo con configuración SEC en el catálogo.
- `known_at`: obligatorio; corte point-in-time UTC (ISO-8601).
- `manager_cik`: opcional; CIK normalizado del gestor institucional declarante.
- `report_id`: opcional; UUID del reporte de tenencias 13F.
- `cusip`: opcional; CUSIP del instrumento.
- `field_name`: opcional; nombre del campo normalizado (por ejemplo, `sshPrnamt`).
- `offset`: opcional; desplazamiento entero >= 0 (por defecto 0).
- `limit`: opcional; entero entre 1 y 10000 (por defecto 1000).

Devuelve `InstitutionalObservationQueryResult` (`observations`, `total_matching`, `truncated`), preservando la
semántica as-filed sin alteración de valores. Cualquier parámetro adicional o activo sin configuración SEC
se rechaza con error HTTP 400 (`invalid_request`).
