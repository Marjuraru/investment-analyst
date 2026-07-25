# Base intradía de BTC-USD

## Alcance

La primera base intradía usa el endpoint público
[Get product candles](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles)
de Coinbase Exchange para `BTC-USD`. Es una fuente paralela y no reemplaza la historia diaria
existente:

- activo: `crypto:btc-usd`;
- fuente: `coinbase-exchange:btc-usd:minute-1-candles`;
- frecuencia persistida: `MINUTE_1`;
- mercado 24/7 y buckets alineados al epoch UTC;
- intervalos derivados: `1m`, `5m`, `15m`, `30m`, `45m`, `1h`, `2h`, `4h` y `5h`.

Coinbase Exchange representa un solo mercado, no un precio agregado de todo el ecosistema. Su API
puede omitir intervalos sin operaciones y limita cada solicitud a 300 velas. El cliente divide el
rango en páginas de 300 minutos y aplica una pausa conservadora respecto del
[límite público de REST](https://docs.cdp.coinbase.com/exchange/rest-api/rate-limits). El importador
registra las ausencias y la agregación marca un bucket incompleto; nunca inventa una vela ni rellena
un precio.

## Persistencia e identidades

Cada vela de un minuto produce un `RawRecord` y cinco observaciones OHLCV. Los identificadores
dependen del contenido y del contrato de fuente, por lo que una repetición equivalente reutiliza las
mismas identidades y una revisión semánticamente distinta crea una nueva versión append-only. La
consulta selecciona únicamente evidencia con `available_at <= known_at`.

Los intervalos mayores se calculan en memoria con `Decimal`:

- apertura de la primera vela;
- máximo de los máximos;
- mínimo de los mínimos;
- cierre de la última vela;
- suma de volumen;
- peor calidad de las entradas;
- evidencia exacta de registros y observaciones.

Un bucket es completo solo cuando contiene todas las marcas de minuto esperadas. Los agregados no se
persisten y no participan en métricas ni diagnósticos diarios.

## Importación acotada

Una ejecución admite como máximo 24 horas para mantener predecibles el número de solicitudes, la
memoria y el reporte de intervalos ausentes. Para backfills extensos se ejecutan ventanas consecutivas
sin solapamiento; la idempotencia permite repetir una ventana segura.

```bash
.venv/bin/python scripts/fetch_coinbase_intraday.py \
  --workspace ~/.local/share/investment-analyst/workspaces/default \
  --start 2026-07-24T00:00:00Z \
  --end 2026-07-25T00:00:00Z
```

El resultado debe declarar `interval: "1m"`, la fuente intradía, conteos creados o reutilizados,
intervalos ausentes y `traceability_verified: true`.

## Consulta y agregación local

Una consulta admite como máximo siete días y limita por defecto la salida a 100 buckets. Este límite
acota la reconstrucción de observaciones de un minuto aunque el workspace crezca.

```bash
.venv/bin/python scripts/query_coinbase_intraday.py \
  --workspace ~/.local/share/investment-analyst/workspaces/default \
  --start 2026-07-24T00:00:00Z \
  --end 2026-07-25T00:00:00Z \
  --known-at 2026-07-25T00:05:00Z \
  --interval 1h \
  --limit 24
```

La consulta es de solo lectura y no contacta al proveedor. Devuelve conteos de buckets completos e
incompletos, las barras solicitadas y `traceability_verified: true`.

## Límites de esta fase

- La interfaz web aún no consume la fuente intradía.
- El scheduler persistente continúa ejecutando únicamente el flujo diario de Apple.
- No existe backfill automático intradía ni calendario de sesiones para AAPL.
- No se mezclan resultados diarios e intradía, ni mercado y fundamentales.
