# Correspondencia de instrumento point-in-time

`instrument-correspondence-v1` conserva una afirmación declarada de `asset_id`, CUSIP, clase,
ventana económica y `available_at`. Su identidad determinista es
`cusip|title_of_class|asset_id|effective_from|effective_to|available_at|schema_version`: una
corrección posterior coexiste; una redeclaración idéntica es idempotente. La versión del catálogo y
el declarante son procedencia del RawRecord, no parte de la afirmación ni de su identidad; una
redeclaración conserva la provenance original sin reescribirla.

La declaración valida el binding CUSIP del catálogo SEC actual. La consulta usa exclusivamente
evidencia persistida al `known_at`; no vuelve a leer el catálogo. Posiciones sin correspondencia,
con período ausente, fuera de ventana o ambiguas se devuelven explícitamente sin vínculo. No
modifica evidencia 13F ni produce métricas, señales o recomendaciones.

```bash
.venv/bin/python scripts/declare_instrument_correspondence.py \
  --root /ruta/al/storage --asset-id equity:us:aapl --cusip 037833100 \
  --title-of-class COM --effective-from 1980-12-12 \
  --available-at 2025-02-14T18:00:00Z --recorded-at 2025-02-15T00:00:00Z \
  --declared-by analyst
```

La proyección local es estrictamente de lectura y requiere `manager_cik` porque ése es el eje de
consulta del corpus Form 13F. Nunca elige una correspondencia ambigua de forma arbitraria y expone
la falta de período como `missing_report_period`:

```bash
.venv/bin/python scripts/query_institutional_holdings_by_asset.py \
  --root /ruta/al/storage --asset-id equity:us:aapl --filer-cik 0001067983 \
  --known-at 2025-02-15T00:00:00Z
```

## Correlación automática por fila

`SEC-CORPUS-29` añade un contrato **aislado** para las coincidencias que no requieren declaración
humana: `sec-institutional-row-correspondence-v1`, bajo el source ID
`sec-edgar:institutional-row-correspondence` y la política
`sec-institutional-row-correspondence-policy-v1`.

Este contrato **no** modifica, reemplaza ni reinterpreta `instrument-correspondence-v1`: no cambia su
identidad, su persistencia, su procedencia ni sus consumidores. Una declaración manual sigue siendo una
afirmación humana con ventana económica libre; el claim row-scoped prueba algo distinto y más estrecho:
que el CUSIP exacto de **una fila** as-filed coincide con el CUSIP del candidato de universo ya derivado
del catálogo para **ese mismo período reportado**. Nunca establece una identidad corporativa perpetua ni
infiere por ticker, nombre de emisor, FIGI, ISIN o similitud de texto.

La vigencia es cerrada y no configurable: `effective_from == report_period` y
`effective_to == report_period + 1 día`. `available_at` es exactamente
`max(snapshot.available_at, artifact.available_at)`, y `event_time` es el inicio UTC del período
reportado. El detalle de identidad, lineage, resolución determinista y materialización está en
[Posiciones institucionales SEC Form 13F](sec_institutional_holdings.md#correspondencia-verificable-por-fila).
