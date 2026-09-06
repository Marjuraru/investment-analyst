# Cartera institucional pública efectiva

`sec-13f-effective-public-holdings-policy-v1` compone, sólo en memoria y bajo un
`known_at` explícito, las filas 13F públicas de un gestor y período. No persiste una
cartera, no normaliza valores ni crea métricas, score, señal o recomendación.

La composición primero exige el cierre 13F válido existente. El original inicia el
conjunto; un `RESTATEMENT` visible sustituye completamente el conjunto anterior; y
solamente `NEW HOLDINGS ENTRIES` posteriores al contribuyente base lo suplementan.
Cada contribuyente debe declarar totales de filas y valor que coincidan exactamente
con sus filas observadas. Ante cadena ambigua, evidencia incompleta o total no
verificable el resultado no expone filas.

La consulta usa `ApplicationRuntime` con workspace de sólo lectura y conserva
artifact, accession, identidad literal de fila e índice de contribuyente. La escala
monetaria sigue siendo `unresolved`; confidencialidad, tipo de reporte y otros
gestores se conservan como limitaciones declaradas. Ejemplo:

```bash
python scripts/query_sec_institutional_effective_holdings.py --workspace /ruta/workspace \
  --manager-cik 0001067983 --report-period 2024-12-31 \
  --known-at 2025-02-14T00:00:00Z
```
