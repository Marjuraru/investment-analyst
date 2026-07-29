# Hoja de ruta estratégica del producto

## Propósito y definición de producto completo

Esta hoja de ruta ordena las ampliaciones necesarias para convertir el MVP local actual en una
herramienta profesional completa de apoyo al análisis. No modifica los límites del producto:
mercado, fundamentales, macro, noticias y actividad de participantes conservan significado,
evidencia y diagnósticos independientes. La aplicación no ejecuta órdenes, no administra dinero y
no convierte sus resultados en una recomendación personalizada o puntuación agregada.

Una primera versión completa deberá:

- seleccionar, actualizar, consultar y analizar todos los activos configurados;
- aplicar contratos distintos a empresas, ETF y criptoactivos;
- incorporar mercados estadounidenses, BVL, fundamentales corporativos, macro, noticias y
  actividad declarada de participantes mediante fuentes identificadas;
- funcionar manualmente y en segundo plano con trabajos reanudables por activo y proveedor;
- conservar revisiones, moneda, período y semántica point-in-time;
- explicar fórmulas, condiciones, inputs, cobertura, frescura y limitaciones;
- ofrecer gráficos, comparación, búsqueda, alertas de candidatos y exportaciones desde una
  interfaz compacta en español;
- continuar funcionando sin un LLM, un proveedor pagado o una conexión permanente;
- poder instalarse, respaldarse, restaurarse y supervisarse en una laptop o equipo dedicado.

La IA cualitativa y la investigación predictiva son módulos opcionales posteriores. No son
dependencias del núcleo determinista.

## Estado de referencia

Checkpoint base auditado: 29 de julio de 2026, `main` en `e53f1c8`.

### Funcional

- persistencia local DuckDB/Parquet, identidades deterministas y evidencia append-only;
- mercado diario para el universo gratuito Alpaca IEX y BTC-USD/Coinbase;
- base intradía BTC-USD de un minuto y nueve agregaciones locales;
- fundamentales SEC completos para AAPL, diez emisores genéricos US-GAAP y tres emisores IFRS
  anuales (Barrick, BVN ADR y TSM);
- 40 métricas fundamentales, historia, comparación y clasificación explicable cuando la evidencia
  alcanza;
- gráficos diarios, semanales, mensuales e intradía, velas, escala logarítmica, zoom, arrastre y
  tres SMA configurables;
- interfaz local, exportaciones, cachés, compresión, scheduler Apple y servicio `systemd --user`;
- importación FRED/ALFRED por vintage explícito, evidencia macro append-only y reconstrucción local
  point-in-time con disponibilidad diaria conservadora;
- catálogo de seis cotizaciones BVL, cliente HTTPS SMV, evidencia registral append-only,
  reconstrucción point-in-time y refresh por lote reanudable;
- dependencias reproducibles, Ruff, Pytest, cobertura, auditoría y CI.

### Parcial

- BVL mercado: lector tipado del boletín diario, todavía sin autorización resuelta para
  persistencia automática, histórico o interfaz;
- operación multi-activo: refresh manual generalizado, pero programación persistente centrada en
  Apple;
- fundamentales BVL: requieren el futuro adaptador SMV y no reutilizan el perfil SEC del ADR;
- screening automático y bandejas locales implementados; catálogo ampliado y notificaciones
  externas pendientes.

### Diseñado, no implementado

- fundamentales SMV;
- scheduler multi-proveedor;
- indicadores técnicos adicionales, comparación y plantillas;
- fundamentales de red para cripto;
- catálogo macro y workspace de historia larga;
- corpus y búsqueda de noticias;
- dominio Cazatiburones;
- IA cualitativa opcional y laboratorio predictivo;
- autenticación o exposición remota segura.

## Alcance inicial de activos

El catálogo actual cubre mercado estadounidense para AAPL, AMD, Barrick (`B`), BVN, CDE, HYMC,
INTC, MSTR, MU, MUX, NEM, PLTR, SCCO, TSM, GBTC, GLD e IBIT, además de BTC-USD. La cobertura
fundamental no es igual para todos.

El catálogo BVL ya separa `CVERDEC1`, `BVN`, `SCCO`, `VOLCABC1`, `MINSURI1` y `POMALCC1` de los
instrumentos estadounidenses. “FCA” permanece sin mapear. `ABX` debe tratarse como alias histórico
de Barrick cuando se confirme su vigencia, no como instrumento duplicado. “TBC” debe aclararse
antes de añadirlo; no se asumirá que significa BTC.

## Consolidación de la base

Los PR #10, #11 y #12 se consolidaron mediante el PR #13. La línea principal ya contiene BTC
intradía, mercado y fundamentales multi-activo, el lector BVL y la planificación de alertas. La base
FRED/ALFRED fue trasladada de forma aislada sobre ese `main`, sin fusionar la pila histórica ni
reintroducir commits laterales.

El criterio permanente es que cada ampliación nazca del `main` vigente, pase CI y smoke local y no
dependa únicamente de una rama paralela.

## Fase 1 — Identidad y catálogo BVL/SMV

Completada localmente el 29 de julio de 2026:

1. Adaptadores tipados para empresas y valores inscritos mediante formularios HTTPS SMV.
2. ISIN completo validado por checksum y código SMV abreviado conservado por separado.
3. Seis cotizaciones BVL con nemónico, moneda, emisor e identidad de mercado independientes.
4. `BVN` y `SCCO` de Lima separados de sus instrumentos estadounidenses.
5. Respuestas completas append-only, deduplicación semántica y revisiones point-in-time.
6. Consulta local del universo con estados explícitos de cobertura y evidencia raw.
7. Refresh por lote, una conexión writer y conservación de progreso ante fallos tardíos.

La limitación residual es explícita: el valor BVL `BVN` no aparece en la consulta de valores del
emisor SMV, por lo que se verifica el emisor pero el listing exacto conserva evidencia BVL
independiente.

## Fase 2 — Mercado diario BVL

1. Persistir el boletín exacto como `RawRecord` solamente después de resolver su uso permitido.
2. Normalizar OHLC, precio anterior, propuestas, promedio, volumen, monto, operaciones y moneda.
3. Distinguir cero, ausencia, propuesta sin operación y sesión sin negociación.
4. Marcar cobertura diferida y no consolidada.
5. Añadir consulta point-in-time y gráfico mediante el servicio histórico común.
6. Integrar los activos BVL en el catálogo visible sin rutas específicas por emisor.
7. Añadir calendario oficial únicamente bajo un contrato versionado.
8. Evaluar boletines fechados para backfill solo cuando términos, estructura y cobertura sean
   compatibles.

Si la automatización histórica de BVL no queda autorizada, la alternativa inicial será importación
manual de documentos oficiales o captura diaria hacia adelante. El endpoint interno
`dataondemand` no será una dependencia de producción sin documentación o autorización.

## Fase 3 — Orquestación multi-activo

Base completada localmente el 29 de julio de 2026. El scheduler dejó de representar una ejecución
Apple y ahora ejecuta trabajos explícitos por activo, proveedor, dominio, frecuencia y zona
horaria.

Trabajos previstos:

- mercado diario Alpaca;
- fundamentales SEC;
- Coinbase diario e intradía;
- BVL diario;
- catálogo y fundamentales SMV;
- FRED/ALFRED;
- noticias y filings cuando existan sus conectores.

Capacidades requeridas:

- actualizar el activo seleccionado o toda una watchlist;
- locks de writer y estado por trabajo;
- planificación por cobertura, no por repetición ciega;
- rate limiting, reintentos acotados y backoff;
- reanudación mediante recibos verificables;
- frescura y estado por activo/fuente;
- recuperación tras reinicios;
- persistencia del progreso exitoso previo a un fallo;
- panel operativo y health check generalizados.

La entrega actual cubre Alpaca diario, SEC, Coinbase diario e intradía, registro SMV y seis series
FRED/ALFRED de baja frecuencia cuando existe la API key. Incluye selección explícita o watchlist
completa, estado, frescura y cobertura por job, un único writer, reintentos acotados, backoff,
recuperación tras reinicios y preservación de progreso previo. BVL diario continúa bloqueado por su
contrato de uso; noticias y filings se añadirán cuando sus conectores estén listos. Queda medir
presupuestos reales por proveedor y diseñar almacenamiento columnar para fuentes de alto volumen.

## Fase 4 — Fundamentales corporativos completos

### US-GAAP

Ampliar progresivamente:

- ROA, ROE y ROIC con saldos promedio;
- crecimiento de 3, 5 y 10 años;
- estabilidad de márgenes y flujos como estadísticas, no score;
- accruals, conversión a caja y dilución;
- vencimientos y estructura de deuda cuando exista evidencia comparable;
- market cap y enterprise value point-in-time;
- P/E, P/B, P/S, EV/ventas, EV/EBIT y EV/EBITDA cuando apliquen;
- FCF yield, earnings yield y PEG con denominadores y limitaciones explícitas;
- valoración frente a la propia historia;
- owner earnings como aproximación separada del FCF contable.

### IFRS

La base anual está implementada para TSM, Barrick y BVN ADR: declara `ifrs-full`, admite 20-F/40-F,
selecciona únicamente conceptos comparables en USD publicados por SEC y mantiene identidades
separadas del perfil US-GAAP. Las siguientes ampliaciones son soporte explícito de moneda funcional
distinta de USD, mayor cobertura de conceptos IFRS y más emisores solo después de smoke real e
idempotencia.

### SMV

Después del catálogo peruano:

- incorporar estados oficiales por empresa, período, tipo y moneda;
- revisar taxonomía, signo y unidad;
- reutilizar una métrica únicamente si los inputs son contablemente equivalentes;
- crear definiciones peruanas versionadas cuando no exista equivalencia.

### Clasificación empresarial

Completar crecimiento lento, estable y rápido; cíclica; recuperación; y activo oculto. Las tres
últimas requieren evidencia sectorial, macro, de reestructuración o de composición de activos. No
se deducirán solo de ratios.

Las influencias de Graham, Buffett y Lynch se documentan en la selección y definición de métricas,
pero la interfaz continúa organizando por tipo de análisis. Dalio corresponde al dominio macro y de
regímenes, no a ratios corporativos artificiales.

## Fase 5 — Herramientas de mercado y comparación

Incorporar por contratos pequeños y probados:

- EMA configurables;
- RSI;
- MACD;
- Bandas de Bollinger;
- ATR y volatilidad histórica;
- beta y correlación frente a un benchmark identificado;
- recuperación de drawdown;
- indicadores de volumen solo cuando la cobertura lo permita;
- comparación normalizada de varios activos;
- benchmarks por mercado y sector;
- acciones corporativas y eventos en la gráfica;
- anotaciones y plantillas locales.

Cada indicador declara fórmula, ventana, frecuencia, warm-up, inputs, unidad, versión y limitación.
Ninguno utiliza observaciones posteriores al corte. Los indicadores se habilitan por paneles
configurables para no saturar la vista.

## Fase 6 — Cripto como familia propia

1. Programar refresh diario e intradía incremental.
2. Añadir backfill por ventanas reanudables y recibos de cobertura.
3. Generalizar el catálogo a otros criptoactivos solamente cuando se seleccionen fuentes.
4. Diseñar fundamentales de red separados de mercado: oferta, emisión, actividad, fees, seguridad,
   distribución y concentración.
5. Incorporar eventos regulatorios y noticias.
6. Evaluar métricas on-chain bajo fuente, cadena, timestamp y evidencia propios.

Una etiqueta heurística de wallet no se interpreta como identidad institucional confirmada.

## Fase 7 — Macro y larga historia

1. Integrar el fundamento FRED/ALFRED existente. Completado para el scheduler local.
2. Crear catálogo versionado de inflación, crecimiento, tipos, curva, empleo, crédito, liquidez,
   dólar y commodities. Completado; las series diarias quedan explícitamente diferidas.
3. Enumerar vintages, actualizar en lotes y reanudar. Completado para bordes nuevos de seis series
   de baja frecuencia; falta backfill histórico y almacenamiento columnar.
4. Añadir variables peruanas oficiales mediante BCRP/INEI cuando corresponda.
5. Construir un panel descriptivo de regímenes inspirado en relaciones macro de crecimiento,
   inflación, liquidez y tipos.
6. Mantener este dominio separado de fundamentales y mercado de un activo.

La historia anterior a la cotización de una empresa vive en un workspace de investigación mediante
índices, macro y factores. French, Shiller y otras fuentes requieren catálogo de licencia,
transformación y vintage. CRSP u otra fuente pagada se evaluará solo si su utilidad justifica el
coste.

## Fase 8 — Noticias y eventos

La primera etapa es determinista:

- SEC y filings;
- SMV/BVL;
- bancos centrales y organismos estadísticos;
- relaciones con inversores;
- GDELT como descubrimiento, no confirmación.

El corpus conserva `event_at`, `published_at`, `available_at` y `retrieved_at`, licencia, URL,
identificador externo, hash, idioma, entidades y revisiones. Debe deduplicar sindicación y
correcciones, permitir búsqueda local y mostrar una línea temporal sin inferir causalidad.

## Fase 9 — Cazatiburones

Versión inicial para acciones:

- Form 13F;
- Forms 3, 4 y 5;
- Schedules 13D y 13G;
- cambios de posiciones reportadas;
- entradas, salidas y concentración;
- transacciones de insiders;
- propiedad directa e indirecta;
- demora real entre evento, filing y disponibilidad.

Para BVL se evaluarán participaciones significativas y comunicaciones SMV. Cripto tendrá otro
contrato on-chain. No existirá un “score de tiburones”.

## Fase 10 — Screening y alertas

El diseño completo se encuentra en
[`automated_screening_alerts.md`](automated_screening_alerts.md). La detección numérica será
determinista, point-in-time y de bajo consumo. Una alerta señalará un candidato para revisión y
explicará condiciones y evidencia; no afirmará que debe comprarse o venderse.

La implementación se divide en:

1. alertas operativas y modo silencioso después del scheduler multi-activo —base local completada
   el 29 de julio de 2026 con motor trivaluado, replay, deduplicación, transiciones auditadas y
   cierre automático tras una recuperación completa del mismo trabajo—;
2. reglas de mercado y fundamentales con métricas existentes —primer monitor local completado con
   motor puro, contratos point-in-time, dos plantillas silenciosas, persistencia, deduplicación,
   confirmaciones, histéresis, cooldown, bandeja separada y conexión al scheduler solo ante
   evidencia nueva; también incluye editor versionado, restauración auditable, locking optimista y
   replay histórico acotado de frecuencia y ruido; queda ampliar el catálogo y observarlo durante
   varios ciclos silenciosos—;
3. reglas de valoración, técnicas y plantillas configurables;
4. eventos, filings, noticias y Cazatiburones;
5. resumen opcional mediante IA solo después de una activación determinista.

## Fase 11 — Interfaz profesional completa

- watchlists y favoritos;
- ficha rápida del activo, cobertura y frescura;
- comparación multi-activo;
- indicadores configurables;
- evolución fundamental y valoración histórica;
- contexto macro;
- noticias, eventos y Cazatiburones;
- bandeja de candidatos y alertas;
- plantillas guardadas;
- búsqueda global;
- exportaciones exactas.

La interfaz conserva español consistente, términos familiares de mercado, decimales por unidad,
dirección visual inequívoca, teclado, tecnologías de asistencia, responsive y tema oscuro. Los
paneles pesados se cargan bajo demanda.

## Fase 12 — IA cualitativa opcional

Solo después del corpus de noticias:

- adaptador independiente del proveedor;
- ejecución bajo demanda o activada por una alerta;
- presupuesto y límite de tokens;
- modelo, versión, parámetros y plantilla registrados;
- documentos exactos, citas y afirmaciones no verificadas;
- protección contra prompt injection;
- salida separada de resultados cuantitativos.

La aplicación seguirá funcionando sin IA. La IA no modifica observaciones, decide por sí sola una
oportunidad ni produce una recomendación o puntuación conjunta.

## Fase 13 — Investigación predictiva

En workspace separado:

- features point-in-time;
- particiones walk-forward;
- baselines simples;
- costes y latencia;
- supervivencia, deslistados y cambios de universo;
- drift;
- evaluación fuera de muestra;
- registro de experimentos;
- intervalos de incertidumbre.

Una predicción seguirá siendo investigación, no una orden o recomendación.

## Fase 14 — Operación 24/7 y release

Después de estabilizar el scheduler multi-fuente:

- backup y restauración probados;
- logs acotados y métricas de latencia, errores y frescura;
- watchdog y recuperación;
- inicio automático de WSL o despliegue ARM64;
- benchmark antes de elegir un SBC;
- empaquetado para Orange Pi u otro equipo;
- acceso remoto solo con VPN, autenticación y TLS;
- actualización y rollback compatibles con el workspace.

El servidor local no se expondrá directamente en `0.0.0.0`.

## Puerta sistemática de calidad

Cada cambio debe completar:

1. alcance, contrato y análisis de compatibilidad;
2. modelos tipados y versiones explícitas;
3. pruebas offline con fixtures mínimos;
4. integración en workspace temporal;
5. point-in-time, idempotencia, identidad, revisión y aislamiento;
6. fallos parciales y reanudación;
7. Ruff y formato;
8. Pytest completo y cobertura;
9. auditoría de dependencias, configuración y secretos;
10. smoke real sin tocar primero el workspace permanente;
11. repetición real para verificar reutilización;
12. benchmarks de tiempo, memoria y tamaño;
13. pruebas HTTP, navegador, responsive y accesibilidad cuando apliquen;
14. revisión de diff, commit, push, PR y CI remoto;
15. smoke posterior a la fusión en `main`.

Los nuevos conectores y módulos de almacenamiento críticos deberían superar 90 % de cobertura
local, sin elevar artificialmente el mínimo global antes de cubrir el código histórico.

## Optimización transversal

- refrescos incrementales y recibos de cobertura;
- una conexión writer por ejecución;
- lotes por proveedor y rate limiting centralizado;
- cachés acotadas e invalidadas por fuente;
- consultas DuckDB por columnas y rangos;
- Parquet particionado para macro, noticias y corpus grandes;
- cálculos rodantes limitados al warm-up necesario;
- agregación previa a enviar décadas al navegador;
- tablas bajo demanda y virtualizadas;
- evaluación de alertas solo sobre evidencia nueva;
- límites de memoria, respuesta y concurrencia;
- backfills por ventanas reanudables;
- migraciones verificadas mediante backup y restauración.

## Orden inmediato recomendado

La ruta crítica vigente se mantiene en
[`basic_functional_release_plan.md`](basic_functional_release_plan.md). Su orden es:

1. estabilizar, validar y fusionar el PR de orquestación y screening;
2. eliminar el centralismo heredado de AAPL mediante contratos genéricos y adaptadores compatibles;
3. eliminar fallos operativos recurrentes y validar backup, restauración y soak silencioso;
4. desacoplar lecturas de refresh largos, compactar la API operativa y medir presupuestos;
5. completar watchlist, valoración, indicadores, reglas y notificaciones reanudables;
6. incorporar corpus oficial y la primera vertical SEC de Cazatiburones;
7. añadir IA cualitativa opcional con citas y presupuesto sobre ese corpus;
8. cerrar la versión mediante CI, smokes, 72 horas de operación y recuperación probada.

Mercado BVL, macro diario columnar, extensiones Cazatiburones para BVL/cripto e investigación
predictiva conservan sus fases de esta hoja de ruta, pero no desplazan la estabilización de la
versión básica ni se implementan sin resolver previamente sus fuentes, licencias y contratos.
