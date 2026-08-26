# Investment Analyst

## Propósito

Herramienta personal para recolectar, procesar, relacionar, visualizar y explicar información útil para el análisis de inversiones. Está diseñada para apoyar el criterio de un analista humano.

## Alcance

- Análisis de acciones, ETF y criptomonedas.
- Diagnósticos de mercado y fundamentales independientes, con presentación consolidada sin agregar
  sus resultados.
- Métricas y resultados transparentes, reproducibles y auditables.
- **Estado actual:** sin compra, venta ni ejecución de órdenes.
- **Estado actual:** sin trading automático ni conexión operativa con brokers.

La interfaz incluye una comparación diaria de 2–5 activos locales con la misma moneda de cotización. Es una lectura descriptiva de una muestra común PIT, no una clasificación ni recomendación. Véase [la documentación de comparación](docs/market_comparison.md).
- **Estado actual:** sin recomendaciones directas de inversión; los diagnósticos y candidatos son
  descriptivos y no sustituyen el criterio humano.

## Horizonte por capas

La versión viva no implementa señales predictivas, recomendaciones ni integración con brokers. La
evolución posible mantiene capas y gates separados:

`evidencia PIT → análisis → detección de oportunidades → señales/predicción validada → recomendación explícita y trazable → decisión humana/política → broker y ejecución controlada futura → posible automatización acotada posterior`

Una señal o predicción planificada requiere evidencia PIT, objetivo y label versionados, baselines,
validación temporal fuera de muestra, explicación, shadow mode y rollback. Una recomendación futura
sería un artefacto separado, no personalizado, versionado y trazable; una integración o ejecución
futura requeriría además contratos propios, autorización explícita, controles de riesgo y decisión
humana o de política. Ninguna de estas capas está implementada, autorizada ni calendarizada.

## Principios

- **Transparencia:** cada dato conserva su fuente, timestamp, periodo y fecha de disponibilidad.
- **Trazabilidad:** cada métrica registra su fórmula, parámetros, unidades, entradas y versión.
- **Separación:** mercado y fundamentales conservan scores, veredictos, confianza, calidad y
  evidencia independientes, incluso cuando se presentan juntos.
- **Independencia de proveedores:** los conectores de datos podrán sustituirse sin modificar el motor analítico.
- **Determinismo:** el MVP no utiliza un LLM activo ni consume tokens durante su funcionamiento.
- **Escalabilidad:** la estructura está preparada para crecer sin añadir complejidad innecesaria al inicio.
- **Restricciones permanentes:** transparencia, auditabilidad, reproducibilidad, `available_at`,
  `Decimal`, identidades deterministas, historia append-only y separación entre evidencia,
  analytics, señal, recomendación, decisión y ejecución.

## Estado actual

El proyecto ya dispone de un flujo básico funcional local. Apple conserva el bootstrap y la
experiencia web completa; la actualización fundamental SEC también puede ejecutarse por CLI para
emisores corporativos declarados en el catálogo:

AAPL es el activo fundador y conserva contratos históricos por compatibilidad; no es el centro
conceptual ni el modelo especial de la herramienta. La dirección vigente es que acciones y ETF
usen casos de uso genéricos derivados del catálogo, mientras cripto conserva su familia analítica
propia. No se añadirán nuevas rutas o modelos específicos de Apple.

El reporte corporativo seleccionado usa `/api/listed-company-report` con `asset_id` y `known_at`
obligatorios. Sus secciones de mercado y fundamentales son independientes, point-in-time y
read-only; una ausencia se informa como `not_found`, nunca como cero.

- workspace persistente con DuckDB, RawRecords, observaciones normalizadas, métricas, diagnósticos y
  exportación Parquet;
- catálogo central de activos y resolución independiente de proveedores;
- evidencia pública de derivados Deribit para BTC y ETH: funding horario, DVOL diario y snapshots
  prospectivos de perpetuos con receipts, revisiones append-only, métricas Decimal34 y diagnóstico
  multidimensional sin score; la interfaz local expone esa evidencia bajo demanda para los activos
  elegibles del catálogo mediante un corte point-in-time y sin mezclarla con Coinbase spot; el
  backfill sólo es visible desde su primera recepción local;
- importación de barras diarias de AAPL mediante Alpaca Market Data IEX, con la limitación explícita
  de que IEX no equivale a cobertura consolidada SIP;
- base intradía separada para BTC-USD mediante velas públicas de un minuto de Coinbase Exchange,
  con ingestión append-only e idempotente, reconstrucción point-in-time y agregaciones locales
  deterministas de 1/5/15/30/45 minutos y 1/2/4/5 horas; la interfaz permite consultarlas en una
  ventana acotada de 24 horas sin sustituir la ruta diaria;
- catálogo separado de seis cotizaciones BVL —`CVERDEC1`, `BVN`, `SCCO`, `VOLCABC1`, `MINSURI1`
  y `POMALCC1`— y conector HTTPS del registro SMV: persiste respuestas oficiales ODbL append-only,
  reconstruye el universo point-in-time y distingue el código abreviado reportado por SMV del ISIN
  completo corroborado; el boletín diario BVL continúa como inspección tipada sin persistencia
  mientras sus condiciones de automatización permanecen en revisión;
- conector macro FRED/ALFRED point-in-time: conserva respuestas oficiales append-only sin
  `asset_id`, protege la API key, enumera vintages de forma paginada y automatiza lotes reanudables
  para seis series mensuales o trimestrales de crecimiento, inflación, empleo, tasas, crédito y
  liquidez; las series diarias de curva, dólar y petróleo quedan catalogadas pero diferidas hasta
  disponer de almacenamiento columnar;
- obtención oficial de fundamentales mediante SEC EDGAR; Apple conserva el flujo completo histórico,
  diez emisores US-GAAP —AMD, Intel, Strategy, Micron, Palantir, CDE, HYMC, MUX, NEM y SCCO— y tres
  emisores IFRS anuales —Barrick, BVN ADR y TSM— reutilizan refresh, consultas, cachés y presentación
  web por `asset_id`, validados contra filings reales sin mezclar mercado, taxonomías ni emisores;
- base de investigación fundamental con 31 hechos SEC adicionales y 40 métricas descriptivas
  versionadas, calculadas point-in-time con `Decimal`, evidencia exacta por input y estadísticas
  históricas separadas para cambios, media, rango y CAGR válido; cada métrica aparece una sola vez,
  agrupada por crecimiento y datos por acción, rentabilidad, retornos y eficiencia, calidad del
  beneficio, liquidez y balance, deuda y solvencia, caja y reinversión o asignación de capital, sin
  producir un score o recomendación;
- métricas y diagnósticos deterministas, point-in-time y trazables;
- estadísticas de mercado diarias con retorno, SMA, volatilidad, volumen relativo, Bollinger, EMA,
  RSI Wilder, MACD y ATR Decimal-exact con linaje point-in-time auditable;
- bootstrap incremental e idempotente del workspace permanente;
- consulta diaria de diagnósticos de mercado y fundamentales, presentados por separado en JSON
  compacto, JSON versionado o texto legible;
- fachada de aplicación tipada para que CLI, automatización e interfaz reutilicen las mismas
  operaciones sin duplicar composición;
- ejecución operativa única con lock por workspace, estado atómico versionado, errores acotados y
  health check de solo lectura;
- scheduler multi-activo derivado del catálogo: mantiene trabajos separados por activo, proveedor,
  dominio, frecuencia y zona horaria para mercado diario Alpaca, fundamentales SEC, Coinbase diario
  e intradía, registro SMV y macro FRED/ALFRED cuando existe `FRED_API_KEY`; conserva cada intento,
  muestra frescura y cobertura, reintenta con backoff acotado solo fallos transitorios clasificados
  por tipo o estado HTTP, recupera interrupciones y no revierte el progreso exitoso de otros
  trabajos;
- preferencias versionadas por workspace para watchlist, favoritos y actualización programada:
  parten de la configuración CLI sin escribir al arrancar, usan control optimista, sobreviven al
  reinicio y reconcilian los mismos jobs en ejecución sin llamadas adicionales a proveedores;
- primer monitor silencioso de alertas operativas: evalúa cada intento mediante reglas trivaluadas,
  persiste resultados e identidades deterministas, detecta cobertura incompleta, deduplica eventos
  y ofrece una bandeja local con transiciones auditadas de vista, descartada o resuelta, sin IA,
  notificaciones externas, gráficos ni consultas adicionales a proveedores;
- monitor de screening analítico determinista: contratos estrictos y un motor puro evalúan
  instantáneas point-in-time de mercado o fundamentales mediante condiciones `met`, `not_met` o
  `not_evaluable`, validan fuente, activo, período, algoritmo, unidad y calidad, y producen una
  identidad reproducible sin mezclar dominios ni calcular un score. El scheduler lo invoca solo
  después de evidencia nueva y completa; resultados, recibos y candidatos se guardan en
  `state/analytical_screening_state_v1.json`. La bandeja analítica separada aplica confirmaciones,
  histéresis, cooldown, deduplicación y transiciones auditadas, incluso tras reiniciar. Un registro
  local adicional versiona cambios de estado, umbrales, histéresis, confirmaciones y cooldown con
  locking optimista; la interfaz permite restaurar valores iniciales sin borrar el historial y
  ejecutar un replay point-in-time de solo lectura por regla y activo. Cada candidato `new` crea
  además una recepción local durable, deduplicada por candidato y confirmable de forma independiente
  desde la interfaz; no entrega datos a navegador, sistema ni servicios externos;
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
  incluye relojes locales de Lima y Wall Street con la ventana regular NYSE explícita y estado
  operativo local actualizado automáticamente cada 30 segundos con pausa y backoff,
  exportaciones exactas CSV/JSON generadas en el navegador, tema oscuro o claro, respuestas
  comprimidas, cachés de lectura acotadas, scheduler de watchlist persistente y unidad de usuario
  `systemd` generada de forma segura;
- entorno reproducible mediante un lock versionado, pruebas unitarias e integraciones locales,
  cobertura de líneas y ramas, auditoría de dependencias y validación continua con GitHub Actions.
- sonda operacional read-only, versionada y fail-closed: une intentos terminales con las cuatro
  reglas operativas y un receipt analítico, verifica cohortes multidiarias, retries, backoff,
  presupuesto y ausencia de trabajo activo, y devuelve `PASS`/`NOT_READY` con fingerprint estable
  sin ejecutar providers ni modificar el workspace.

El MVP actual entrega alertas operativas persistentes y una primera bandeja analítica local en modo
silencioso. Las plantillas iniciales cubren actividad relativa de mercado y una condición conjunta
trimestral de balance, margen y crecimiento; ya pueden versionarse desde la interfaz y validarse
mediante replay histórico de frecuencia, cobertura y ruido. Todavía faltan reglas adicionales,
observación silenciosa durante varios ciclos y canales de notificación externos. Tampoco existen
autenticación o exposición remota, inicio automático desde Windows Task Scheduler, ejecución de
órdenes ni recomendaciones de
inversión. La programación continua actualiza la watchlist visible mediante los conectores ya
configurados; puede limitarse a activos explícitos. Activos fuera del catálogo, indicadores o
fuentes nuevas requieren fases de diseño y validación independientes.

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
- [Runtime por capacidades](docs/capability_driven_runtime.md): consultar dispatch tipado, overview
  no bloqueante, cola manual durable, telemetría y backup/restore verificados.
- [Readiness operacional](docs/operational_readiness.md): ejecutar la sonda zero-write, interpretar
  reason codes y realizar por separado el rehearsal HUMAN exact-SHA de backup/restore.
- [Preferencias de activos](docs/asset_preferences.md): persistir watchlist, favoritos y selección
  automática con precedencia CLI documentada y reconciliación sin reinicio.
- [Base intradía de Coinbase](docs/coinbase_intraday.md): importar ventanas acotadas de un minuto y
  consultar agregaciones trazables por CLI o interfaz sin mezclar la historia diaria.
- [Derivados cripto Deribit](docs/crypto_derivatives.md): refrescar funding, DVOL y snapshots de
  perpetuos para BTC/ETH, consultar replay PIT y entender receipts, scheduler y limitaciones.
- [Refresh fundamental SEC por emisor](docs/sec_issuer_refresh.md): actualizar snapshots,
  observaciones, métricas y diagnóstico de una empresa configurada sin acoplarla a su mercado.
- [Corpus documental SEC](docs/sec_document_corpus.md): importar y reconstruir documentos primarios
  oficiales sin convertirlos en métricas, señales o recomendaciones.
- [Valoración corporativa point-in-time](docs/corporate_valuation_point_in_time.md): alinear precio
  y último ejercicio anual, persistir múltiplos/yields descriptivos y explicar ausencias sin score,
  recomendación, TTM ni FX.
- [Estrategia BVL/SMV](docs/bvl_market_strategy.md): plan gratuito por fases para identidad,
  cotización diferida, fundamentales y futura sustitución de proveedores.
- [Registro SMV y universo BVL](docs/smv_bvl_registry.md): actualizar y consultar las seis
  identidades peruanas con evidencia append-only y point-in-time.
- [Conector FRED/ALFRED point-in-time](docs/fred_alfred_point_in_time.md): importar vintages
  explícitos y consultar revisiones históricas sin mezclar macro con activos.
- [Hoja de ruta del producto](docs/product_roadmap.md): orden de integración, mercados,
  fundamentales, macro, noticias, Cazatiburones, interfaz, IA y operación 24/7.
- [Plan de cierre básico](docs/basic_functional_release_plan.md): auditoría arquitectónica,
  prioridades, presupuestos y ruta crítica hasta notificaciones e IA cualitativa opcional.
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
