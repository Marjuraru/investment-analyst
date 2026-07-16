# Investment Analyst

## Propósito

Herramienta personal para recolectar, procesar, relacionar, visualizar y explicar información útil para el análisis de inversiones. Está diseñada para apoyar el criterio de un analista humano.

## Alcance

- Análisis de acciones, ETF y criptomonedas.
- Diagnósticos de mercado y fundamentales independientes, con presentación consolidada sin agregar
  sus resultados.
- Métricas y resultados transparentes, reproducibles y auditables.
- Sin compra, venta ni ejecución de órdenes.
- Sin trading automático ni conexión operativa con brokers.
- Sin recomendaciones directas de inversión.

## Principios

- **Transparencia:** cada dato conserva su fuente, timestamp, periodo y fecha de disponibilidad.
- **Trazabilidad:** cada métrica registra su fórmula, parámetros, unidades, entradas y versión.
- **Separación:** mercado y fundamentales conservan scores, veredictos, confianza, calidad y
  evidencia independientes, incluso cuando se presentan juntos.
- **Independencia de proveedores:** los conectores de datos podrán sustituirse sin modificar el motor analítico.
- **Determinismo:** el MVP no utiliza un LLM activo ni consume tokens durante su funcionamiento.
- **Escalabilidad:** la estructura está preparada para crecer sin añadir complejidad innecesaria al inicio.

## Estado actual

El proyecto ya dispone de un flujo básico funcional por CLI, centrado en Apple y ejecutado de forma
local:

- workspace persistente con DuckDB, RawRecords, observaciones normalizadas, métricas, diagnósticos y
  exportación Parquet;
- catálogo central de activos y resolución independiente de proveedores;
- importación de barras diarias de AAPL mediante Alpaca Market Data IEX, con la limitación explícita
  de que IEX no equivale a cobertura consolidada SIP;
- obtención oficial de fundamentales de Apple mediante SEC EDGAR;
- métricas y diagnósticos deterministas, point-in-time y trazables;
- bootstrap incremental e idempotente del workspace permanente;
- consulta diaria de diagnósticos de mercado y fundamentales, presentados por separado en JSON
  compacto, JSON versionado o texto legible;
- fachada de aplicación tipada para que CLI, automatización e interfaz reutilicen las mismas
  operaciones sin duplicar composición;
- entorno reproducible mediante un lock versionado, pruebas unitarias e integraciones locales,
  cobertura de líneas y ramas, auditoría de dependencias y validación continua con GitHub Actions.

El MVP actual no incluye interfaz gráfica, alertas, programación diaria automática, ejecución de
órdenes ni recomendaciones de inversión. Nuevos activos, indicadores o fuentes requieren fases de
diseño y validación independientes.

## Flujos principales

- [Bootstrap del workspace de Apple](docs/aapl_workspace_bootstrap.md): crear o actualizar el
  workspace real.
- [Consulta diagnóstica consolidada](docs/consolidated_diagnostic_query.md): consultar el reporte
  diario point-in-time.
- [Fachada de aplicación](docs/application_facade.md): invocar bootstrap y consulta mediante una
  API programática estable.
- [Integración continua](docs/continuous_integration.md): validar automáticamente cada pull request.
- [Gestión de dependencias](docs/dependency_management.md): reproducir y actualizar el entorno
  validado.

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
