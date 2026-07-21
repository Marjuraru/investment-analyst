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

El gráfico local agrega en memoria las barras diarias ya seleccionadas point-in-time. Su contrato
v2 conserva puntos diarios hasta 2A, semanas ISO para 5A y meses UTC para Máx., sin crear una nueva
frecuencia persistida ni modificar identidades. Todo intervalo agregado retiene las identidades de
sus sesiones fuente y reglas versionadas para OHLCV, VWAP, calidad y SMA.

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
