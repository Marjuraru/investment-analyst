# Cobertura de universo

`scripts/query_universe_coverage.py` expone una matriz local, read-only y point-in-time del
catálogo completo: 37 identidades configuradas. Requiere un workspace explícito y nunca inicializa,
refresca, modifica archivos, escribe almacenamiento ni contacta proveedores.

La respuesta `universe-coverage-v1` identifica el `catalog_version` y un SHA-256 del documento
canónico. La configuración `supported` y la evidencia `present` son conceptos distintos:
`missing` significa que la consulta local no encontró evidencia elegible al corte, no precio,
volumen ni actividad cero. `not_configured`, `not_implemented` y `not_applicable` permanecen
separados de `present`, `missing` y `not_queried`. El informe no es una señal, ranking,
recomendación ni instrucción operativa.

La matriz muestra por separado mercado diario, fundamentales SEC, valoración corporativa y registro
BVL local. Cada sección incluye fuentes, período de referencia, `available_at`, edades enteras y,
cuando existe, el linaje de la última barra o registro; no los combina en un score. Las seis
identidades BVL aparecen aunque no tengan mercado diario configurado: su mercado queda
`not_configured` y el registro SMV queda `missing` hasta importar evidencia oficial.

La cobertura de acciones y ETF usa Alpaca Market Data con feed IEX y adjustment `all`; IEX no es
SIP consolidado. Las altcoins usan únicamente velas diarias Coinbase y el volumen usa el token. Su
límite de inicio 2025-01-01 es una restricción conservadora de solicitud, no la primera cotización.
Las acciones sin base documental de unidades de acción quedan `not_evaluable` en valoración con
`share_basis_unavailable`; no se heredan ratios a ETF o cripto.

El final de mercado sólo puede ser un día UTC completamente transcurrido al `known_at`; los rangos
públicos siguen siendo inclusivos. La consulta por defecto recorre todo el catálogo; `--asset-id`
restringe a IDs canónicos únicos y ordenados.

```bash
PYTHONPATH=src .venv/bin/python scripts/query_universe_coverage.py \
  --workspace /ruta/al/workspace --known-at 2026-08-29T23:00:00Z \
  --market-start 2026-08-01 --market-end 2026-08-28 \
  --fundamental-start 2020-01-01 --fundamental-end 2026-08-28 \
  --asset-id crypto:sol-usd --asset-id equity:us:msft
```

El smoke de entrega es deliberadamente distinto de la consulta: crea sólo un workspace scratch nuevo,
refresca las 12 altas con las facades existentes y, después, elimina las credenciales del entorno
antes de repetir la matriz local offline. No acepta un workspace existente ni toca el workspace
permanente:

```bash
scratch_parent=$(mktemp -d /tmp/investment-analyst-universe-smoke.XXXXXX)
set -a; . /home/marjuraru/projects/investment-analyst/.env; set +a
PYTHONPATH=src .venv/bin/python scripts/smoke_universe_coverage.py \
  --workspace "$scratch_parent/workspace" --known-at 2026-08-31T12:00:00Z
```
