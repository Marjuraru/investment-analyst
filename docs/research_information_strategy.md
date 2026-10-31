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
   mirar resultados. La interpretación cualitativa no bloquea este paso.
7. Evaluar análisis cualitativo opcional mediante IA sobre paquetes ya calculados, con presupuesto,
   citas y auditoría; puede desconectarse sin afectar datos, inferencia ni interfaz numérica.
