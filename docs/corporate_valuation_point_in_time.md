# Valoración corporativa point-in-time

La valoración corporativa es un dominio descriptivo independiente de los diagnósticos de mercado y
fundamentales. No produce recomendaciones, rankings, scores, confianza agregada, precio objetivo,
TTM, estimaciones, consenso ni DCF.

## Consulta y alcance

`GET /api/v1/valuation` requiere `asset_id`, `known_at` UTC y `valuation_date` (`YYYY-MM-DD`);
`basis` admite `latest_annual` (por defecto) y `selected_annual_filing`, que exige además el selector
completo descrito abajo. La ruta abre el workspace en modo read-only, no toma el writer y no llama a
Alpaca, SEC ni otro proveedor. La interfaz carga el panel «Valoración» bajo demanda y permite
exportar el snapshot JSON exacto; el redondeo de moneda, ratio y porcentaje ocurre solo en la
presentación.

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

## Selección explícita de ejercicio y filing

`GET /api/v1/valuation/filings` lista únicamente contextos anuales SEC ya persistidos y visibles en
`known_at`. Exige `asset_id`, `known_at` UTC, `valuation_date` y un rango inclusivo de cierre
`start_date`/`end_date`; pagina con `offset` y `limit` (por defecto 50, máximo 200). Responde con el
contrato `corporate-valuation-filing-options-v1`: `request`, `total`, paginación y opciones compactas.
Cada opción declara el selector literal —`annual_period_end`, `filing_accession_number`,
`filing_accepted_at`, `filing_form`, `fiscal_year` y `fiscal_period`—, los roles de campo presentes y
las taxonomías observadas. No incluye raws, documentos, lineage completo ni los once cálculos, y no
ordena por cobertura: el orden es cierre descendente, disponibilidad descendente y el resto del
selector ascendente. Una página no representa el universo completo; el cliente debe leer todas las
páginas solicitadas.

`filing_accepted_at` identifica la disponibilidad del contexto persistido (la tupla que ya agrupa la
selección `latest_annual`), no un timestamp de aceptación SEC inventado. Dos disponibilidades del
mismo accession se muestran separadas y no se fusionan, corrigen ni eligen por ser más completas.

`GET /api/v1/valuation?basis=selected_annual_filing` exige los seis campos del selector, además de
`asset_id`, `known_at` y `valuation_date`; un selector parcial, parámetros de selección en modo
`latest_annual` o una base desconocida se rechazan. Sólo entran observaciones `VALID`, anuales, del
activo, source, cierre, disponibilidad, accession, forma, año y período exactos, visibles en
`known_at` y con `annual_period_end <= valuation_date`. Una selección bien formada sin evidencia
visible devuelve `fundamentals_unavailable`; nunca se sustituye por otro filing o ejercicio. Los
duplicados equivalentes conservan la resolución determinista vigente y una contradicción semántica
falla cerrado.

El precio y la base del título siguen usando `valuation_date` y `known_at`: elegir un ejercicio 2024
no elige un precio 2024. La respuesta
`corporate-valuation-selected-filing-snapshot-v1` reutiliza las once fórmulas, claves, estados,
inputs y evidencias existentes; sólo cambia el vintage publicado a
`corporate-valuation-selected-annual-filing-v1-decimal34` y
`corporate-valuation-selected-annual-filing-definition-v1`, con la limitación explícita de ejercicio
elegido, no último anual ni TTM. Cada `result_id` es UUIDv8 y liga `basis=selected_annual_filing`, el
selector completo, los inputs reales, el precio y la base ADR; `known_at` y `computed_at` no alteran
la identidad si la evidencia es idéntica. `available_at` sigue siendo el máximo de inputs y evidencia
ADR.

La modalidad seleccionada es estrictamente efímera y read-only. No persiste métricas, no crea
definiciones, no alimenta la historia materializada, el scheduler ni la cobertura global, y el
pipeline persistente rechaza ese DTO antes de cualquier escritura. `latest_annual` conserva sus
bytes, selector, IDs, versiones y persistencia sin cambios. ETFS y cripto siguen `not_applicable`.

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

Sólo la modalidad `latest_annual` persiste. Las métricas evaluadas se guardan como `MetricResult` con
categoría `valuation`. El `result_id` UUIDv5 legacy incorpora activo, clave, `known_at`, fecha de
valoración, base anual, período, precio, versión de unidad, IDs de inputs y algoritmo. En workspace
v2 la identidad es UUIDv8, excluye el corte y liga la versión, el factor y el digest de la revisión
ADR cuando existe. La misma evidencia y semántica reutiliza identidad; una revisión o input distinto
crea historia append-only. Cada métrica referencia solo sus inputs transitivos exactos. Los estados
no evaluables no se persisten como cero: permanecen en la proyección y en los contadores operativos.
La consulta `selected_annual_filing` reutiliza las fórmulas pero permanece efímera: sus IDs UUIDv8
incluyen `basis` y el selector completo y no se escriben en el workspace.

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

## Adopción operacional 2026-10-29

`FUNDAMENTALS-COVERAGE-OPERATIONS`/#373 adoptó la capacidad ya integrada sobre la base
`origin/main@a5b8581c71faaeb98665cb0ae9bd835df1cf5664` (release local activa; rollback
`4a9ccb4c070df622d15b14d150de69d5ad352f5c`) y el workspace UUID
`70c64668-1738-4dfe-ba1f-7e72026bb7fe`. El refresh anual real se ejecutó sobre AAPL, AMD, Barrick
(`equity:us:b`), TSM y BVN con `requested_known_at: null`; los cortes efectivos quedaron entre
`2026-10-29T19:45:58Z` y `2026-10-29T19:46:37Z`. La lectura se consolidó con
`known_at=2026-10-29T19:47:00Z` (K1) y se contrastó con `known_at=2026-10-27T00:00:00Z` (K0).

### Matriz 5×11

`not_evaluable` conserva el motivo y nunca se representa como cero. El total es 55 celdas:
AAPL 11/11; AMD y B 6/11; TSM y BVN 1/11.

| Métrica | AAPL | AMD | B | TSM | BVN |
| --- | --- | --- | --- | --- | --- |
| `earnings_yield_latest_annual` | `evaluated` | `evaluated` | `evaluated` | `not_evaluable` (missing_input) | `not_evaluable` (missing_input) |
| `enterprise_value` | `evaluated` | `not_evaluable` (missing_input) | `not_evaluable` (missing_input) | `not_evaluable` (missing_input) | `not_evaluable` (missing_input) |
| `enterprise_value_to_ebit_latest_annual` | `evaluated` | `not_evaluable` (missing_input) | `not_evaluable` (missing_input) | `not_evaluable` (missing_input) | `not_evaluable` (missing_input) |
| `enterprise_value_to_ebitda_latest_annual` | `evaluated` | `not_evaluable` (ebitda_unavailable) | `not_evaluable` (ebitda_unavailable) | `not_evaluable` (ebitda_unavailable) | `not_evaluable` (ebitda_unavailable) |
| `enterprise_value_to_sales_latest_annual` | `evaluated` | `not_evaluable` (missing_input) | `not_evaluable` (missing_input) | `not_evaluable` (missing_input) | `not_evaluable` (missing_input) |
| `financial_debt` | `evaluated` | `not_evaluable` (missing_input) | `not_evaluable` (missing_input) | `not_evaluable` (missing_input) | `not_evaluable` (missing_input) |
| `free_cash_flow_yield_latest_annual` | `evaluated` | `evaluated` | `evaluated` | `not_evaluable` (missing_input) | `not_evaluable` (missing_input) |
| `market_cap` | `evaluated` | `evaluated` | `evaluated` | `evaluated` | `evaluated` |
| `price_to_book` | `evaluated` | `evaluated` | `evaluated` | `not_evaluable` (missing_input) | `not_evaluable` (missing_input) |
| `price_to_earnings_latest_annual` | `evaluated` | `evaluated` | `evaluated` | `not_evaluable` (missing_input) | `not_evaluable` (missing_input) |
| `price_to_sales_latest_annual` | `evaluated` | `evaluated` | `evaluated` | `not_evaluable` (missing_input) | `not_evaluable` (missing_input) |

TSM y BVN sólo evalúan `market_cap` porque el corte no ofrece los inputs monetarios comparables que
exigen las demás fórmulas; la base ADR y el precio sí están disponibles. No se completa la ausencia
con otro ejercicio, otro emisor, FX, deuda sintética ni D&A estimada. El precio de B tenía 9 días de
antigüedad al corte y se publica con esa limitación.

### Procedencia de acciones y base del título

Los valores son `Decimal` serializados como texto. Las observaciones y raws conservan su identidad
exacta y no se reescribieron.

| Emisor | Tag y taxonomía | Accession | Observación / raw | Valor | Base aplicada |
| --- | --- | --- | --- | --- | --- |
| AAPL | `us-gaap:CommonStockSharesOutstanding` | `0000320193-25-000079` | `5e49533a-5421-5edf-b951-7f71b675ca78` / `ddf69926-344d-577c-aea7-c75018dc11f0` | `14773260000` | `reported_common_share`, factor `1` |
| AMD | `us-gaap:CommonStockSharesOutstanding` | `0000002488-26-000018` | `9beea886-bce7-55c9-8cd6-acc71452a4d6` / `caa19cbd-ef8c-590b-a2f0-f7c9e4121c07` | `1630000000` | `reported_common_share`, factor `1` |
| B | `dei:EntityCommonStockSharesOutstanding` | `0001193125-26-079253` | `f4c77d9f-7172-56a9-9fa4-e74d82251292` / `7f35f24d-a379-5740-b813-60a3858ea9b3` | `1675360395` | `reported_common_share`, factor `1` |
| TSM | `dei:EntityCommonStockSharesOutstanding` | `0001628280-26-025362` | `91e71ffc-f4c5-5efa-94e3-eedc649320e9` / `678cbbf6-4500-5467-89a2-0b244a005279` | `25932524521` | `depositary_receipt`, factor `0.2`, revisión `1228824…`, `valid_from=2026-04-17` |
| BVN | `dei:EntityCommonStockSharesOutstanding` | `0001104659-26-053101` | `875b74d2-73e6-51e6-983c-b2383cd09ca4` / `4bdaae69-5784-574e-88a9-652dadbb7f94` | `253715190` | `depositary_receipt`, factor `1`, revisión `b8a0066…`, `valid_from=2026-05-01` |

B, TSM y BVN demuestran DEI real persistida; AAPL y AMD usan el concepto nativo y no necesitaron la
alternancia DEI. Las revisiones ADR conservan su SHA-256 de fuente oficial (`9b4a4e6c…805a141` para
TSM y `ec2067c4…3e8dd` para BVN) y la valoración selecciona la revisión point-in-time documentada.

### K0, historia y reapertura

- K0 devolvió la misma cobertura por emisor: AAPL `evaluated` 11/11; AMD y B `partial` 6/11; TSM y
  BVN `partial` 1/11. Usó precios con `valuation_as_of` del 23-oct (AAPL/AMD) y 20-oct (B/TSM/BVN), y
  `available_at <= K0`; ninguna adquisición de K1 se transportó a K0. Los `result_id` difieren donde
  difiere el precio.
- La historia materializada devolvió puntos nuevos con `valuation_date=2026-10-29`: AAPL 100/220
  (truncada por límite), AMD 54, B 6, TSM 1 y BVN 1. No hay backfill ni puntos sintéticos.
- La repetición de las lecturas de investigación y valoración conservó IDs, valores y orden; el
  archivo DuckDB no cambió de tamaño ni de `mtime` y no apareció WAL. La reapertura fría/caliente se
  registra en [`v2_operational_readiness.md`](v2_operational_readiness.md).
