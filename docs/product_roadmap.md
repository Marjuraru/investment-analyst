# Hoja de ruta estratégica del producto

## Propósito y definición de producto completo

Esta hoja de ruta ordena las ampliaciones necesarias para convertir el MVP local actual en una
herramienta profesional completa de apoyo al análisis. Mercado, fundamentales, macro, noticias y
actividad de participantes conservan significado, evidencia y diagnósticos independientes. La
versión actual no ejecuta órdenes, no administra dinero ni convierte sus resultados en una
recomendación personalizada o puntuación agregada.

La comparación diaria multi-activo disponible es una lectura descriptiva local sobre una muestra común; no introduce ranking, recomendaciones, señales ni automatización de ejecución.

La evolución conserva una dirección por capas, no una autorización presente:

`evidencia PIT → análisis → detección de oportunidades → señales/predicción validada → recomendación explícita y trazable → decisión humana/política → broker y ejecución controlada futura → posible automatización acotada posterior`

Las restricciones permanentes son transparencia, auditabilidad, reproducibilidad, `available_at`,
`Decimal`, identidades deterministas, historia append-only, separación de capas e independencia de
proveedores en analytics. Una recomendación futura será explícita, no personalizada, versionada y
trazable; una futura ejecución estará fuera del núcleo analítico y requerirá contratos, autorización
y controles de riesgo propios.

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

## Decisión de arquitectura híbrida local-first

La propuesta `DuckDB/Parquet → modelo tabular → atribuciones SHAP → LLM` se adopta como
carril de investigación con las siguientes correcciones:

- DuckDB es el motor OLAP local y Parquet el formato columnar para datasets voluminosos; no son la
  capa de descarga ni sustituyen validación, modelos tipados, raw JSON/XML o catálogos de fuente;
- SQL, `Decimal` y algoritmos versionados continúan calculando hechos, métricas, correlaciones y
  estadísticas deterministas;
- un motor tabular local podrá evaluar anomalías o hipótesis predictivas solamente en un
  workspace de investigación;
- XGBoost y LightGBM son candidatos, no dependencias decididas: primero se compararán con regresión
  regularizada, reglas robustas y otros baselines simples por precisión fuera de muestra, memoria,
  latencia, reproducibilidad y soporte ARM64;
- SHAP se calcula localmente para atribuir una predicción al modelo; no demuestra causalidad, no
  valida la predicción y no reemplaza valores, unidades, cobertura o incertidumbre;
- el LLM no recibe series temporales crudas ni ejecuta cálculos. Solo redacta una explicación a
  partir de un paquete JSON compacto, versionado y ya calculado;
- la aplicación cuantitativa funciona sin modelo estadístico, SHAP, LLM o infraestructura cloud.

El paquete explicativo no contiene solo SHAP. Incluye resultado, baseline, valores originales de
las features, principales atribuciones, calibración o rango del score, corte point-in-time,
versiones, evidencia, limitaciones y, cuando corresponda, fragmentos documentales citables. El LLM
produce narrativa, nunca el diagnóstico matemático ni una recomendación.

### Dominios estructuralmente independientes

1. **Evidencia PIT:** conectores, raw, normalización, catálogo, `available_at`, DuckDB y Parquet.
2. **Cálculo e inferencia local:** métricas deterministas, features, entrenamiento, inferencia,
   calibración, SHAP y validación temporal.
3. **Interfaz semántica:** API local, gráficos, reportes, citas y LLM opcional.

No se permite que el dominio 3 escriba observaciones, recalcule el dominio 2 o modifique evidencia
del dominio 1. Tampoco se desplegará una base de datos o servidor de inferencia cloud como
dependencia del análisis central.

La disciplina de desarrollo usará requisitos, diseño, implementación y verificación trazados de
forma bidireccional. Puede inspirarse en el V-model de VDI/VDE 2206, pero el proyecto no afirmará
conformidad con una norma destinada a sistemas mecatrónicos. Para software y modelos se tomarán como
referencias proporcionales ISO/IEC/IEEE 12207 y las funciones `govern`, `map`, `measure` y `manage`
de NIST AI RMF. Esto no implica certificación formal.

## Estado de referencia

Referencia de contratos: 9 de agosto de 2026, `main` en
`00188caf34e045cfb5ed79d62f0289a15e6bb265`. Los checkpoints anteriores conservan valor histórico,
pero no describen por sí solos el estado operativo actual.

### Funcional

- persistencia local DuckDB/Parquet, identidades deterministas y evidencia append-only;
- mercado diario para el universo gratuito Alpaca IEX y los pares spot BTC-USD y ETH-USD de
  Coinbase Exchange;
- base intradía BTC-USD de un minuto y nueve agregaciones locales;
- fundamentales SEC completos para AAPL, diez emisores genéricos US-GAAP y tres emisores IFRS
  anuales (Barrick, BVN ADR y TSM);
- 40 métricas fundamentales, historia, comparación y clasificación explicable cuando la evidencia
  alcanza;
- gráficos diarios, semanales, mensuales e intradía, velas, escala logarítmica, zoom, arrastre y
  tres SMA configurables;
- interfaz local, exportaciones, cachés, compresión, runtime por capacidades, scheduler persistente
  de watchlist y servicio `systemd --user`;
- preferencias versionadas y persistentes de watchlist, favoritos y actualización programada;
- valoración corporativa point-in-time v1 para empresas elegibles, con estados explícitos cuando la
  base del título, el precio, el filing o la moneda no permiten evaluar una métrica, e historia
  materializada local de resultados evaluados sin backfill;
- importación FRED/ALFRED por vintage explícito, evidencia macro append-only y reconstrucción local
  point-in-time con disponibilidad diaria conservadora;
- catálogo de seis cotizaciones BVL, cliente HTTPS SMV, evidencia registral append-only,
  reconstrucción point-in-time y refresh por lote reanudable;
- dependencias reproducibles, Ruff, Pytest, cobertura, auditoría y CI.

### Parcial

- BVL mercado: lector tipado del boletín diario, todavía sin autorización resuelta para
  persistencia automática, histórico o interfaz;
- fundamentales BVL: requieren el futuro adaptador SMV y no reutilizan el perfil SEC del ADR;
- screening automático, bandejas locales y outbox con acuse local implementados; catálogo ampliado
  y notificaciones externas pendientes.

### Diseñado, no implementado

- fundamentales SMV;
- indicadores técnicos adicionales y plantillas;
- fundamentales de red para cripto;
- catálogo macro y workspace de historia larga;
- corpus y búsqueda de noticias;
- dominio Cazatiburones;
- IA cualitativa opcional y laboratorio predictivo;
- autenticación o exposición remota segura.

## Alcance inicial de activos

El catálogo actual cubre mercado estadounidense para AAPL, AMD, Barrick (`B`), BVN, CDE, HYMC,
INTC, MSTR, MU, MUX, NEM, PLTR, SCCO, TSM, GBTC, GLD e IBIT, además de BTC-USD y ETH-USD. La
cobertura fundamental no es igual para todos; los dos criptoactivos solo tienen el dominio de
mercado spot diario y BTC conserva, además, un contrato intradía separado.

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

La base quedó integrada en `main` y el scheduler dejó de representar una ejecución Apple: ahora
ejecuta trabajos explícitos por activo, proveedor, dominio, frecuencia y zona horaria, derivados
del catálogo, las capacidades y la watchlist persistente.

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

La entrega actual cubre Alpaca diario, SEC, Coinbase diario para BTC-USD y ETH-USD, Coinbase
intradía para BTC-USD, registro SMV y seis series FRED/ALFRED de baja frecuencia cuando existe la
API key. Incluye selección explícita o watchlist completa, estado, frescura y cobertura por job, un
único writer, reintentos acotados, backoff, recuperación tras reinicios y preservación de progreso
previo. BVL diario continúa bloqueado por su contrato de uso; noticias y filings se añadirán cuando
sus conectores estén listos. Queda medir presupuestos reales por proveedor y diseñar almacenamiento
columnar para fuentes de alto volumen.

## Fase 4 — Fundamentales corporativos completos

### US-GAAP

La valoración corporativa point-in-time v1 ya reconstruye market cap, enterprise value y múltiplos
anuales para empresas elegibles. La historia materializada local muestra únicamente resultados ya
persistidos hasta un corte explícito, con revisión, cobertura sparse y estadísticas descriptivas
Decimal; no aplica a ETF o cripto, no infiere una base de acciones ausente ni reemplaza reglas
posteriores de esta fase. Las reglas relativas versionadas cierran la lectura descriptiva de esa
historia sin crear señales, recomendaciones ni candidatos.

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

Ya disponibles por contratos pequeños y probados: EMA configurables con linaje derivado MKT-2,
Bandas de Bollinger y MKT-3: comparación diaria multi-activo PIT con normalización base 100,
retorno, volatilidad, drawdown, correlación y beta v1 sobre una muestra común. Pendientes:

RSI Wilder, MACD y ATR PIT ya están disponibles como métricas descriptivas y alimentan únicamente
candidatos técnicos SILENT. La outbox local durable registra cada candidato `new` y su acuse sin
entrega externa; los canales de navegador, sistema o red permanecen pendientes.

- indicadores de volumen solo cuando la cobertura lo permita;
- benchmarks por mercado y sector;
- acciones corporativas y eventos en la gráfica;
- anotaciones y plantillas locales.

Cada indicador declara fórmula, ventana, frecuencia, warm-up, inputs, unidad, versión y limitación.
Ninguno utiliza observaciones posteriores al corte. Los indicadores se habilitan por paneles
configurables para no saturar la vista.

## Fase 6 — Cripto como familia propia

El mercado spot diario productivo ya está activo para BTC-USD y ETH-USD mediante Coinbase Exchange.
El intradía de un minuto, sus nueve agregaciones locales y su ventana acotada siguen siendo un
contrato separado exclusivo de BTC-USD; no se infiere cobertura intradía para ETH-USD.

La base de derivados productiva también está implementada para BTC y ETH mediante métodos públicos
Deribit: funding horario, DVOL diario, snapshots prospectivos, receipts, métricas Decimal34, replay
PIT y scheduler. Es descriptiva, no puntuada, y no autoriza opciones, basis, señales ni trading. Su
semántica y limitaciones están en [`crypto_derivatives.md`](crypto_derivatives.md). La interfaz
local expone ahora ese replay en un panel separado y lazy para activos elegibles del catálogo, sin
mezclarlo con el mercado spot ni convertirlo en señal.

1. Mantener el backfill Deribit por ventanas/receipts y extenderlo a otra fuente sólo mediante un
   contrato independiente.
2. Generalizar el catálogo a otros criptoactivos solamente cuando se seleccionen fuentes.
3. Diseñar fundamentales de red separados de mercado: oferta, emisión, actividad, fees, seguridad,
   distribución y concentración.
4. Incorporar eventos regulatorios y noticias.
5. Evaluar métricas on-chain bajo fuente, cadena, timestamp y evidencia propios.

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

Las anomalías se evaluarán localmente sobre features point-in-time de filings: tamaño relativo de
una transacción, cambio de tenencia, concentración, recurrencia, enmiendas y latencia de reporte. El
primer baseline será determinista o estadístico robusto. Un modelo XGBoost/LightGBM solo se usará
si existe objetivo o etiqueta defendible y supera esos baselines fuera de muestra.

Volumen y cartera de órdenes pertenecen a un futuro dominio de microestructura. IEX gratuito es una
sola bolsa y no permite atribuir actividad al mercado consolidado ni a una institución. Un score de
anomalía no se llamará probabilidad salvo que proceda de un clasificador calibrado; por ello no se
fija `P > 0,85` como regla universal.

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

- watchlists y favoritos persistentes ya disponibles; faltan ampliaciones de experiencia y plantillas
  guardadas;
- ficha rápida del activo, cobertura y frescura;
- comparaciones ampliadas por mercado o sector, separadas de la muestra diaria MKT-3;
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

Para explicar una inferencia cuantitativa, el LLM recibirá solo un `ModelExplanationPacket`
compacto: predicción o score, baseline, valores de features, atribuciones SHAP locales, calidad,
calibración, incertidumbre, versiones y evidencia. Para resumir filings o noticias podrá recibir
fragmentos citables del corpus, no series numéricas completas. Toda aritmética, correlación,
selección temporal y SHAP se ejecuta antes y localmente.

La activación requiere evidencia nueva, trazabilidad completa, candidato local elegible, cooldown,
deduplicación y presupuesto disponible. El objetivo inicial de 800 tokens se aplica al contenido de
evidencia condensada y se medirá junto con instrucciones, salida y coste realmente facturado. El
límite final será configurable por proveedor; ningún umbral ni tamaño de prompt prometerá coste
“cero” sin medir precios vigentes.

La aplicación seguirá funcionando sin IA. La IA no modifica observaciones, decide por sí sola una
oportunidad ni produce una recomendación o puntuación conjunta.

## Fase 13 — Investigación predictiva

En workspace separado:

- **P0 — pregunta y etiqueta:** declarar objetivo, horizonte, unidad de observación, utilidad y
  errores tolerables antes de elegir algoritmo;
- **P1 — matriz PIT:** generar features mediante DuckDB y snapshots Parquet inmutables con
  `feature_available_at`, universo histórico y versión;
- **P2 — baselines y candidatos:** comparar reglas robustas y modelos lineales contra XGBoost y
  LightGBM; elegir como máximo un booster para la primera implementación;
- **P3 — validación:** purged walk-forward con embargo según horizonte de label, ajuste solo en
  train/validation y holdout cronológico final intacto;
- **P4 — calibración y explicación:** reliability curve, Brier/log-loss cuando exista
  probabilidad, umbral elegido por coste de falsos positivos y SHAP local;
- **P5 — shadow mode:** inferencia local sin alertar, drift de datos/predicción, estabilidad por
  régimen, latencia, memoria y comparación continua con baseline;
- **P6 — promoción reversible:** activar una salida experimental solo si supera puertas
  predefinidas y puede deshabilitarse sin afectar el núcleo determinista.

Se prohíbe `KFold` aleatorio, shuffle y cualquier partición que entrene con observaciones futuras.
El purge elimina muestras cuyo intervalo de formación de label se solape con validación/test; el
embargo añade separación tras la frontera. Transformaciones, imputación, selección de features,
calibración y tuning se ajustan dentro de cada fold, nunca sobre todo el dataset.

Si no existen etiquetas confiables, el resultado se denomina `anomaly_score` o percentil, no
probabilidad. Un umbral como `0,85` solo se permitirá cuando la salida sea una probabilidad
calibrada fuera de muestra y el umbral quede justificado por una política de alertas versionada.

Una predicción seguirá siendo investigación, no una orden ni una recomendación actual. Sólo después
de todas las puertas PIT, validación temporal, calibración cuando aplique, explicación, shadow mode
y rollback podrá promoverse a una señal o detección de oportunidad versionada; cualquier
recomendación futura seguirá siendo un artefacto separado y trazable.

## Fase 14 — Operación 24/7 y release

Después de estabilizar el scheduler multi-fuente:

- readiness operacional reproducible y backup/restauración detenidos, propuestos por OPS-8 y sólo
  efectivos después de AUDIT, rehearsal HUMAN exact-SHA y merge;
- logs acotados y métricas de latencia, errores y frescura;
- watchdog y recuperación;
- inicio automático de WSL o despliegue ARM64;
- benchmark antes de elegir un SBC;
- empaquetado para Orange Pi u otro equipo;
- acceso remoto solo con VPN, autenticación y TLS;
- actualización y rollback compatibles con el workspace.

El servidor local no se expondrá directamente en `0.0.0.0`.

## Horizonte posterior — recomendación y ejecución controlada

Este horizonte no está implementado, autorizado ni calendarizado. Tras una señal validada, una
recomendación futura requerirá política explícita, evidencia separada de mercado y fundamentales,
limitaciones y trazabilidad completa. Sólo después de una decisión humana o de política y contratos
de broker independientes podría evaluarse ejecución controlada; una automatización acotada sería un
horizonte todavía posterior y nunca reescribiría evidencia, señales, recomendaciones o decisiones.

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

Todo cambio de modelado añade además: contrato de label, auditoría de leakage, purged walk-forward,
holdout final, comparación con baseline, calibración cuando corresponda, manifest de modelo,
reproducibilidad de SHAP, shadow mode, drift y criterio de rollback. Una mejora media no compensa un
modelo inestable entre regímenes ni una tasa de falsos positivos incompatible con el presupuesto.

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
- migraciones verificadas mediante backup y restauración;
- matrices de features generadas por consulta local y materializadas por corte, no por DataFrames
  completos mantenidos permanentemente en memoria;
- inferencia por lote y SHAP solo para resultados candidatos, no para todo el universo en cada
  polling;
- caché por hash de evidencia, feature set, modelo y versión de explicación;
- límites diarios de llamadas, tokens y moneda para cada proveedor LLM.

## Orden inmediato recomendado

La ruta crítica vigente se mantiene en
[`basic_functional_release_plan.md`](basic_functional_release_plan.md). Su orden es:

1. estabilizar el runtime por capacidades y observar los jobs de la watchlist persistente;
2. eliminar el centralismo heredado de AAPL mediante contratos genéricos y adaptadores compatibles;
3. integrar OPS-8 tras su aceptación HUMAN, conservar los fallos clasificados visibles y medir por
   job el peak global no atribuido antes de fijar un presupuesto de memoria;
4. desacoplar lecturas de refresh largos, compactar la API operativa y medir presupuestos;
5. ampliar la valoración v1 hacia historia y reglas posteriores, e incorporar indicadores
   restantes, reglas y notificaciones reanudables;
6. incorporar corpus oficial y la primera vertical SEC de Cazatiburones;
7. abrir el carril predictivo local con matriz PIT, baselines y validación temporal, sin promoverlo
   a diagnóstico operativo;
8. añadir IA cualitativa opcional con citas y presupuesto sobre evidencia ya calculada;
9. cerrar la versión mediante CI, smokes, una captura operacional finita y recuperación probada.

La aceptación operacional se implementa con un carril pre-merge acotado: `candidate-stage` y
`candidate-update` sólo adquieren `refs/pull/<pr-number>/head` con SHA/tree exactos, mientras que el
observer read-only registra GET loopback, `systemctl show` y `/proc/<MainPID>/status` en scratch. No
son nuevas capacidades de análisis, no acceden al workspace ni ejecutan providers o scheduler, y no
atribuyen causalidad de memoria. La duración de la captura es finita y explícita, sin duración mínima
ni número mínimo de muestras, sesiones o ciclos como gate. `RELEASE-ACCEPTANCE` fue reconciliada
HUMAN como `RELEASE-ACCEPTANCE DONE → EQUITY-UNIVERSE NEXT → SEC-CORPUS PLANNED`.
La transición integrada es `EQUITY-UNIVERSE DONE → SEC-CORPUS NEXT`; no presenta SEC-CORPUS como
implementado. `EXTENDED-SOAK / DEDICATED-RUNTIME ALWAYS-ON ACCEPTANCE` queda diferido a un Work
Block independiente.

Mercado BVL, macro diario columnar, extensiones Cazatiburones para BVL/cripto e investigación
predictiva conservan sus fases de esta hoja de ruta, pero no desplazan la estabilización de la
versión básica ni se implementan sin resolver previamente sus fuentes, licencias y contratos.

## Referencias técnicas de esta decisión

- [DuckDB: lectura de Parquet y pushdown](https://duckdb.org/docs/stable/data/parquet/overview)
- [XGBoost: predicción y contribuciones TreeSHAP](https://xgboost.readthedocs.io/en/stable/prediction.html)
- [LightGBM: algoritmos histogram-based](https://lightgbm.readthedocs.io/en/stable/Features.html)
- [scikit-learn: particiones temporales y `gap`](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)
- [scikit-learn: calibración de probabilidades](https://scikit-learn.org/stable/modules/calibration.html)
- [Lundberg y Lee: fundamento de SHAP](https://arxiv.org/abs/1705.07874)
- [ISO/IEC/IEEE 12207:2026](https://www.iso.org/standard/90219.html)
- [NIST AI Risk Management Framework](https://www.nist.gov/itl/ai-risk-management-framework)
- [VDI/VDE 2206: alcance mecatrónico y ciberfísico](https://www.vdi.de/en/home/vdi-standards/details/vdivde-2206-development-of-mechatronic-and-cyber-physical-systems)
- [Alpaca: alcance de IEX frente a SIP](https://docs.alpaca.markets/us/docs/historical-stock-data-1)
