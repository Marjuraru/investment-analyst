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
