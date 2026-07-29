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

El proyecto ya dispone de un flujo básico funcional local. Apple conserva el bootstrap y la
experiencia web completa; la actualización fundamental SEC también puede ejecutarse por CLI para
emisores corporativos declarados en el catálogo:

- workspace persistente con DuckDB, RawRecords, observaciones normalizadas, métricas, diagnósticos y
  exportación Parquet;
- catálogo central de activos y resolución independiente de proveedores;
- importación de barras diarias de AAPL mediante Alpaca Market Data IEX, con la limitación explícita
  de que IEX no equivale a cobertura consolidada SIP;
- base intradía separada para BTC-USD mediante velas públicas de un minuto de Coinbase Exchange,
  con ingestión append-only e idempotente, reconstrucción point-in-time y agregaciones locales
  deterministas de 1/5/15/30/45 minutos y 1/2/4/5 horas; la interfaz permite consultarlas en una
  ventana acotada de 24 horas sin sustituir la ruta diaria;
- sondeo acotado de las rutas oficiales SMV/BVL y lector tipado de solo lectura del boletín diario
  BVL para `CVERDEC1`, `BVN`, `SCCO`, `VOLCABC1`, `MINSURI1` y `POMALCC1`; valida el documento
  completo, conserva moneda y decimales originales y emite metadatos auditables sin persistencia ni
  activación prematura de identidades peruanas;
- obtención oficial de fundamentales mediante SEC EDGAR; Apple conserva el flujo completo histórico
  y diez emisores genéricos —AMD, Intel, Strategy, Micron, Palantir, CDE, HYMC, MUX, NEM y SCCO—
  reutilizan refresh, consultas, cachés y presentación web por `asset_id`, validados contra sus
  filings reales sin mezclar mercado ni emisores;
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
  seleccionar desde un catálogo central Apple, Bitcoin y una lista inicial de acciones y ETF
  estadounidenses disponible mediante Alpaca IEX gratuito. Incluye AMD, Barrick (`B`), BVN, CDE,
  HYMC, INTC, MSTR, MU, MUX, NEM, PLTR, SCCO, TSM, GBTC, GLD e IBIT sin codificar una ruta distinta
  por símbolo. BTC-USD usa el histórico diario público de Coinbase Exchange y un modo exclusivamente
  de mercado, sin inventar fundamentales. Cada activo de mercado puede actualizarse de forma
  incremental y auditable desde la propia página. La interfaz permite explorar OHLC,
  actividad, retorno, volatilidad, volumen relativo y tres SMA configurables
  por ventana y color —5, 20 y 50 de forma predeterminada—, con escala de precio lineal o logarítmica,
  gráfico de línea o velas, zoom exclusivo con la rueda del mouse, desplazamiento horizontal por
  arrastre e intervalos reales de un día, una semana o
  un mes, además de intervalos intradía de BTC entre 1 minuto y 5 horas sobre las últimas 24 horas:
  la carga inicial y diaria se limita al último año, la semanal amplía a cinco años y la
  mensual permite consultar todo el histórico local point-in-time; cada agregado conserva sus días
  e identidades fuente y el último cierre permanece
  separado; incorpora evolución trimestral o anual de cinco hechos SEC, una clasificación
  empresarial visible que declara cuando la evidencia aún no es suficiente, ficha fundamental y
  una matriz compacta de 40 métricas derivadas con fórmulas e inputs auditables;
  incluye relojes locales de Lima y Wall Street con la ventana regular NYSE explícita,
  exportaciones exactas CSV/JSON generadas en el navegador, tema oscuro o claro, respuestas
  comprimidas, cachés de lectura acotadas, scheduler diario persistente y unidad de usuario
  `systemd` generada de forma segura;
- entorno reproducible mediante un lock versionado, pruebas unitarias e integraciones locales,
  cobertura de líneas y ramas, auditoría de dependencias y validación continua con GitHub Actions.

El MVP actual no incluye alertas, autenticación o exposición remota, inicio automático desde
Windows Task Scheduler, ejecución de órdenes ni recomendaciones de inversión. La programación
continua actualiza Apple; BTC-USD y los demás activos de mercado se actualizan manualmente desde la
misma interfaz. Activos fuera del catálogo, indicadores o fuentes nuevas requieren fases de diseño
y validación independientes.

La ampliación de [investigación fundamental](docs/fundamental_research_foundation.md), la
[estrategia de datos históricos](docs/historical_research_data.md), la
[estrategia de historia larga y noticias](docs/research_information_strategy.md), las
[referencias de interfaz profesional](docs/professional_analysis_interface.md), la
[hoja de ruta integral](docs/product_roadmap.md), el diseño de
[screening y alertas automáticas](docs/automated_screening_alerts.md) y el futuro dominio
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
- [Base intradía de Coinbase](docs/coinbase_intraday.md): importar ventanas acotadas de un minuto y
  consultar agregaciones trazables por CLI o interfaz sin mezclar la historia diaria.
- [Refresh fundamental SEC por emisor](docs/sec_issuer_refresh.md): actualizar snapshots,
  observaciones, métricas y diagnóstico de una empresa configurada sin acoplarla a su mercado.
- [Estrategia BVL/SMV](docs/bvl_market_strategy.md): plan gratuito por fases para identidad,
  cotización diferida, fundamentales y futura sustitución de proveedores.
- [Hoja de ruta del producto](docs/product_roadmap.md): orden de integración, mercados,
  fundamentales, macro, noticias, Cazatiburones, interfaz, IA y operación 24/7.
- [Screening y alertas](docs/automated_screening_alerts.md): evaluación determinista y de bajo
  consumo de candidatos para revisión, notificaciones y enriquecimiento cualitativo opcional.
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
