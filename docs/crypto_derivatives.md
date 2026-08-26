# Derivados cripto Deribit

## Alcance productivo

La vertical de derivados incorpora evidencia pública y descriptiva de Deribit para
`crypto:btc-usd` y `crypto:eth-usd`. No crea activos nuevos ni modifica la historia spot de
Coinbase. Cada activo resuelve tres capacidades independientes desde el catálogo:

- funding horario del perpetuo;
- velas diarias del índice DVOL;
- snapshot prospectivo del book summary del perpetuo.

Las seis fuentes inmutables son:

- `deribit:btc-perpetual:funding-rate-history` y
  `deribit:eth-perpetual:funding-rate-history`;
- `deribit:btc:dvol:daily` y `deribit:eth:dvol:daily`;
- `deribit:btc-perpetual:book-summary` y
  `deribit:eth-perpetual:book-summary`.

Deribit se usa únicamente mediante HTTPS GET sobre
`https://www.deribit.com/api/v2` y los métodos públicos
`get_funding_rate_history`, `get_volatility_index_data` y
`get_book_summary_by_instrument`. No se configura API key, cuenta, WebSocket, método privado,
trading, posición u orden.

## Contrato de provider e intervalos

Las fechas públicas son inclusivas y se convierten a un intervalo interno UTC half-open. Funding
se solicita en chunks máximos de 31 días y siempre se filtra con
`start <= timestamp < end`: una fila devuelta exactamente en `end_timestamp` no entra en el
information set. `interest_1h` e `interest_8h` son campos históricos distintos.

DVOL usa resolución literal `1D`, chunks máximos de 366 días y paginación hacia atrás. El valor
`continuation` pasa a ser el próximo `end_timestamp`; repetición, ciclo, dirección no decreciente o
valor fuera del intervalo falla cerrado. El resultado final se ordena, conserva OHLC exacto y no
interpola días.

Book summary exige exactamente un instrumento perpetuo y conserva los nullable como ausentes. El
campo raw de Deribit `last` se mantiene en el payload y se promueve explícitamente como observación
`last_price`. `current_funding`, el snapshot `funding_8h` y los históricos `interest_1h` e
`interest_8h` no son intercambiables.

Cada respuesta está acotada a 4 MiB y 1.000 filas históricas. JSON truncado, números no finitos,
shape inesperado, error JSON-RPC o estado HTTP fallan sin publicar un receipt de cobertura. El
transporte compartido reintenta sólo fallos transitorios dentro de su presupuesto y el cliente
aplica pacing mínimo de 250 ms; esto no constituye un SLA ni una garantía de rate limit.

## Evidencia, disponibilidad e identidad

Los endpoints históricos no son un vintage archive demostrado. Una revisión sólo está disponible
desde su primera recepción local: `available_at = received_at`. Un replay anterior a esa recepción
no ve el backfill, aunque el evento sea histórico. `computed_at` nunca sustituye esa disponibilidad.

Los schemas son `deribit-funding-point-v1`, `deribit-dvol-candle-v1`,
`deribit-perpetual-summary-v1` y `deribit-fetch-receipt-v1`. Raw, observaciones y métricas usan UUID5
deterministas sobre preimágenes canónicas; una revisión idéntica reutiliza identidad y primera
disponibilidad, mientras un payload corregido crea una revisión append-only. Dos revisiones
semánticamente diferentes con la misma disponibilidad elegible son ambiguas y fallan.

Un receipt se escribe sólo después de completar todas las páginas del intervalo lógico. Un
intervalo vacío válido tiene receipt y cero observaciones. El receipt no es una barra, no entra a
analytics y no modifica `as_of`. Un fallo tardío conserva stages completos anteriores, sin rollback
global. El schema y el formato de workspace continúan en v1 sin migración.

## Normalización y analytics

Funding produce `funding_interest_1h`, `funding_interest_8h`, `index_price` y
`prev_index_price`. DVOL produce `dvol_open`, `dvol_high`, `dvol_low` y `dvol_close`. Summary puede
producir OI, precios, `last_price`, funding de snapshot, volumen y cambio de precio; no fabrica
ceros para nullable.

`crypto-derivatives-metrics-v1-decimal34` calcula con precisión Decimal34 y fuentes aisladas:

- suma y media de `funding_interest_1h` para 24, 168 y 720 horas consecutivas;
- cambio de `dvol_close` para 1, 7 y 30 días exactos;
- spread `(ask - bid) / mid * 10000` desde tres observaciones del mismo snapshot.

Ventanas incompletas, gaps, fuentes o activos mezclados, inputs posteriores al corte y denominadores
inválidos omiten el resultado y declaran el requisito faltante. No se anualiza funding ni se calcula
un delta de OI irregular.

`crypto-derivatives-diagnostic-v1` es read-only y multidimensional. Publica estado, signo descriptivo
de funding 168h, dirección del cambio DVOL 7d, OI/funding actuales, spread, cobertura, IDs y
limitaciones. No contiene score agregado, confidence, veredicto, ranking, señal o recomendación y no
mezcla derivados con Coinbase spot, fundamentales, valoración o macro.

## CLI, runtime y scheduler

Los comandos reutilizan `ApplicationRuntime`, el catálogo y un access mode explícito:

```bash
.venv/bin/python scripts/refresh_crypto_derivatives.py \
  --workspace /ruta/workspace-inicializado \
  --asset-id crypto:btc-usd \
  --start 2026-08-01 \
  --end 2026-08-07 \
  --refresh-mode auto

.venv/bin/python scripts/query_crypto_derivatives.py \
  --workspace /ruta/workspace-inicializado \
  --asset-id crypto:btc-usd \
  --start 2026-08-01 \
  --end 2026-08-07 \
  --known-at 2026-08-11T00:00:00Z
```

`auto` detecta sólo bordes prefix/suffix mediante receipts; no interpreta fines de semana, feriados
ni gaps internos. `full` vuelve a solicitar el intervalo sin borrar ni duplicar historia. Cada
refresh ejecuta funding, DVOL, summary actual y métricas en ese orden; el summary se captura incluso
cuando los históricos ya están cubiertos. Query no crea transporte ni escribe storage.

## Consulta local HTTP e interfaz

La interfaz loopback ofrece `GET /api/v1/crypto-derivatives` exclusivamente para activos cuyo
descriptor `market-asset-universe-v5` declara `supports_crypto_derivatives=true`. La elegibilidad
se resuelve al completar las tres capacidades Deribit del catálogo; no existe una lista HTTP o de
navegador de símbolos permitidos. La ruta acepta exactamente `asset_id`, `start`, `end` y
`known_at`, con límites inclusivos `YYYY-MM-DD` y un corte ISO 8601 con zona.

La ruta construye `CryptoDerivativesQueryRequest` y llama sólo a la consulta read-only de la
fachada. Devuelve sin transformación `crypto-derivatives-query-result-v1`; no inicializa workspace,
no usa transporte Deribit, no recalcula ni escribe. El panel visible es independiente del gráfico
spot, carga una ventana UTC de 90 días bajo demanda y conserva cobertura, frescura, ausencias,
limitaciones e IDs como evidencia, sin score, señal ni recomendación.

Para cada criptoactivo seleccionado y elegible, el scheduler registra
`deribit:<asset_id>:crypto-derivatives`, dominio `crypto_derivatives`, a +10 minutos del job diario y
con frescura de 36 horas. Solicita una ventana móvil de 90 días terminada en el último día UTC
cerrado; después el planner limita la red a bordes faltantes y conserva el snapshot prospectivo.

## Limitaciones

La integración demuestra acceso público práctico, no historia completa, vintage, retención eterna,
SLA, gratuidad permanente, licencia de redistribución ni representatividad de todo el mercado
cripto. Los payloads permanecen locales. Cambios de términos, fields, instrumentos, acceso o rate
limits requieren revalidación. Opciones, superficies IV, Greeks, futures fechados, basis, trades,
liquidaciones, order book y cualquier ejecución quedan fuera de este contrato.
