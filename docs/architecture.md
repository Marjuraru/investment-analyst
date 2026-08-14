# Arquitectura

## Separación por capas
- `providers`: obtención de datos crudos por dominio (registry, market, fundamentals,
  crypto, macro).
- `core`: modelos, configuración e interfaces compartidas.
- `analytics`: cálculos deterministas por dominio, incluido el futuro módulo
  [`cazatiburones`](cazatiburones.md).
- `diagnostics`: composición de diagnósticos de mercado, fundamental y unificado.
- `validation`, `alerts`, `audit`, `knowledge`, `storage`: soporte transversal.
- `api` y `frontend`: capas de exposición, aisladas del núcleo.

## Independencia de proveedores
El acceso a datos se define mediante interfaces en `core/interfaces`. Los
proveedores concretos son intercambiables; ningún módulo de análisis depende
directamente de un proveedor específico.

## Trazabilidad
Cada dato conserva fuente, timestamp, periodo y fecha real de disponibilidad.
Cada métrica documenta fórmula, parámetros, unidades, entradas y versión. Cada
diagnóstico debe poder reconstruirse y auditarse; no existe lógica financiera
oculta.

La investigación fundamental derivada se ejecuta en una capa de solo lectura sobre observaciones
SEC normalizadas. Selecciona revisiones por `available_at` y `known_at`, calcula con `Decimal` a 34
dígitos y devuelve los UUID de todos los inputs. En esta etapa no persiste resultados ni altera las
cinco variables del contrato fundamental original.

La ingestión corporativa SEC se compone por `asset_id` en un refresh independiente de mercado:
snapshots, normalización, cinco métricas persistidas y diagnóstico fundamental. Cada etapa recibe la
misma configuración inmutable de emisor y usa una sola conexión writer. Los fallos tardíos conservan
el progreso append-only anterior; no existe un rollback global que elimine evidencia válida.

La vista fundamental unificada organiza el resultado histórico exacto por tipo de análisis:
crecimiento y datos por acción, rentabilidad, retornos y eficiencia, calidad del beneficio, liquidez
y balance, deuda y solvencia, caja y reinversión y asignación de capital.
Cada métrica pertenece a una sola sección, conserva su relevancia y disponibilidad y no se duplica
por autor o método de inversión. La clasificación empresarial expone sus categorías y requisitos,
pero no asigna una categoría sin una regla versionada y evidencia suficiente. Esta capa no recalcula
cifras, no introduce umbrales y no se combina con mercado ni con el futuro dominio macro.

El gráfico local agrega en memoria las barras diarias ya seleccionadas point-in-time. Su contrato
v5 conserva por compatibilidad los rangos históricos, añade intervalos explícitos diarios,
semanales y mensuales; la interfaz solicita siempre `max`. No crea una nueva frecuencia persistida
ni modifica identidades. También publica tres SMA configurables sin alterar las dos SMA canónicas del
diagnóstico. Todo intervalo agregado retiene las identidades de sus días fuente y reglas versionadas
para OHLCV, VWAP, calidad y SMA.

La política de cobertura amplia se aplica por dominio. El RawRecord conserva el payload completo y
la capa normalizada promueve todos los campos verificables del contrato activo: las barras Alpaca
diarias ya conservan OHLC, volumen, número de operaciones y VWAP. Incorporar quotes, trades,
corporate actions, opciones o cobertura SIP exige fuentes y contratos independientes; no se
inventan a partir de una barra ni se atribuyen al feed IEX.

## Dominio de derivados cripto

Deribit añade tres cadencias separadas —funding horario, DVOL diario y snapshots prospectivos— para
BTC y ETH. El provider valida y persiste raw/receipts; `analytics/crypto/derivatives_*` consume sólo
observaciones genéricas y no importa el conector. La revisión PIT se elige por `available_at`, las
métricas se calculan con Decimal34 y el diagnóstico read-only no usa el `DiagnosticResult` puntuado
ni se combina con spot. El contrato completo está en
[`crypto_derivatives.md`](crypto_derivatives.md).

## Diagnósticos separados y presentación consolidada
Los diagnósticos de mercado y fundamental se calculan de forma independiente y
pueden consultarse por separado. La consulta consolidada los presenta juntos sin
mezclar su significado, sus fuentes ni producir una puntuación agregada.

## Descentralización del activo fundador

AAPL fue el primer flujo vertical completo y por eso persisten nombres `Aapl*`, valores
predeterminados y contratos históricos en aplicación, operación e interfaz. Es deuda de
compatibilidad, no una decisión de dominio: Apple debe comportarse como otra empresa del catálogo.

La migración seguirá estas reglas:

1. no crear nuevos casos de uso, rutas, estados ni modelos específicos de AAPL;
2. introducir contratos genéricos por activo y dominio antes de añadir más capacidades;
3. mantener temporalmente los contratos y esquemas históricos como adaptadores del flujo genérico;
4. no renombrar archivos de estado, IDs o esquemas persistidos sin una migración versionada;
5. probar las mismas rutas con varios emisores US-GAAP, IFRS y proveedores de mercado;
6. permitir especialización por familia —empresa, ETF o cripto—, no por símbolo;
7. retirar una bifurcación Apple solo cuando replay, idempotencia, point-in-time y clientes
   compatibles estén cubiertos.

El criterio de salida es que seleccionar AAPL, AMD, CDE, BVN ADR o TSM reutilice la misma
orquestación empresarial según las capacidades declaradas. Las diferencias legítimas serán fuente,
taxonomía, frecuencia, moneda y cobertura, nunca el nombre del activo.

## Screening y alertas

El motor de [screening y alertas](automated_screening_alerts.md) consume resultados persistidos
después de un refresh exitoso y con evidencia nueva. Evalúa condiciones versionadas de forma
trivaluada, conserva evidencia exacta y ofrece un replay point-in-time de solo lectura; no recalcula
datos mediante el frontend ni produce una puntuación conjunta. Los eventos analíticos y los futuros
intentos de notificación tienen identidades separadas para poder reintentar un canal sin duplicar el
candidato.

## Estadísticas de mercado derivadas

`analytics/market/` conserva la separación entre barras normalizadas, cálculos de métricas y
diagnósticos. Las EMA diarias MKT-2 se siembran dentro de la consulta point-in-time y cada paso
posterior referencia la observación de cierre actual más el UUID del EMA previo en
`input_metric_result_ids`. La identidad UUID5 añade esas dependencias solo cuando existen, por lo
que las métricas históricas sin linaje derivado mantienen su preimagen e identidad. La verificación
de pipeline y backup exige que toda dependencia derivada exista y sea compatible con activo, fuente,
ventana, algoritmo y corte temporal.

## Dominio de valoración corporativa

`analytics/valuation/` compone barras y observaciones SEC persistidas mediante capacidades del
catálogo. Mantiene contratos, selección point-in-time, fórmulas e identidad separados de los
diagnósticos de mercado y fundamentales. Los refresh elegibles reutilizan la misma conexión writer
y persisten `MetricResult` de categoría `valuation`; la consulta HTTP reconstruye el snapshot en
modo read-only y nunca llama proveedores. ETF y cripto salen antes de seleccionar evidencia.

## Escalabilidad futura
La modularidad y el desacoplamiento permiten incorporar más adelante Docker,
PostgreSQL, un VPS y una versión de escritorio (PWA o Tauri) sin reescribir el
núcleo. Nada de esa infraestructura se incluye en el MVP.

Los datasets centenarios destinados a investigación y modelado seguirán la
[estrategia de datos históricos](historical_research_data.md): workspace separado, licencias
registradas, vintages point-in-time y evaluación temporal sin contaminar el pipeline operativo.
El primer [conector FRED/ALFRED](fred_alfred_point_in_time.md) persiste cada snapshot macro como
RawRecord sin `asset_id` y lo consulta por `available_at`; no reutiliza el contrato de barras ni
crea métricas o diagnósticos de activos.

El [registro SMV/BVL](smv_bvl_registry.md) aplica la misma separación a identidad: las respuestas
registrales completas son RawRecords con `asset_id=None`, mientras el catálogo versionado conserva
las identidades de cotización. La consulta las relaciona sin convertir campos registrales en
precios, fundamentales o señales.

## Estado actual y horizontes separados

El sistema actual no ejecuta operaciones ni se integra con brokers. Produce información,
diagnósticos y candidatos analíticos para decisión humana; no emite señales predictivas operativas,
recomendaciones de inversión ni asesoramiento personalizado.

La evolución posible mantiene capas explícitas: evidencia PIT → análisis → detección de
oportunidades → señales/predicción validada → recomendación explícita y trazable → decisión humana
o de política → broker y ejecución controlada futura. Una capa posterior sólo referencia los
artefactos versionados de la anterior y nunca reescribe evidencia histórica. Señales o predicciones
futuras exigen objetivo/label versionado, baselines, validación temporal fuera de muestra,
calibración cuando aplique, explicación, shadow mode y rollback. Una recomendación futura será
separada y no personalizada; cualquier ejecución futura quedará fuera del núcleo analítico, detrás
de contratos propios, autorización explícita y controles de riesgo.
