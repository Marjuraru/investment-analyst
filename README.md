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
- base de investigación fundamental con 31 hechos SEC adicionales y 40 métricas descriptivas
  versionadas, calculadas point-in-time con `Decimal`, evidencia exacta por input y estadísticas
  históricas separadas para cambios, media, rango y CAGR válido; cada métrica aparece una sola vez,
  agrupada por crecimiento y datos por acción, rentabilidad, retornos y eficiencia, calidad del
  beneficio, liquidez y balance, deuda y solvencia, caja y reinversión o asignación de capital, sin
  producir un score o recomendación;
- métricas y diagnósticos deterministas, point-in-time y trazables;
- bootstrap incremental e idempotente del workspace permanente;
- consulta diaria de diagnósticos de mercado y fundamentales, presentados por separado en JSON
  compacto, JSON versionado o texto legible;
- fachada de aplicación tipada para que CLI, automatización e interfaz reutilicen las mismas
  operaciones sin duplicar composición;
- ejecución operativa única con lock por workspace, estado atómico versionado, errores acotados y
  health check de solo lectura, preparada para un programador externo;
- interfaz web local compacta para ejecutar el flujo, consultar el reporte, revisar la evidencia y
  explorar OHLC, VWAP, actividad, retorno, volatilidad, volumen relativo y tres SMA configurables
  por ventana y color —5, 20 y 50 de forma predeterminada—, con escala de precio lineal o logarítmica,
  gráfico de línea o velas, zoom exclusivo con la rueda del mouse, desplazamiento horizontal por
  arrastre e intervalos reales de un día, una semana o
  un mes sobre todo el histórico local point-in-time; el modo automático usa resolución mensual
  para ese historial completo; cada agregado conserva sus días e identidades fuente y el último cierre permanece
  separado; incorpora evolución trimestral o anual de cinco hechos SEC, una clasificación
  empresarial visible que declara cuando la evidencia aún no es suficiente, ficha fundamental y
  una matriz compacta de 40 métricas derivadas con fórmulas e inputs auditables;
  incluye exportaciones exactas CSV/JSON generadas en el navegador, tema oscuro o claro, respuestas
  comprimidas, cachés de lectura acotadas, scheduler diario persistente y unidad de usuario
  `systemd` generada de forma segura;
- entorno reproducible mediante un lock versionado, pruebas unitarias e integraciones locales,
  cobertura de líneas y ramas, auditoría de dependencias y validación continua con GitHub Actions.

El MVP actual no incluye alertas, autenticación o exposición remota, inicio automático desde
Windows Task Scheduler, ejecución de órdenes ni recomendaciones de inversión. Nuevos activos,
indicadores o fuentes requieren fases de diseño y validación independientes.

La ampliación de [investigación fundamental](docs/fundamental_research_foundation.md), la
[estrategia de datos históricos](docs/historical_research_data.md), las
[referencias de interfaz profesional](docs/professional_analysis_interface.md) y el futuro dominio
[Cazatiburones](docs/cazatiburones.md) tienen contratos separados para no atribuir a los datos más
información de la que realmente contienen.

## Flujos principales

- [Bootstrap del workspace de Apple](docs/aapl_workspace_bootstrap.md): crear o actualizar el
  workspace real.
- [Consulta diagnóstica consolidada](docs/consolidated_diagnostic_query.md): consultar el reporte
  diario point-in-time.
- [Fachada de aplicación](docs/application_facade.md): invocar bootstrap y consulta mediante una
  API programática estable.
- [Runner operativo de Apple](docs/operational_runner.md): ejecutar un refresh bloqueado, consultar
  su estado y preparar la programación local.
- [Interfaz y operación continua](docs/local_interface.md): usar la herramienta en el navegador y
  mantener el servicio local mediante `systemd --user`.
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
