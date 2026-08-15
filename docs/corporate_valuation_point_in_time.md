# Valoración corporativa point-in-time

La valoración corporativa es un dominio descriptivo independiente de los diagnósticos de mercado y
fundamentales. No produce recomendaciones, rankings, scores, confianza agregada, precio objetivo,
TTM, estimaciones, consenso ni DCF.

## Consulta y alcance

`GET /api/v1/valuation` requiere `asset_id`, `known_at` UTC y `valuation_date` (`YYYY-MM-DD`);
`basis` solo admite `latest_annual`. La ruta abre el workspace en modo read-only, no toma el writer
y no llama a Alpaca, SEC ni otro proveedor. La interfaz carga el panel «Valoración» bajo demanda y
permite exportar el snapshot JSON exacto; el redondeo de moneda, ratio y porcentaje ocurre solo en
la presentación.

La v1 aplica únicamente a `listed_company` + `corporate` con mercado diario, SEC configurada y un
contrato central de unidad del título. Apple y AMD declaran `reported_common_share`, factor exacto
`1`, `adjustment=all` y `security-unit-basis-v1`. Un ADR sin ratio depositario explícito queda
`share_basis_unavailable`; ETF y cripto quedan `not_applicable`. El catálogo conserva por separado
los perfiles Bitcoin, Ethereum y altcoin y no habilita activos nuevos.

## Selección temporal y compatibilidad

- precio: último cierre diario en o antes de `valuation_date`, con `available_at <= known_at`;
- fundamentales: último ejercicio anual SEC con `period_end <= valuation_date` y aceptación
  `available_at <= known_at`;
- una revisión posterior solo entra cuando su aceptación entra en el corte;
- revisiones igualmente disponibles y semánticamente distintas fallan como ambigüedad;
- los hechos de duración comparten emisor, source ID, frecuencia anual, ejercicio, `period_start`,
  `period_end`, filing, moneda y taxonomía compatible;
- los hechos instantáneos comparten `period_end` y revisión;
- no se mezclan ejercicios, emisores, fuentes, frecuencias o monedas para completar ausencias;
- `computed_at` puede ser posterior a `known_at`; `available_at` del resultado es el máximo de sus
  inputs y nunca se sustituye por el reloj de cálculo.

El snapshot informa `price_age_days`; un precio con más de tres días se presenta con una limitación,
pero no se oculta. No existe FX: una moneda de reporte distinta a la cotización produce
`currency_mismatch`. La base común v1 declara compatibilidad entre el precio Alpaca
`adjustment=all` y la unidad reportada, pero también declara que las acciones proceden del cierre
anual: no se infieren recompras, emisiones o conversiones posteriores.

## Fórmulas v1

Todas usan `Decimal` con precisión 34 y claves `valuation.corporate.*`.

| Métrica | Fórmula | Regla de evaluación |
| --- | --- | --- |
| `market_cap` | `close_price × shares_outstanding × market_units_per_reported_share` | acciones y resultado positivos |
| `financial_debt` | `commercial_paper + long_term_debt_current + long_term_debt_noncurrent` | los tres componentes son obligatorios |
| `enterprise_value` | `market_cap + financial_debt - cash_and_cash_equivalents` | no infiere cash o deuda ausentes |
| `price_to_earnings_latest_annual` | `market_cap / annual_net_income` | utilidad estrictamente positiva |
| `price_to_book` | `market_cap / stockholders_equity` | patrimonio estrictamente positivo |
| `price_to_sales_latest_annual` | `market_cap / annual_revenue` | ingresos estrictamente positivos |
| `enterprise_value_to_sales_latest_annual` | `enterprise_value / annual_revenue` | ingresos estrictamente positivos |
| `enterprise_value_to_ebit_latest_annual` | `enterprise_value / annual_operating_income` | EBIT estrictamente positivo |
| `enterprise_value_to_ebitda_latest_annual` | `enterprise_value / (annual_operating_income + annual_depreciation_and_amortization)` | EBITDA estrictamente positivo y D&A oficial compatible |
| `free_cash_flow_yield_latest_annual` | `(annual_operating_cash_flow - annual_capital_expenditures) / market_cap` | conserva signo; market cap positivo |
| `earnings_yield_latest_annual` | `annual_net_income / market_cap` | conserva signo; market cap positivo |

EV v1 no añade leases, preferred equity, minoritarios ni inversiones negociables. D&A se normaliza
solo desde los conceptos oficiales `us-gaap:DepreciationDepletionAndAmortization` e
`ifrs-full:DepreciationAndAmortisationExpense`; si falta, EV/EBITDA queda `ebitda_unavailable` y no
se sustituye silenciosamente por EBIT.

## Estados, identidad y persistencia

Cada métrica es `evaluated`, `not_evaluable` o `not_applicable`; el snapshot puede ser `evaluated`,
`partial`, `not_evaluable` o `not_applicable`, sin convertir cobertura en veredicto. Los reason
codes cerrados incluyen configuración ausente, base del título, precio o filing ausente/ambiguo,
revisión ambigua, período/fuente/frecuencia/base contable/moneda/unidad incompatible, input ausente,
denominador inválido y EBITDA no disponible.

Las métricas evaluadas se persisten como `MetricResult` con categoría `valuation`. El `result_id`
UUIDv5 incorpora activo, clave, `known_at`, fecha de valoración, base anual, período, precio,
versión de unidad, IDs de inputs y algoritmo. La misma evidencia y semántica reutiliza identidad;
una revisión o input distinto crea historia append-only. Cada métrica referencia solo sus inputs
transitivos exactos. Los estados no evaluables no se persisten como cero: permanecen en la
proyección y en los contadores operativos.

Los refresh de mercado cotizado, fundamentales SEC y el bootstrap Apple calculan o reutilizan la
valoración después de persistir nueva evidencia, dentro de la misma conexión writer. No realizan
otra llamada de proveedor. Un fallo tardío de valoración no revierte raw records, observaciones o
métricas ya guardadas. ETF y cripto no reciben esa etapa.

## Historia materializada

`GET /api/v1/valuation-history` consulta sólo `MetricResult` de valoración ya materializados. Exige
un corte UTC, rango inclusivo, `basis=latest_annual` y límite acotado. Selecciona la revisión con el
mayor `parameters.known_at` elegible sin desempatar por UUID; conserva series sparse, IDs e inputs y
publica únicamente estadísticas descriptivas Decimal. No es backfill, TTM, forward, ranking ni regla.

## Regla relativa explícita

`GET /api/v1/valuation-history-rule` evalúa bajo demanda una regla immutable y versionada sobre una
única serie homogénea ya seleccionada por la historia materializada. Usa sólo puntos estrictamente
anteriores al punto actual y el percentil empírico Decimal34 de rango medio `(menores + 0.5 × iguales) / N`.
La salida `met`, `not_met` o `not_evaluable` conserva IDs, cobertura, fórmula y limitaciones; no
persiste evaluaciones, no llama proveedores y no expresa barato/caro, señal o recomendación.

## Limitación de mercado

El precio de las acciones configuradas procede de Alpaca Market Data, feed IEX, barras diarias y
`adjustment=all`. IEX cubre una bolsa y no el SIP consolidado. La limitación acompaña cada snapshot,
definición que usa precio y presentación correspondiente.
