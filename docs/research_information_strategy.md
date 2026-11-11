# Estrategia de historia larga y noticias

## Decisión

La herramienta incorporará historia de investigación y noticias como dominios independientes de las
barras operativas de cada activo. Una serie macroeconómica de 1950, un factor de mercado de 1926 y
una noticia publicada hoy no son observaciones intercambiables ni se insertarán artificialmente en
el histórico de precios de una empresa.

La arquitectura conservará cuatro tiempos cuando correspondan:

- `event_at`: cuándo ocurrió el hecho descrito;
- `published_at`: cuándo lo publicó la fuente;
- `available_at`: desde cuándo podía conocerlo el analista;
- `retrieved_at`: cuándo lo obtuvo la herramienta.

Toda relación con retornos, fundamentales o diagnósticos utilizará `available_at` para impedir
información futura. El dato revisado actual no sustituirá silenciosamente el valor conocido en el
pasado.

## Historia desde 1950

La profundidad histórica se organizará por función analítica:

1. **Precio del activo individual.** Se conserva la historia real disponible para su identidad,
   mercado y proveedor. No se extiende una empresa antes de su cotización ni se reemplaza por un
   índice sin declararlo.
2. **Índices y ciclos de mercado.** Las
   [series de Robert Shiller](https://www.econ.yale.edu/~shiller/data.htm) aportan precios,
   dividendos, beneficios, inflación y tipos estadounidenses desde 1871 para análisis de régimen
   de muy largo plazo.
3. **Factores y carteras de investigación.** La
   [biblioteca de Kenneth French](https://mba.tuck.dartmouth.edu/pages/Faculty/ken.french/Data_Library/det_10_port_form_pr_12_2_daily.html)
   contiene retornos estadounidenses desde 1926. Son carteras o factores académicos, no precios de
   los activos de la lista de seguimiento.
4. **Macroeconomía y vintages.** La
   [API de FRED](https://fred.stlouisfed.org/docs/api/fred/fred/overview.html) permite obtener series
   completas y [ALFRED](https://fred.stlouisfed.org/docs/api/fred/alfred.html) conserva fechas de
   publicación y revisiones. Este será el candidato prioritario para crecimiento, inflación, tipos,
   empleo, crédito y liquidez con semántica point-in-time.
5. **Datos institucionales futuros.** CRSP u otras fuentes licenciadas solo se evaluarán cuando la
   utilidad justifique el coste. Sus datos no se mezclarán con fuentes gratuitas bajo un mismo
   `source_id`.

Esta separación permite estudiar desde aproximadamente 1950 —e incluso antes— sin afirmar que existe
historia individual donde no la hay. Los modelos de régimen usarán índices, factores y macro; el
análisis del activo conservará su propio período observable.

## Contrato futuro para noticias

La primera etapa será ingestión y búsqueda, no interpretación automática. Cada documento deberá
conservar como mínimo:

- proveedor, identificador externo, URL canónica y tipo de fuente;
- título, idioma, autores y medio cuando estén disponibles;
- `published_at`, `available_at` y `retrieved_at`;
- hash del contenido realmente autorizado para almacenar;
- texto completo, extracto o solo metadatos según licencia;
- activos, empresas, países, sectores y temas mencionados, con evidencia de la extracción;
- relaciones de revisión, duplicado o sindicación;
- calidad y limitaciones explícitas de cobertura.

Las fuentes primarias —SEC, bancos centrales, organismos estadísticos, bolsas y relaciones con
inversores— tendrán identidad separada de agregadores periodísticos.
[GDELT GKG](https://data.gdeltproject.org/documentation/GDELT-Global_Knowledge_Graph_Codebook-V2.pdf)
es candidato gratuito para descubrimiento global y metadatos estructurados; una coincidencia de
entidad o tono no se tratará como un hecho confirmado.

La deduplicación utilizará URL normalizada, identificador del proveedor y hash. Una corrección crea
otra revisión append-only. Si la licencia no permite conservar el cuerpo, se guardarán únicamente
metadatos, hash, extracto permitido y enlace a la fuente.

## Preparación para IA cualitativa

La ingestión determinista no dependerá de un LLM. La interpretación cualitativa es una capa
opcional, intercambiable y posterior; no es prerrequisito de la investigación cuantitativa ni de un
pronóstico numérico. Una operación opcional y explícita podrá leer el corpus local o un
`ModelExplanationPacket` ya calculado y producir un resultado cualitativo separado que registre:

- documentos exactos utilizados;
- modelo, proveedor, versión, parámetros y plantilla;
- fecha de ejecución y corte `known_at`;
- respuesta estructurada, citas y afirmaciones no verificadas;
- coste, tokens y errores;
- versión del algoritmo de relación con datos cuantitativos.

Una salida de IA nunca modificará observaciones fuente ni se convertirá en recomendación, orden,
score conjunto o verdad histórica. El sistema seguirá funcionando sin IA durante su operación
normal.

## Relación cuantitativa responsable

Antes de entrenar o afirmar capacidad predictiva se exigirán:

- una pregunta, target, horizonte, unidad, universo, calendario y `decision_at` declarados antes de
  mirar resultados, junto con baselines, métrica primaria y regla de rechazo;
- datos point-in-time y vintages, con `feature_available_at <= decision_at` y un label estrictamente
  posterior con `label_available_at` separado; no se retrofecha una métrica, un raw o una feature;
- control de supervivencia, cambios de símbolo, splits, composición de índices y revisiones;
- particiones temporales purged walk-forward con embargo; nunca `KFold` aleatorio ni shuffle;
- transformaciones, imputación, selección, calibración y tuning ajustados sólo dentro de
  train/validation, con holdout cronológico final intacto;
- costes y latencias realistas, evaluados por separado de la accuracy o el retorno bruto;
- comparación contra baselines simples y métricas fuera de muestra con incertidumbre que respete la
  dependencia temporal;
- registro de todos los intentos, incluidos los fallos, sin seleccionar umbrales sobre el holdout;
- separación entre correlación descriptiva, pronóstico, señal y decisión humana.

La historia larga aumenta el número de regímenes observables, pero no garantiza precisión. Su primer
uso será contexto, pruebas de robustez y relaciones transparentes. La evidencia de la investigación
cripto ([síntesis 2026-08](research/crypto/crypto_research_synthesis_2026-08.md) §3.1–3.2 y
[brief del planificador](research/crypto/crypto_planner_brief.md)) es anti-anclaje: disponer de más
RSI/MACD o de más indicadores no demuestra ventaja predictiva, y una fuente adicional sólo se admite
si la pregunta muestra la carencia y existe licencia, PIT y coste verificados. La
[revisión estratégica externa de 2026-09](planning_context/external_strategy_review_decisions_2026-09.md)
y [historical_research_data.md](historical_research_data.md) documentan decisiones y disponibilidad
histórica; [product_roadmap.md](product_roadmap.md#capacidad-predictiva-e-interpretación-estado-verificable-2026-10-31)
mantiene la matriz integrado/por construir/aceptación y
[basic_functional_release_plan.md](basic_functional_release_plan.md) fija la secuencia táctica.
Estas referencias son evidencia de investigación, no hechos vigentes sobre proveedores ni permiso de
implementación.

## Escala de almacenamiento

- DuckDB mantendrá identidad, catálogo, metadatos, trazabilidad y estados operativos.
- Parquet particionará corpus extensos por dominio, proveedor, año y, cuando corresponda, activo.
- Las consultas leerán columnas y rangos acotados; la interfaz nunca cargará décadas completas sin
  agregación.
- Cada conector tendrá límites, reanudación, recibos de cobertura y una política explícita de
  revisiones.
- Noticias, macro, factores, fundamentales y mercado conservarán pipelines y `source_id` separados.

## Fases

1. Completar el universo multi-activo y sus fuentes de mercado actuales.
2. Completar el [conector FRED/ALFRED](fred_alfred_point_in_time.md): el corte inicial ya importa
   vintages explícitos y prueba la selección point-in-time; faltan catálogo versionado,
   descubrimiento paginado de revisiones, reanudación y almacenamiento macro normalizado.
3. Incorporar índices y factores históricos de investigación con licencias documentadas.
4. Definir y probar ingestión de noticias primarias y descubrimiento GDELT en un workspace temporal.
5. Crear búsqueda local y una línea temporal que relacione documentos y datos sin inferir causalidad.
6. Abrir la investigación predictiva con `PREDICTIVE-FOUNDATION` (P0–P1): pregunta y label
   versionados, elegibilidad temporal, dataset PIT mínimo, baselines y criterio de rechazo antes de
   mirar resultados. La fundación ya quedó abierta por #391; la evaluación de un baseline sólo
   procede si la muestra PIT elegible es suficiente. La interpretación cualitativa no bloquea este
   paso.
7. Evaluar análisis cualitativo opcional mediante IA sobre paquetes ya calculados, con presupuesto,
   citas y auditoría; puede desconectarse sin afectar datos, inferencia ni interfaz numérica.

## Estado de la fundación predictiva (2026-10-31)

`PREDICTIVE-RESEARCH` es el único `NEXT`. `PREDICTIVE-FOUNDATION`/#391 (`ADVANCES`) fijó la
pregunta, el target, el horizonte, `decision_at`, las tres features y el label de cinco intervalos,
y construyó el dataset/export PIT aislado sobre el workspace operativo en solo lectura. La autoridad
técnica y operativa es [`predictive_foundation.md`](predictive_foundation.md).

La ejecución real sobre BTC 2026-08-03..2026-10-31 con corte `2026-10-31T23:21:30.756664Z` produjo
90 decisiones, 1 feature PIT disponible, 89 `not_yet_available`, 84 labels maduros, 6 pendientes y
0 elegibles antes de split; readiness `insufficient_pit_sample`. SOL como control READ_ONLY
reprodujo la misma carencia. La historia de precios existe, pero su `available_at` empieza en
2026-10-21: no es historia entrenable point-in-time. No se entrena, no se retrofecha y no se
promueve un predictor. `ML-BASELINE-VALIDATION` es el siguiente esperado sólo si existe muestra
elegible suficiente; si no, el siguiente PLAN resuelve la carencia temporal demostrada usando
primero evidencia histórica recuperable o captura prospectiva existente.

## Decisión sobre historia externa (2026-11-01)

`PREDICTIVE-HISTORICAL-DATA`/#393 (`ADVANCES`) resuelve la carencia temporal con un archivo
histórico oficial acotado en lugar de esperar captura prospectiva. La fuente es Coinbase Exchange
diario para `crypto:btc-usd` (`2019-01-01..2025-12-31`, ≤2.560 días y ≤12 GET), ya integrada en el
catálogo y en USD; no se sustituye por Binance/USDT ni por precios ajustados de otro mercado.

La historia de precios no es historia de recepción. El archivo conserva `retrieved_at` real y el
dataset declara `availability_policy=historical-close-plus-1h-assumed-v1`,
`availability_basis=historical_reconstruction_assumed` y `strict_pit_verified=false`. Un precio
revisado hoy pudo tener otra primera publicación entonces; esa limitación no se resuelve con el
timestamp de la vela. La reconstrucción permite investigación retrospectiva condicional y nunca
satisface por sustitución el gate de PIT observado ni autoriza promoción o señal.

Las ausencias se conservan, no se rellenan y no se interpretan como precio cero. No se crea un
segundo pipeline de noticias ni se combina mercado con fundamentales. Para unos miles de barras no
se requiere Drive, Colab ni NotebookLM: son opciones de almacenamiento, cómputo o apoyo documental
posteriores, no dependencias del análisis central. No se compran ni descargan modelos grandes y no se
consume un LLM. La autoridad técnica y los comandos viven en
[`predictive_foundation.md`](predictive_foundation.md).

## Resultado de la hipótesis ML (2026-11-01)

`ML-BASELINE-VALIDATION`/#395 evaluó la hipótesis preregistrada sobre el dataset reconstruido
`d927c7f1…`: tres folds walk-forward, regresión logística e histogram gradient boosting frente a
`p=0.5` y al prior de clase, Brier pooled y bootstrap pareado de bloques móviles. El resultado fue
`no_demonstrated_edge`: ningún candidato alcanzó el 5 % de mejora frente a ambas referencias con
límites inferiores positivos, y el prior de clase resultó la mejor referencia (0,248527). El holdout
nunca se abrió y no se ajustó ningún parámetro.

La lectura útil no es "el modelo casi funciona": con estas tres features técnicas y este horizonte,
la comparación no muestra valor incremental sobre una referencia ingenua. Un resultado negativo es
una entrega válida y no se convierte en señal, recomendación ni predictor promovido. Tampoco autoriza
tuning, otra longitud de bloque, más indicadores ni nuevos activos para buscar un ganador. El
siguiente PLAN decide entre una hipótesis de datos/features distinta sustentada por investigación y
el cierre de este experimento; la interpretación LLM sigue siendo una capa posterior sobre resultados
estructurados.

## Entrega local del resultado negativo (2026-11-02)

`ML-RESEARCH-RESULTS-DELIVERY`/#397 (`ADVANCES`) cierra la frontera motor independiente → resultado
verificable → interfaz con un consumidor read-only. La API local `GET
/api/v1/research/ml-report?asset_id=<catalog_id>&run_id=<uuid>` y el disclosure «Evaluación ML de
investigación» verifican el artefacto sellado y muestran `ml-research-report-view-v1`: identidad,
fechas, features, folds, Brier y comparaciones contra ambas referencias, intervalos, calibración,
limitaciones y estado del holdout. No reejecutan el experimento, no abren el dataset, no evalúan el
holdout, no entrenan y no convierten el resultado en señal o recomendación.

El run real `fc4ea570-4ffb-55fc-ac49-17d74c328d24` se mantiene como evidencia negativa verificable:
`status=completed`, `decision=no_demonstrated_edge`, `holdout=not_evaluated`, 0 accesos y 2.532 filas
OOS selladas. La consulta conserva el estado, los hashes y el holdout intactos; el asset ajeno se
rechaza con `409` y el lector funciona sin dependencias ML. La hipótesis de tres features técnicas y
horizonte de cinco intervalos queda cerrada como resultado negativo; no se declara ML imposible ni se
reabre el mismo experimento. No se repiten sus parámetros ni se ajusta el criterio.

La próxima hipótesis pertenece a PLAN y debe ser diferencial, no otro tuning de RSI/MACD, otro ticker
buscando ganador ni shadow del modelo rechazado. Un candidato honesto es una familia de datos/features
distinta —por ejemplo derivados o funding— sólo si la disponibilidad, la cobertura y el coste se
demuestran antes de comprometer un entrenamiento. La interpretación LLM sigue siendo una capa
posterior e independiente sobre resultados estructurados.

## Resultado del aporte incremental de funding (2026-11-03)

`ML-FUNDING-VALIDATION`/#405 ejecutó el experimento que #403 habilitó y #399 había dejado preparado.
El protocolo se congeló antes del primer fit: mismo control técnico de tres features y el mismo modelo
logístico con dos sumas causales de funding, sobre idénticas 633 decisiones de desarrollo, tres folds
walk-forward, embargo y purge originales, 6 fits y criterio preregistrado (≥5 % de mejora relativa de
Brier frente a ambas referencias simples, delta positivo frente al control técnico y tres límites
inferiores positivos).

El resultado real es `no_demonstrated_increment`. El Brier del brazo de funding (0,250944…) es peor
que el del control técnico (0,249403…), que `p=0.5` (0,25) y que el prior de clase (0,248535…). Los
tres deltas son negativos y ningún límite inferior es positivo; tampoco gana en ningún fold
individual. El holdout no se abrió (0 accesos, 0 fits) y la segunda ejecución reutilizó el mismo run
sin reentrenar.

La lectura es directa: con este dataset, estas dos features de funding y este horizonte, no hay
evidencia incremental de desarrollo frente al control técnico ni a las referencias ingenuas. No se
ajustan parámetros, no se prueban otras ventanas de funding, otro ticker ni otro umbral para buscar un
ganador. El resultado no es una señal, recomendación ni predictor promovido, y no autoriza abrir el
holdout. El siguiente PLAN decide si esta hipótesis se detiene y si existe otra justificación
investigativa; la interpretación LLM sigue siendo una capa posterior e independiente sobre resultados
estructurados.

## Investigación de variación futura (2026-11-05)

`VOLATILITY-BASELINE-RESEARCH`/#420 (`ADVANCES`) elige una hipótesis distinta de dirección: anticipar
un proxy de variación futura de BTC en lugar de la probabilidad de retorno positivo. Usa el mismo
archivo Coinbase sellado, ventanas de retornos logarítmicos cuadráticos 1/7/30, referencias de
persistencia 5/30 y media de train, un único `StandardScaler`+`Ridge` con 3 fits, folds con embargo y
un bootstrap pareado de bloques. No reabre #395/#405, no ajusta sus datasets y no usa el holdout.

El run real `44b61556-c34e-5778-9f7f-af081a6cc195` produce `no_demonstrated_increment`. El pooled sí
mejora a las tres referencias (36,48 %, 8,49 % y 21,88 %) con límites inferiores positivos, pero el
fold 2 no supera a `persistence_30d`: 1,6488321182713472 frente a 1,6297878184531283 (−1,17 %). El
criterio preregistrado exige mejora positiva en cada fold, así que no se declara evidencia de
desarrollo. No se ajusta el criterio, no se prueban otras ventanas, activo o semilla, el holdout
permanece `not_evaluated` con 0 fits y no se promueve un predictor. La familia vive en el recorrido
local de investigación como tercer miembro cerrado (`family=volatility`), con el mismo listado,
detalle verificado, contraste descriptivo y descarga que baseline/funding y sin comparación pareada
entre familias. `PREDICTIVE-RESEARCH` permanece `NEXT`,
`FUNDAMENTALS-COVERAGE` sigue `PLANNED` y V1-U4 sigue `UNKNOWN`.

## Descubrimiento y contraste de estudios (2026-11-03)

`RESEARCH-EXPERIMENTS-DELIVERY`/#407 (`ADVANCES`) cierra el recorrido de consulta: el usuario ya no
necesita copiar un UUID para encontrar sus estudios. El panel de investigación carga el listado del
activo, permite elegir familia, página y run, abre el detalle verificado de las tres familias,
contrasta hasta dos condiciones lado a lado y descarga exactamente la proyección consultada. No se crea
un segundo dashboard, un endpoint de comparación ni un artefacto persistente, y la consulta legacy
continúa disponible para baseline.

La honestidad del contraste es explícita: baseline, funding y volatility no comparten target, máscara
ni protocolo, de modo que sus métricas no son una comparación pareada entre estudios y la evidencia
válida sigue estando dentro de cada run. Dos runs de la misma familia tampoco se comparan como pares:
la vista describe condiciones, diferencias y campos desconocidos, sin delta, significancia cruzada,
ranking ni «mejor estudio». El resultado negativo de #405 no se reabre, no se ajusta y no se promueve.

Los cinco runs presentes (2 baseline y 3 funding) son inventario de ejecución, no cinco experimentos
estadísticamente independientes; el listado los recorre por páginas sin elegir el mayor `mtime` ni
ocultar los adicionales. Una raíz ausente es un estado vacío explícito, nunca un cero. El listado no
abre reportes, predicciones, datasets ni lineage: sólo verifica cabeceras de la página solicitada.
`FUNDAMENTALS-COVERAGE` permanece `PLANNED`, `PREDICTIVE-RESEARCH` sigue `NEXT` y V1-U4 permanece
`UNKNOWN`.

## Entrega local de la variación futura (planificada en este bloque)

Este bloque integra la variación futura sellada por #420 al recorrido anterior sin entrenar, sin
promover y sin abrir el holdout: `family=volatility` descubre el run `44b61556…`, detalla su
`no_demonstrated_increment` con folds, MSE/MAE, comparaciones pareadas y el motivo del fold 2, y
descarga `volatility-44b61556….json`. El contraste con baseline/funding es descriptivo lado a lado,
nunca pareado entre familias. La readiness sigue cubriendo sólo el experimento direccional cripto: se
declara explícitamente que readiness no evalúa volatility.

## Evidencia histórica de funding (2026-11-02)

`PREDICTIVE-FUNDING-DATA`/#399 (`ADVANCES`) prepara la evidencia de una hipótesis diferencial: la
presión de financiación del perpetuo puede aportar información sobre el mismo retorno spot futuro de
cinco intervalos. No se afirma causalidad económica, ventaja predictiva ni rentabilidad negociable.
La fuente es Deribit `public/get_funding_rate_history`, reutilizando el cliente, el catálogo y las
operaciones Decimal34 existentes; el spot Coinbase y el funding no se fusionan como una serie de
precios.

El archivo real cubre `2019-12-25T01:00Z..2025-12-25T01:00Z` para `crypto:btc-usd`: 52.536 de 52.608
puntos horarios observados, 72 ausencias, 71 páginas y 14,67 MB. Las features son sumas causales de
`interest_1h` de 24 h y 168 h, unidad ratio, sin `interest_8h`, anualización, composición de retorno
ni imputación. Un cero válido es distinto de un hueco; una ventana de 168 h incompleta queda
`missing` y no se acorta. El `funding_feature_hash` no incluye labels ni el hash del dataset base.

El complemento real tiene 2.186 filas y referencia el dataset `d927c7f1…` por identidad, sin copiar
sus features, labels, precios ni filas. La admisión es `insufficient_funding_coverage`: 1.334 de
1.726 filas base elegibles de TRAIN+VALIDATION (77,29 %) tienen ambas features completas, por debajo
del piso de 95 %. Los pisos de train (1.006), validation (328) y holdout (347) sí se cumplen. La
vista offline sólo materializa TRAIN/VALIDATION; el holdout se verifica y se cuenta, pero no se
entrega a una matriz de entrenamiento. No se entrena, no se calcula correlación, performance,
selección de clases ni target de holdout.

El resultado de #399 no es una prueba estadística ni autoriza entrenar. La revisión
`FUNDING-BOUNDARY-COVERAGE`/#403 separó las causas: 70 de las 72 ausencias coincidían con inicios de
página de 31 días y eran un defecto de partición, no una carencia del proveedor; sólo
`2020-08-27T06:00Z` y `07:00Z` permanecen ausentes. La nueva recepción `9d6c0972…` (52.606/52.608)
y el bundle `fca740b8…` alcanzan `funding_data_ready` con 1.719/1.726 = 99,59 % de cobertura
conjunta. Esa mejora habilita un experimento de funding, no un predictor ni una señal. El siguiente
PLAN decide el experimento sin ampliar fuentes por inercia y debe congelar la comparación del aporte
incremental frente a referencias y control técnico sobre exactamente las mismas fechas, sin tuning
ni promoción automática. La interpretación LLM sigue siendo una capa posterior e independiente.

## Preparación PIT observable en el producto (2026-11-04)

`PIT-READINESS-DELIVERY`/#418 (`ADVANCES`) convierte la admisión PIT en una lectura de producto sin
cambiar la estrategia de información: la misma pregunta, el mismo target de cinco intervalos, las
mismas tres features y los mismos pisos y splits. La información nueva es operativa, no estadística:
el analista puede ver, por activo y corte, cuántas decisiones solicitadas están disponibles, aún no
disponibles, ausentes, incompatibles o ambiguas, y qué código de razón explica cada incidencia.

La lectura es descriptiva y acotada. `research-pit-readiness-view-v1` publica sólo agregados y el
summary ya existente; no expone filas, precios, retornos ni clases del holdout, y rotula que las
incidencias de razón pueden solaparse porque una decisión puede leer varios insumos. Una consulta de
`equity:us:aapl` responde 422 `asset_incompatible`: el experimento registrado cubre cripto spot USD y
este bloque no expande el label ni el universo a equities.

El coste se mantiene proporcional: la proyección reutiliza el constructor del dataset y su caché
firmada, la respuesta se mantiene muy por debajo de 256 KiB y el workspace no cambia (mismo tamaño y
`mtime` de `index.duckdb`, sin WAL, mismo SHA-256 de `manifest.json`). No hay proveedor, no hay
entrenamiento, no hay export persistente en servidor y la descarga del navegador reproduce la
proyección recibida.

La evidencia PIT observada sigue siendo escasa: BTC y SOL informan 1 feature disponible de 93
decisiones en 2026-08-03..2026-11-03 con corte `2026-11-04T19:51:37Z`, y `insufficient_pit_sample`.
El contrato admite hasta 366 fechas por consulta y acumular PIT depende del calendario; no se promete
que esperar alcance los pisos. `PREDICTIVE-RESEARCH` permanece `NEXT` y V1-U4 sigue `UNKNOWN`. La
siguiente decisión debe justificar con evidencia si otra hipótesis preregistrada o datos externos
merecen un experimento, conservando costes, el holdout cerrado y la separación entre reconstrucción
histórica y disponibilidad real.

## Decisión de investigación (2026-11-11, `PREDICTIVE-RESEARCH-DECISION`/#438, `ADVANCES`)

Con el marco de este documento, la [síntesis cripto 2026-08](research/crypto/crypto_research_synthesis_2026-08.md)
§3.1–§3.2 y el [brief del planificador](research/crypto/crypto_planner_brief.md): las tres
hipótesis evaluadas (dirección 5d, funding incremental, variación futura) cerraron como
negativas bajo sus protocolos preregistrados y se conservan como STOP sin tuning ni
reapertura (tabla, IDs, hashes, cortes, criterios y limitaciones en
[`predictive_foundation.md`](predictive_foundation.md)).

Comparación finita de dos alternativas y nada más:

1. Confirmación independiente de variación con protocolo congelado y muestra nueva no
   utilizada ni reservada. Rechazada como GO: no existe evidencia local documentada de
   muestra realmente independiente y suficiente; el holdout reservado no puede
   sustentarla y la descarga o inspección de muestras nuevas está prohibida en este
   bloque. Exigiría además no relajar el criterio fallido (mejora positiva en cada
   fold frente a `persistence_30d`).
2. Conservar la captura PIT y no ejecutar otro experimento hasta contar con evidencia
   suficiente. Adoptada: la preview prospectiva BTC/SOL de D=2026-11-10 sigue
   `inputs_unavailable` sin diferencia frente al último recibo, y la historia
   entrenable PIT observada sigue siendo de una sola muestra.

Resultado obligatorio: `NO_GO` para planificar un nuevo experimento, con condiciones
concretas de reapertura: muestra PIT observada suficiente (pisos 300/150/40/40 con
clases) o una familia de datos/features distinta con disponibilidad, licencia, PIT y
coste verificados, siempre con propuesta preregistrada propia en un PLAN posterior
(pregunta, perfil/fuente, universo y fechas, objetivo/unidad/horizonte, candidato
congelado, baselines, separación temporal/purga/embargo, métrica primaria,
incertidumbre, tamaño mínimo, regla de rechazo original, límites de fits/tiempo/RSS
y criterio stop/go). V1-U4 permanece `UNKNOWN`; un `NO_GO` no prueba imposibilidad
general del ML, sólo rechaza estas hipótesis bajo sus protocolos. Sin porcentaje de
progreso ni promesa de rentabilidad; el LLM sigue posterior/opcional y no resuelve la
ausencia de evidencia cuantitativa. Evidencia histórica asumida y PIT observado
siguen separados; activos y fuentes no se mezclan y ningún resultado retrospectivo se
llama señal.
