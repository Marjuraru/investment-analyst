# Investment Analyst

## Propósito

Herramienta personal para recolectar, procesar, relacionar, visualizar y explicar información útil para el análisis de inversiones. Está diseñada para apoyar el criterio de un analista humano.

## Alcance

- Análisis de acciones, ETF y criptomonedas.
- Diagnósticos de mercado, fundamentales y unificados.
- Métricas y resultados transparentes, reproducibles y auditables.
- Sin compra, venta ni ejecución de órdenes.
- Sin trading automático ni conexión operativa con brokers.
- Sin recomendaciones directas de inversión.

## Principios

- **Transparencia:** cada dato conserva su fuente, timestamp, periodo y fecha de disponibilidad.
- **Trazabilidad:** cada métrica registra su fórmula, parámetros, unidades, entradas y versión.
- **Separación:** los análisis de mercado, fundamentales y unificados pueden consultarse independientemente.
- **Independencia de proveedores:** los conectores de datos podrán sustituirse sin modificar el motor analítico.
- **Determinismo:** el MVP no utiliza un LLM activo ni consume tokens durante su funcionamiento.
- **Escalabilidad:** la estructura está preparada para crecer sin añadir complejidad innecesaria al inicio.

## Estado actual

El proyecto contiene únicamente su estructura base. Todavía no incluye:

- Conexiones con APIs.
- Indicadores financieros.
- Modelos de datos.
- Diagnósticos.
- Bases de datos.
- Alertas.
- Interfaz gráfica.

## Entorno mínimo

- Windows con WSL 2.
- Ubuntu 24.04.
- Python 3.12.
- Entorno virtual `.venv`.
- Visual Studio Code.
- Git.

Consulta `docs/development.md` para los comandos de desarrollo.

## Advertencia

Esta herramienta no ejecuta operaciones y no constituye asesoramiento financiero.
