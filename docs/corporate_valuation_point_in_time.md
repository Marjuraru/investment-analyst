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
contrato central de unidad del título. Apple, AMD y las dieciocho acciones ordinarias declaran
`reported_common_share`, factor exacto `1`, `adjustment=all` y `security-unit-basis-v1`. Un ADR sin
ratio depositario declarado, o sin revisión elegible en el corte, queda `share_basis_unavailable`;
ETF y cripto quedan `not_applicable`. El catálogo conserva por separado los perfiles Bitcoin,
Ethereum y altcoin y no habilita activos nuevos.

## Selección temporal y compatibilidad

- precio: último cierre diario en o antes de `valuation_date`, con `available_at <= known_at`;
- fundamentales: último ejercicio anual SEC con `period_end <= valuation_date` y aceptación
  `available_at <= known_at`;
- una revisión posterior solo entra cuando su aceptación entra en el corte;
- revisiones igualmente disponibles y semánticamente distintas fallan como ambigüedad;
- los hechos de duración comparten emisor, source ID, frecuencia anual, ejercicio, `period_start`,
  `period_end`, filing, moneda y una única taxonomía contable monetaria (`us-gaap` o `ifrs-full`);
- el número de acciones puede ser nativo de esa misma taxonomía monetaria o
  `dei:EntityCommonStockSharesOutstanding`; DEI se admite exclusivamente para
  `fundamental.shares_outstanding` y con el mismo filing, cierre, forma, año/período y aceptación;
- los hechos instantáneos comparten `period_end` y revisión;
- si sólo existen acciones y ningún hecho monetario, no se infiere base contable ni moneda de
  reporte; la valoración puede calcular `market_cap` pero no inventa una taxonomía contable;
- no se mezclan ejercicios, emisores, fuentes, frecuencias o monedas para completar ausencias;
- `computed_at` puede ser posterior a `known_at`; `available_at` del resultado es el máximo de sus
  inputs y nunca se sustituye por el reloj de cálculo.

El snapshot informa `price_age_days`; un precio con más de tres días se presenta con una limitación,
pero no se oculta. No existe FX: una moneda de reporte distinta a la cotización produce
`currency_mismatch`. La base común v1 declara compatibilidad entre el precio Alpaca
`adjustment=all` y la unidad reportada, pero también declara que las acciones proceden del cierre
anual: no se infieren recompras, emisiones o conversiones posteriores.

La combinación autorizada es una única taxonomía monetaria (`us-gaap` o `ifrs-full`) más DEI
exclusivamente para el número de acciones. DEI no es una taxonomía contable, no altera las fórmulas,
versiones, ratios ADR, moneda ni el significado de las métricas, y cada input conserva su
`taxonomy`/`tag`/`observation_id` de origen. Las observaciones DEI usan
`sec-companyfacts-dei-shares-v1`; las nativas conservan su versión. La base contable se determina
sólo por los hechos monetarios, nunca por el primer elemento de la colección. Si una acción nativa
pertenece a una taxonomía distinta de la monetaria, el resultado es
`accounting_basis_mismatch`; si no hay hechos monetarios, `report_currency` queda ausente.

## Base documentada acción↔ADS (`security-unit-basis-v2`)

Un ADR usa `CatalogAsset.security_unit_basis_revisions`, una tupla ordenada de revisiones estrictas
`SecurityUnitBasisRevision`. Cada revisión declara `schema_version=security-unit-basis-v2`,
`basis=depositary_receipt`, `factor` Decimal positivo y finito, `adjustment=all`,
`reported_share_unit=common_share`, `valid_from` inclusivo, `valid_to` exclusivo opcional,
`available_at` UTC, CIK, accession, `source_url` y `source_sha256` de los bytes oficiales. El campo
`revision` es el SHA-256 canónico de todos los campos semánticos, excluyendo el propio `revision` y
el literal de esquema; un valor declarado que no coincida falla cerrado.

Los cuatro campos estáticos (`security_unit_factor`, `security_unit_basis`,
`security_unit_basis_version`, `security_unit_market_adjustment`) y las revisiones son
representaciones mutuamente excluyentes: declarar ambas, o revisiones fuera de orden, invalida el
catálogo. Las dieciocho bases ordinarias legacy conservan exactamente su camino, valores, IDs y
claves de reuso; los snapshots históricos sin evidencia siguen siendo legibles.

Declaración real sellada el 2026-10-28 (bytes oficiales leídos una vez con la identidad SEC
configurada; sólo se conservan metadatos y extractos mínimos en Git):

| Activo | CIK | Accession | Documento | Unidad | Factor | Disponibilidad |
| --- | --- | --- | --- | --- | --- | --- |
| `equity:us:tsm` | `0001046179` | `0001628280-26-025362` | `exhibit21.htm` (EX-2.1, 20-F 2026-04-16) | 1 ADS = 5 acciones comunes | `0.2` | `2026-04-17T00:00:00Z` |
| `equity:us:bvn` | `0001013131` | `0001104659-26-053101` | `R1.htm` (20-F 2026-04-30) | 1 ADS = 1 acción común | `1` | `2026-05-01T00:00:00Z` |

La cota de disponibilidad es conservadora: el día siguiente a la presentación, no un timestamp de
aceptación SEC inventado. `valid_from` coincide con esa cota y la vigencia queda abierta hasta una
revisión documentada; no existe vigilancia automática de acciones corporativas. El SHA-256 de TSM
es `9b4a4e6c…805a141`, el de BVN `ec2067c4…3e8dd` y el fixture
`tests/fixtures/valuation/adr_basis_sources.json` conserva la referencia completa.

Resolución única compartida por consulta, refresh, cobertura y export:

1. filtrar revisiones con `available_at <= known_at`;
2. colapsar cada `valid_from` a su revisión de mayor disponibilidad elegible; un empate con
   contenido semánticamente distinto falla como ambigüedad, nunca por UUID u orden;
3. seleccionar por intervalo `[valid_from, valid_to)` sobre `valuation_date`; dos tramos distintos
   que cubran la fecha sin resolución inequívoca fallan cerrados;
4. comprobar que la fecha del precio seleccionado también pertenece al tramo.

Así, una revisión correctora que cierra un intervalo no permite volver a la versión anterior abierta,
y un cambio de factor o intervalo publicado después del corte no altera reconstrucciones anteriores.
`share_basis_unavailable` indica ausencia de revisión elegible; `corporate_action_basis_unavailable`
indica ambigüedad de declaración; `security_unit_mismatch` indica precio fuera del tramo o unidad
incompatible. No se aplica un factor por defecto `1` a un ADR.

`ValuationSecurityBasis` conserva sus cuatro campos y admite `basis_evidence` opcional con la
revisión seleccionada. Cuando existe evidencia, `contract_version` es
`security-unit-basis-v2:<revision>` y los parámetros persistidos, el snapshot, la API, el export y el
panel existente exponen fuente, SHA-256, factor, unidad, disponibilidad y tramo. `available_at` de
cada métrica y del snapshot incorpora la disponibilidad de la base documental; nunca `computed_at`.
La identidad UUIDv8 liga el digest de la revisión y sus parámetros, de modo que un cambio de ratio
crea una identidad distinta sin reescribir historia, y un replay idéntico conserva IDs.

Capacidad ADR no equivale a cobertura completa. TSM sólo usa hechos USD ya normalizados y BVN
conserva sus ausencias legítimas: no hay FX, TTM, IFRS trimestral ni estimación de deuda o D&A. El
número de acciones anuales puede provenir de DEI; si falta, la ausencia es legítima y no se completa
con otro ejercicio. No se convierte moneda del PDF.

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
UUIDv5 legacy incorpora activo, clave, `known_at`, fecha de valoración, base anual, período, precio,
versión de unidad, IDs de inputs y algoritmo. En workspace v2 la identidad es UUIDv8, excluye el
corte y liga la versión, el factor y el digest de la revisión ADR cuando existe. La misma evidencia
y semántica reutiliza identidad; una revisión o input distinto crea historia append-only. Cada
métrica referencia solo sus inputs transitivos exactos. Los estados no evaluables no se persisten
como cero: permanecen en la proyección y en los contadores operativos.

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
