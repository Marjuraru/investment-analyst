# Arquitectura

## Separación por capas
- `providers`: obtención de datos crudos por dominio (market, fundamentals,
  crypto, macro).
- `core`: modelos, configuración e interfaces compartidas.
- `analytics`: cálculos deterministas por dominio y unificados, incluido
  `cazatiburones`.
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

## Diagnósticos separados y unificados
Los diagnósticos de mercado y fundamental se calculan de forma independiente y
pueden consultarse por separado. El diagnóstico unificado los combina sin
mezclar sus fuentes ni ocultar su origen.

## Escalabilidad futura
La modularidad y el desacoplamiento permiten incorporar más adelante Docker,
PostgreSQL, un VPS y una versión de escritorio (PWA o Tauri) sin reescribir el
núcleo. Nada de esa infraestructura se incluye en el MVP.

## Prohibición actual de ejecución de órdenes
El sistema no ejecuta operaciones ni se integra con brokers. Solo produce
información y diagnósticos para decisión humana.
