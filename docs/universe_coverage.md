# Cobertura de universo

`scripts/query_universe_coverage.py` expone una matriz local, read-only y point-in-time de la
configuración de catálogo y de las barras diarias disponibles. Requiere un workspace explícito y
nunca inicializa, refresca, modifica archivos, escribe almacenamiento ni contacta proveedores.

La respuesta `universe-coverage-v1` identifica el `catalog_version` y un SHA-256 del documento
canónico. La configuración `supported` y la evidencia `present` son conceptos distintos:
`missing` significa que la consulta local no encontró barras elegibles al corte, no precio, volumen
ni actividad cero. El informe no es una señal, ranking, recomendación ni instrucción operativa.

La cobertura de acciones y ETF usa Alpaca Market Data con feed IEX y adjustment `all`; IEX no es
SIP consolidado. Las nuevas altcoins usan únicamente velas diarias Coinbase y el volumen usa el
token. Su límite de inicio 2025-01-01 es una restricción conservadora de solicitud, no la primera
cotización. Las acciones nuevas no declaran base documental de unidades de acción: valoración
corporativa queda no configurada y no se heredan ratios a ETF o cripto.

```bash
PYTHONPATH=src .venv/bin/python scripts/query_universe_coverage.py \
  --workspace /ruta/al/workspace --known-at 2026-08-29T23:00:00Z \
  --market-start 2026-08-01 --market-end 2026-08-28 \
  --fundamental-start 2020-01-01 --fundamental-end 2026-08-28 \
  --asset-id crypto:sol-usd --asset-id equity:us:msft
```
