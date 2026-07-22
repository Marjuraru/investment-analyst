# Arquitectura

## Separación por capas
- `providers`: obtención de datos crudos por dominio (market, fundamentals,
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

## Diagnósticos separados y presentación consolidada
Los diagnósticos de mercado y fundamental se calculan de forma independiente y
pueden consultarse por separado. La consulta consolidada los presenta juntos sin
mezclar su significado, sus fuentes ni producir una puntuación agregada.

## Escalabilidad futura
La modularidad y el desacoplamiento permiten incorporar más adelante Docker,
PostgreSQL, un VPS y una versión de escritorio (PWA o Tauri) sin reescribir el
núcleo. Nada de esa infraestructura se incluye en el MVP.

Los datasets centenarios destinados a investigación y modelado seguirán la
[estrategia de datos históricos](historical_research_data.md): workspace separado, licencias
registradas, vintages point-in-time y evaluación temporal sin contaminar el pipeline operativo.

## Prohibición actual de ejecución de órdenes
El sistema no ejecuta operaciones ni se integra con brokers. Solo produce
información y diagnósticos para decisión humana.
