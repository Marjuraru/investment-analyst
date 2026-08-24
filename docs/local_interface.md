# Interfaz local y operación continua

La interfaz local convierte los flujos existentes de mercado, fundamentales y BTC-USD en una
herramienta básica utilizable desde el navegador. Un scheduler derivado del catálogo puede
actualizar la watchlist por trabajos independientes. No añade scores combinados, recomendaciones,
Trading API ni un LLM activo.

## Capacidades

`GET /api/v1/market-comparison` acepta `asset_id` repetido, `benchmark_id`, `start`, `end` y `known_at`. Devuelve `market-multi-asset-comparison-v1` desde almacenamiento local de solo lectura; la interfaz carga el gráfico normalizado bajo demanda.

La página permite:

- revisar el workspace, la última ejecución, la trazabilidad y la programación;
- revisar una bandeja persistente de incidencias operativas evaluadas en modo silencioso;
- mantener ese estado actualizado automáticamente cada 30 segundos, sin ejecutar proveedores ni
  recargar gráficos;
- ver simultáneamente la hora de Lima y Wall Street, junto con el estado de la ventana regular
  09:30–16:00 ET de NYSE;
- cargar automáticamente el último reporte elegible al abrir la página;
- seleccionar desde un combobox único agrupado por clase de activo (Acciones, ETF, Cripto), buscando por ticker o nombre;
- explorar el histórico point-in-time de cada activo con OHLC, VWAP cuando la fuente lo entrega,
  operaciones, tres SMA configurables y volumen;
- usar en BTC-USD el histórico diario persistido de Coinbase Exchange o una ventana intradía local
  de 24 horas con OHLC y volumen en BTC;
- consultar en la misma vista el retorno diario, volatilidad diaria de 20 días con datos, volumen
  relativo de 20 días, distancias a las SMA, extremos, retorno, CAGR y máximo drawdown del rango
  consultado;
- ajustar las ventanas y colores de las tres SMA, con límites explícitos, valores iniciales 5/20/50
  y preferencia local persistente; también permite mostrar u ocultar cada SMA y el volumen;
- alternar el eje de precios entre escala lineal y logarítmica sin volver a consultar el workspace;
- alternar entre línea de cierre y velas OHLC sin repetir la consulta, conservando medias y volumen;
- consultar un año por defecto, ampliar a cinco años al elegir semana y solicitar el histórico
  completo solo al elegir mes; BTC añade intervalos de 1/5/15/30/45 minutos y 1/2/4/5 horas sobre
  las últimas 24 horas;
- ampliar el gráfico alrededor del cursor con la rueda del mouse o con `+` y `-`, y restablecer la
  vista con `0`, sin consultar nuevamente el servicio;
- desplazar horizontalmente la vista ampliada mediante arrastre con el botón izquierdo;
- comparar ocho trimestres o cinco años de ingresos y resultado neto, junto con la ficha de balance
  del último período y los ratios fundamentales seleccionados;
- consultar 40 métricas fundamentales derivadas del último período, agrupadas por crecimiento y
  datos por acción, rentabilidad, retornos y eficiencia, calidad del beneficio, liquidez y balance,
  deuda y solvencia, caja y reinversión y asignación de capital;
- revisar cada métrica una sola vez en ocho áreas, sin duplicarlas por método o nombre de inversor;
- ver inmediatamente el estado de la clasificación empresarial, sus seis categorías posibles y la
  evidencia que aún falta para asignarla de forma explicable;
- comparar cada métrica con el período disponible anterior y mostrar CAGR anual cuando sea válido;
- ver las métricas no disponibles resumidas en un bloque desplegable (disclosure), sin interpretarse como ceros;
- inspeccionar la fórmula, versión, valor exacto y UUID de cada observación usada por esas métricas desde un disclosure dedicado;
- exportar los intervalos de mercado, hechos SEC y métricas derivadas como CSV, además del reporte diagnóstico
  completo como JSON versionado;
- alternar el tema visual mediante un botón de ícono sin texto redundante;
- ampliar o reducir localmente el tramo visible sin descartar datos de la consulta completa;
- ejecutar manualmente el bootstrap completo de SEC EDGAR y Alpaca Market Data IEX;
- ejecutar una actualización exclusivamente de mercado, incremental por los bordes del histórico
  o completa, para BTC-USD o cualquier activo Alpaca visible; si una empresa también declara SEC,
  ambos writers se ejecutan en orden y conservan evidencia independiente;
- importar explícitamente las últimas 24 horas de BTC-USD de un minuto cuando se selecciona una
  resolución intradía;
- consultar el reporte diario point-in-time en modo trimestral o anual;
- seleccionar opcionalmente fechas `as-of` independientes para mercado y fundamentales;
- ver diagnósticos, métricas, frescura, limitaciones y el contrato JSON versionado;
- editar reglas de screening mediante revisiones locales auditadas y restaurar sus valores
  iniciales sin borrar historia;
- ejecutar un replay point-in-time de frecuencia y ruido sobre el activo seleccionado;
- mantener actualizaciones diarias de la watchlist mientras el servicio está activo.

Mercado y fundamentales se muestran en tarjetas separadas. La interfaz no calcula ni muestra un
veredicto, confianza, calidad, recomendación o ranking combinado.

## Estado operativo automático

Las celdas operativas que se actualizan cada 30 segundos consultan
`GET /api/v1/overview`, un snapshot pequeño y no bloqueante. `GET /api/overview` se conserva para
compatibilidad y detalle bajo demanda, pero no participa en el polling periódico. El snapshot
resume estado, frescura, fallos bloqueados, esperas de reintento y la próxima ejecución sin
transportar el historial ni el detalle de los jobs. Solo leen estado local: no
ejecutan SEC, Alpaca ni Coinbase, no escriben en el workspace y no recargan series pesadas. Nunca
hay más de una solicitud en curso; los fallos aplican backoff exponencial hasta cinco minutos y la
consulta se pausa con la pestaña oculta. `Verificar` queda como recuperación manual, no como
requisito de uso. La bandeja carga hasta 50 eventos únicamente al abrirla mediante
`GET /api/alerts`.

## Relojes de mercado

Los relojes se calculan enteramente en el navegador con las zonas IANA `America/Lima` y
`America/New_York`. No realizan consultas de red y una única actualización, alineada al cambio de
minuto, se pausa cuando la pestaña deja de estar visible. Esto permite reflejar automáticamente los
cambios de horario de verano de Nueva York sin fijar una diferencia horaria estática.

La franja compacta del encabezado muestra ambas horas y describe exclusivamente la sesión regular
NYSE publicada de 09:30 a 16:00 ET. Distingue si Nueva York está antes, dentro o después de esa
ventana sin ocupar otra tarjeta del área analítica. No afirma que el mercado esté operando: fines de
semana se identifican, pero los feriados y cierres anticipados todavía no se evalúan. Esta
limitación permanece disponible para tecnologías de asistencia. Un calendario oficial versionado
será un contrato separado antes de convertir el estado horario en un estado operativo de mercado.

## Criterios de presentación

La interfaz está orientada al análisis, no al trading. Mantiene el contexto del activo visible y usa
una distribución compacta: la navegación principal se colapsa en un sidebar sin numerar; la cabecera del activo
muestra un avatar local, ticker, precio, variación diaria, calidad material y clasificación corporativa si existe;
el gráfico, fecha seleccionada y estadísticas técnicas comparten la vista principal; la evolución y ficha
fundamental permanecen en un panel propio. La regla histórica de "cobertura local y número de días siempre visibles"
ha sido reemplazada por visibilidad bajo demanda (disclosure), manteniendo la calidad parcial, retrasos y limitaciones IEX compactas a simple vista. Los paneles ficticios o vacíos no se renderizan si el activo no los soporta. La
navegación, estados, veredictos, calidad, métricas, mensajes y limitaciones se
presentan en español; se conservan nombres técnicos ampliamente usados como AAPL, SEC EDGAR, IEX,
SIP, SMA, JSON y point-in-time cuando aportan precisión.

Los valores financieros exactos no se modifican ni se vuelven a persistir. El navegador aplica
redondeo únicamente para presentación:

- puntuaciones independientes: hasta un decimal;
- cobertura de evidencia: porcentaje con hasta un decimal;
- retornos, volatilidad y ratios porcentuales: porcentaje con hasta dos decimales;
- volumen relativo y pasivos sobre patrimonio: múltiplo con hasta dos decimales;
- medias móviles en USD: dos decimales;
- OHLC y VWAP diarios en USD: dos decimales;
- hechos SEC en la ficha y el gráfico: miles de millones de USD con hasta un decimal;
- importes fundamentales derivados: miles de millones de USD con hasta un decimal;
- márgenes e intensidades fundamentales derivadas: porcentajes con hasta un decimal;
- current ratio y cash ratio: múltiplos con hasta dos decimales;
- deuda, arrendamientos y obligaciones financieras: miles de millones de USD con hasta un decimal;
- cobertura, rotación y deuda frente a patrimonio o FCF: múltiplos con hasta dos decimales;
- EPS, ingresos y flujo de caja por acción: USD por acción con hasta dos decimales;
- acciones promedio y en circulación: miles de millones con hasta dos decimales;
- volumen del gráfico: acciones o participaciones enteras para activos Alpaca y hasta dos decimales
  de BTC; ambos usan notación compacta con un decimal en el resumen;
- operaciones: enteros con separador de miles;
- conteos: enteros con separador de miles.

Los ceros decimales innecesarios se omiten, salvo en importes monetarios. El contrato JSON desplegable
conserva el `Decimal` completo, las unidades, fórmulas, parámetros, identidades y timestamps para
auditoría. El endpoint local `/api/market-chart` entrega `aapl-market-chart-v5` para Apple,
`listed-market-chart-v1` para los demás activos Alpaca y `btc-market-chart-v1` para Bitcoin:
acepta `asset_id`, `interval=auto|1d|1w|1mo`, además de
`short_sma_window`, `long_sma_window` y `third_sma_window`. La interfaz exige ventanas crecientes
entre 2 y 400; el tercer parámetro conserva un valor predeterminado compatible para solicitudes
anteriores,
mantiene los
decimales como cadenas exactas y separa días fuente de puntos diarios, semanales o mensuales.
La interfaz limita la carga inicial a `period=1y`, tanto en modo automático como con intervalo
diario. Al seleccionar una semana amplía la consulta a `period=5y`; el intervalo mensual es la
acción explícita que solicita `period=max` y permite cargar todo el histórico local disponible.
El contrato HTTP conserva los demás rangos compatibles. Esta progresión evita incluir miles de
sesiones diarias de BTC-USD en la respuesta inicial, sin recortar la evidencia persistida ni impedir
el acceso al historial completo.
El endpoint separado `/api/market-intraday` entrega `btc-intraday-chart-v1` únicamente para
`crypto:btc-usd`. Acepta los nueve intervalos fijos, consulta una ventana de 24 horas y excluye el
minuto todavía en curso. No acepta rangos arbitrarios desde el navegador ni reutiliza el contrato
diario. La actualización `POST /api/market-intraday-refresh` se ejecuta solo por una acción explícita,
importa como máximo 1.440 minutos y expone conteos creados/reutilizados para auditar idempotencia.
Para no truncar una vela por el límite del rango, un intervalo semanal o mensual puede incluir un
bloque completo que supere ligeramente el objetivo de días. La vela del calendario vigente sí puede
estar en curso: contiene únicamente la evidencia disponible en `known_at` y se identifica como tal.
Cada punto agregado conserva todos los `raw_record_ids`, inputs de volumen, operaciones y VWAP,
además de las observaciones exactas de apertura, máximo, mínimo y cierre. Apertura y cierre proceden
del primer y último día con datos; máximo y mínimo son los extremos exactos; volumen y operaciones
completas se suman; VWAP solo se publica cuando todos los inputs existen y se pondera por volumen.
La calidad usa de forma conservadora el estado más restrictivo del intervalo: `suspect`, `partial`,
`delayed` y `valid`, en ese orden de precedencia.
Las SMA se recalculan sobre cierres de la resolución visible e incluyen el punto actual. El
gráfico también puede mostrar Bandas de Bollinger descriptivas configurables: la ventana y el
multiplicador exacto viajan en la consulta local, cada punto conserva los IDs de cierres usados y
el dominio vertical incluye sus bandas superior e inferior. La banda plana expone su ausencia de
Percent B sin inventar un valor.
servidor carga el contexto precedente suficiente para que la primera media visible sea exacta; el
contrato expone las ventanas efectivas y cada promedio conserva todos sus UUID de entrada. Las
estadísticas de rango conservan la
fórmula versionada, CAGR cuando existen al menos 365 días entre extremos y los inputs exactos del
máximo drawdown basado en cierres de esa resolución. El último cierre y sus estadísticas diarias se
mantienen separadas. La tabla OHLC se construye solo al abrirla para no cargar miles de nodos DOM.
El endpoint `/api/fundamental-research` entrega el contrato exacto
`aapl-fundamental-research-v2`, incluidas fórmulas, versiones, limitaciones e identidades de inputs.
La matriz compacta presenta el último período; su exportación conserva todos los períodos acotados
devueltos por la consulta. Los endpoints fundamentales aceptan `asset_id`; la interfaz lo envía
siempre y el servidor rechaza activos sin un pipeline fundamental completo antes de invocar los
servicios SEC configurados. La omisión conserva AAPL como valor compatible para clientes locales
anteriores.

El endpoint `/api/fundamental-research-history` envuelve ese contrato sin modificarlo y añade media,
mínimo, máximo, rango, cambio frente al período disponible anterior, cambio del horizonte y CAGR.
Los ratios se comparan mediante diferencias absolutas; no se expresa su cambio relativo. El CAGR
solo se calcula para series anuales de nivel —USD, USD por acción o acciones— con extremos positivos
y utiliza días transcurridos. Las series trimestrales no se anualizan porque los flujos discretos
pueden ser estacionales.

El endpoint `/api/fundamental-analysis` envuelve a su vez el historial exacto y añade ocho secciones
versionadas no solapadas. La cobertura cuenta métricas disponibles en el último período, series
históricas y comparaciones posibles; no es un score. También expone el estado de la clasificación
empresarial y los datos que todavía impiden asignarla. Las flechas y colores distinguen aumentos,
disminuciones y valores sin cambio; indican dirección, no si el cambio es favorable para la empresa.

La estructura usa HTML semántico, un enlace para saltar al contenido, controles táctiles principales
de al menos 44 px,
foco visible, texto adicional al color, diseño adaptable y compatibilidad con reducción de movimiento
y colores forzados. El gráfico SVG se puede recorrer con las flechas, `Inicio` y `Fin`, ampliar con
`+` y `-`, desplazar mediante arrastre y restablecer con `0`; una tabla desplegable expone únicamente los intervalos visibles
OHLC, VWAP, SMA, volumen y operaciones sin depender de la imagen. Estos criterios reducen barreras,
pero no sustituyen una auditoría formal con tecnologías de asistencia.

## Preparación segura

La interfaz hereda las mismas variables que el runner:

```text
ALPACA_API_KEY=...
ALPACA_API_SECRET=...
SEC_USER_AGENT="Investment Analyst nombre@correo.com"
```

El archivo local debe usar únicamente entradas `NAME=value`, sin `export` ni comandos de shell, y
no debe quedar versionado. Protege las credenciales antes de iniciar o instalar el servicio:

```bash
chmod 600 ~/projects/investment-analyst/.env
```

La aplicación no entrega estas variables al navegador, no las persiste en el workspace y no las
incluye en estados, unidades `systemd`, errores o logs HTTP.

## Ejecución interactiva

Desde cualquier directorio, exporta el entorno y ejecuta el script por su ruta absoluta:

```bash
cd ~/projects/investment-analyst
set -a
source .env
set +a
.venv/bin/python scripts/serve_investment_analyst.py
```

La salida esperada incluye:

```text
Investment Analyst available at http://127.0.0.1:8765
```

Abre [http://127.0.0.1:8765](http://127.0.0.1:8765) en el navegador de Windows. `Ctrl+C` detiene el
proceso de forma ordenada.

El servidor se vincula exclusivamente a `127.0.0.1`. Valida el encabezado `Host`, no habilita CORS,
exige JSON para operaciones, limita el cuerpo de cada solicitud y aplica CSP, `no-store`,
`nosniff` y protección contra frames. No existe una opción para exponerlo a la red local; esta
primera versión no tiene autenticación remota.

Las respuestas JSON y los assets de tamaño suficiente se entregan con compresión `gzip` cuando el
navegador la anuncia. Las consultas idénticas de mercado y fundamentales se conservan en cachés
acotadas de memoria y se invalidan después de cada intento de actualización, incluso si ese intento
termina con progreso parcial. La caché no escribe ni sustituye evidencia del workspace.

El tema, tipo de gráfico, intervalo, escala de precio y configuración de ventanas y colores de las
SMA se guardan únicamente en
`localStorage`, con claves versionadas. Las ventanas se envían al servidor en cada consulta para
calcular evidencia exacta; el tipo y los colores nunca salen del navegador. Cambiar solo el tipo,
color o escala reutiliza la respuesta visible y no repite la consulta local. Cambiar el intervalo sí
solicita una nueva agregación exacta al servicio. Ninguna preferencia modifica
datos persistidos y todas pueden restablecerse desde la interfaz o borrando los datos locales del
sitio.

Las exportaciones se construyen en el navegador a partir de la respuesta exacta que ya está visible.
No crean archivos dentro del workspace ni realizan una nueva consulta al proveedor. Los CSV usan
UTF-8, conservan decimales como texto e incluyen identidades de evidencia, versiones de algoritmo,
`known_at` y trazabilidad. Cuando el gráfico está ampliado, su CSV contiene exactamente el tramo
visible; restablecer el zoom vuelve a incluir el rango consultado completo. El JSON conserva
íntegramente el contrato del reporte diario.

## Personalización analítica

Las tres medias móviles ya permiten personalizar ventana, color y visibilidad; el gráfico también
permite elegir escala lineal o logarítmica, línea o velas —con velas como vista predeterminada— e
intervalo diario, semanal o mensual, además de intervalos fijos intradía para BTC. Esta personalización
usa límites tipados, muestra los parámetros efectivos, conserva fórmula, valores exactos y evidencia
y no modifica resultados persistidos ni algoritmos canónicos. Quedan para expansiones posteriores
los parámetros de otras estadísticas y las plantillas reutilizables de indicadores.

## Base intradía

La historia diaria de los activos Alpaca y BTC-USD permanece separada por identidad y fuente. Existe
una fuente paralela de velas BTC-USD de un minuto de Coinbase Exchange, mercado 24/7, con identidad
y disponibilidad point-in-time propias. Sobre esa evidencia se agregan localmente intervalos fijos
UTC de 1, 5, 15, 30 y 45 minutos y de 1, 2, 4 y 5 horas, conservando OHLCV, calidad, completitud y
UUID de entrada.

La interfaz presenta la fuente intradía solo cuando el activo seleccionado es BTC. Cambiar el
intervalo consulta exclusivamente el workspace; la acción de actualización ejecuta primero el flujo
diario y después importa 24 horas de minutos completos. Un fallo de la segunda etapa no elimina el
progreso diario. No se reconstruyen minutos a partir de barras diarias ni se aplican las SMA,
estadísticas o diagnósticos diarios a esta fuente.

## Universo de mercado

El endpoint `/api/market-assets` entrega `market-asset-universe-v4`, generado directamente desde el
catálogo central y las configuraciones tipadas de proveedores. El navegador construye el selector
con esa respuesta; no mantiene otra lista de símbolos. Cada descriptor declara identidad canónica,
símbolo del proveedor, fuente, esquema de gráfico, fecha inicial soportada, unidad de volumen,
capacidad intradía, elegibilidad de derivados cripto, tipo de actualización y un perfil analítico
por familia. La interfaz decide si
un intervalo es intradía a partir de la capacidad del descriptor, no comparando el ID con Bitcoin.

El perfil separa empresas cotizadas, fondos cotizados y criptoactivos. También distingue análisis
corporativo, análisis de fondos y análisis de red/token; por ello una criptomoneda nunca muestra por
accidente fundamentales SEC. El mismo contrato admite futuras acciones de BVL como empresas
cotizadas sin asignarles un proveedor estadounidense.

Apple conserva la actualización completa SEC + Alpaca. Los demás activos Alpaca visibles utilizan
el contrato genérico `listed-market-refresh-v1`, exclusivamente de mercado, y comparten gráfico,
estadísticas y diagnóstico sin una ruta HTTP por símbolo. Su cobertura gratuita es Alpaca IEX:
una sola bolsa y no el mercado consolidado SIP. El catálogo inicial contiene AMD, Barrick (`B`),
BVN, CDE, HYMC, INTC, MSTR, MU, MUX, NEM, PLTR, SCCO, TSM, GBTC, GLD e IBIT, además de AAPL y
BTC-USD.

AMD, Intel, Strategy, Micron, Palantir, CDE, HYMC, MUX, NEM y SCCO declaran una fuente corporativa
SEC US-GAAP. Barrick, BVN ADR y TSM declaran un perfil IFRS anual. El backend web dispone de lectura
y actualización SEC genéricas por `asset_id`:
`/api/fundamental-refresh` ejecuta el writer independiente y tendencia, investigación, historia y
análisis usan cachés aisladas por emisor. Para otro emisor la tendencia publica
`sec-fundamental-trend-v2`; Apple conserva `aapl-fundamental-trend-v1`. La disponibilidad visual se
deriva de las capacidades y vinculaciones SEC completas del catálogo, no de una lista adicional de
símbolos en la interfaz o el backend.

El smoke real de AMD del 28 de julio de 2026 procesó dos documentos oficiales, 883 observaciones y
23 métricas persistidas del diagnóstico base. La repetición reutilizó exactamente los dos raw, las
883 observaciones, las 23 métricas y el diagnóstico, sin crear duplicados. La investigación ampliada
obtuvo 25 de 40 métricas en el período más reciente y 26 series históricas.

El smoke real anual de Intel del mismo día procesó dos documentos oficiales, 687 observaciones y 19
métricas base. MSTR, MU y PLTR produjeron respectivamente 477, 873 y 559 observaciones y 27, 39 y 28
métricas base. Las repeticiones reutilizaron toda la evidencia sin crear duplicados. Estos emisores
publican `has_fundamentals=true` y `refresh_kind=market_only`: la interfaz muestra su análisis
corporativo y, al actualizar, ejecuta primero su mercado Alpaca y después SEC como dos operaciones
independientes. No utiliza el bootstrap ni la consulta diagnóstica consolidada de Apple. Los
conceptos ausentes permanecen visibles como faltantes; no se rellenan con cero ni con datos de otro
emisor.

CDE, HYMC, MUX, NEM y SCCO produjeron en sus smokes reales 873, 633, 845, 569 y 1224 observaciones.
La cobertura diagnóstica incompleta se muestra como tal y no impide consultar la evidencia
disponible. Los smokes IFRS reales del 29 de julio de 2026 produjeron 166 observaciones y 43 métricas
base para Barrick, 215 y 38 para BVN, y 149 y 38 para TSM. Cada repetición reutilizó dos snapshots,
todas las observaciones, métricas y el diagnóstico. La interfaz ofrece solo frecuencia anual y el
servidor rechaza trimestral antes de invocar una lectura o un proveedor.

## Integración actual de cripto

El descriptor `crypto:btc-usd` usa únicamente velas diarias públicas de Coinbase Exchange:
representa un solo mercado, no un precio agregado de todo el ecosistema. La interfaz oculta
fundamentos SEC y clasificaciones empresariales al seleccionar Bitcoin; no reutiliza identidades de
AAPL ni interpreta sesiones bursátiles en un mercado 24/7.

La sección Operación cambia al flujo BTC-USD y ofrece actualización incremental automática o rango
completo. El rango público es inclusivo y solo admite días UTC terminados. El plan automático detecta
únicamente prefijos y sufijos fuera de las velas persistidas; no inventa huecos internos. Después de
importar conserva `RawRecord`, observaciones, métricas y diagnóstico de mercado independientes y
devuelve `btc-market-refresh-v1` con conteos, corte efectivo y trazabilidad. El scheduler crea para
Bitcoin trabajos separados de mercado diario e intradía; ninguno inventa fundamentales
corporativos ni reutiliza identidades de acciones.

Todo el OHLCV solicitado se conserva para la gráfica y los cálculos de consulta. La actualización
operativa persiste estadísticas únicamente sobre los 90 días calendario finales del rango: cubre
holgadamente las ventanas técnicas actuales de 20 días y evita recalcular once años en cada corte
diario. El contrato declara `analytics_start`, `analytics_end` y `analytics_lookback_days`; esta
optimización no recorta los datos históricos guardados ni la vista de máximo histórico.

## Derivados cripto en la interfaz

Para un descriptor con `supports_crypto_derivatives=true`, la navegación muestra un panel separado
de la gráfica spot. El panel es lazy: al abrirlo consulta `GET /api/v1/crypto-derivatives` con
`asset_id`, `start`, `end` y `known_at`; los límites son fechas UTC inclusivas de una ventana de 90
días terminada en la fecha UTC del corte visible. El endpoint rechaza parámetros repetidos,
desconocidos, fechas inválidas, cortes sin zona y activos no elegibles antes de abrir storage.

La respuesta se conserva como `crypto-derivatives-query-result-v1`. La presentación muestra estado
descriptivo, dirección de funding y DVOL, funding acumulado 168 h, cambio DVOL 7 d, interés abierto,
funding actual/8 h, spread, cobertura, corte, fuentes, requisitos faltantes, limitaciones e
identidades. No infiere calidad, señal, recomendación, ranking ni mezcla esos datos con spot,
fundamentales o valoración.

## Programación de la watchlist

La configuración predeterminada deriva trabajos para todos los activos visibles del catálogo. El
mercado diario Alpaca y Coinbase se evalúa a las `07:00` de `America/Lima`, los fundamentales SEC
a las `07:15` y Coinbase intradía a las `07:30`. El inicio de mercado predeterminado es
`2025-01-01`; el fin diario es el día calendario anterior:

```text
fecha local de ejecución - 1 día calendario
```

Este cálculo es explícitamente calendario; no pretende identificar sesiones, feriados o cierres de
bolsa. El planificador de mercado existente sigue detectando únicamente bordes faltantes.

Cada job declara activo, proveedor, dominio, frecuencia y zona horaria. Si el proceso inicia después
de su hora y todavía no terminó el job de esa fecha local, lo evalúa inmediatamente. Persiste el
estado `running` antes de invocar el conector, conserva los trabajos anteriores aunque uno posterior
falle y admite hasta tres intentos diarios con backoff de 15 minutos únicamente para fallos
transitorios. Tras reiniciar, transforma un intento interrumpido en un fallo trazable antes de
reintentarlo. Las operaciones del controlador continúan compartiendo un único mutex writer; no se
ejecutan dos escrituras simultáneas.

### Riesgo operativo separado: contención del mutex writer

El mutex compartido protege la escritura única y evita ejecuciones concurrentes sobre el workspace,
pero una cola de trabajos vencidos puede mantenerlo ocupado durante varios proveedores consecutivos.
El health usa ahora un snapshot inmutable y las cachés otro lock, por lo que el overview no espera el
tiempo completo del proveedor. La contención sigue retrasando otras escrituras; permanece como riesgo
operativo separado y no debe resolverse introduciendo otro writer.

La decisión de reintento usa excepciones tipadas, su cadena causal y el estado HTTP estructurado; no
interpreta texto libre. Timeout, conexión interrumpida, `408`, `429` y los estados transitorios
`500`, `502`, `503` y `504` conservan backoff y presupuesto. Configuración o credenciales inválidas,
`401`/`403`, activo o capacidad no soportada, payload incompatible, validación point-in-time,
almacenamiento o estado incompatible, otros estados HTTP e imprevistos terminan sin otro intento de
proveedor ese día. El error persistido usa una categoría estable y un mensaje fijo de hasta 500
caracteres, sin URL, headers, traceback ni texto arbitrario que pudiera contener secretos.

Una cobertura FRED incompleta se conserva como ejecución exitosa con `coverage_complete=false`, no
como éxito completo ni como fallo reintentable. Si falla el observador de alertas, el scheduler
reintenta solo esa notificación local y no vuelve a ejecutar el proveedor.

Por defecto se programa toda la watchlist soportada. Para restringirla, repite `--schedule-asset`;
por ejemplo:

```bash
.venv/bin/python scripts/serve_investment_analyst.py \
  --schedule-asset equity:us:amd \
  --schedule-asset crypto:btc-usd
```

Estas opciones son una semilla compatible solo cuando no existe
`state/asset_preferences_state_v1.json`; arrancar no crea ese archivo. La primera edición explícita
desde la interfaz o `PUT /api/v1/asset-preferences` crea una revisión y, desde entonces, el estado
persistido tiene precedencia sobre `--schedule-asset`. `GET /api/v1/asset-preferences` devuelve el
fingerprint y la revisión esperados por PUT. Una edición concurrente responde `409` y no sobrescribe
la selección más reciente. Consulta [`asset_preferences.md`](asset_preferences.md).

Al guardar, el registro se reconstruye desde catálogo y capacidades y se publica atómicamente. No
se llama a proveedores, no se abre otro writer y no se cancela el trabajo ya activo. Retirar y
reactivar conserva `job_id`, intentos e historia. Si el tick ya había capturado otros jobs retirados
pero aún no iniciados, los omite. Un activo nuevo del catálogo no entra solo en una watchlist ya
persistida.

Usa `--no-schedule-intraday` si deseas conservar Bitcoin diario sin su ventana automática de un
minuto. El registro SMV se programa por defecto y puede desactivarse con `--no-schedule-smv`.
Si el `.env` contiene una `FRED_API_KEY` válida, también se registran seis trabajos macro
mensuales/trimestrales; `--no-schedule-macro` los desactiva sin afectar el resto. El instalador
`scripts/install_local_service.py` acepta las mismas opciones y las conserva como argumentos
explícitos en la unidad revisable.

Personaliza el horario y el rango al iniciar:

```bash
.venv/bin/python scripts/serve_investment_analyst.py \
  --schedule-at 07:30 \
  --timezone America/Lima \
  --market-start 2025-01-01 \
  --market-end-lag-days 1 \
  --fundamental-frequency quarterly
```

Desactiva únicamente el scheduler, conservando la UI y la ejecución manual:

```bash
.venv/bin/python scripts/serve_investment_analyst.py --no-scheduler
```

La opción también conserva `scheduled_refresh=true` en las preferencias, pero publica
`effective_scheduled_refresh=false`, cero activos programados efectivos y cero jobs en API,
overview e interfaz. Con scheduler deshabilitado puede guardarse una selección vacía; al habilitarlo
se exige al menos un activo disponible programado.

El lock `state/aapl_local_service.lock` impide dos servicios para el mismo workspace. El estado de
todos los intentos se guarda atómicamente en `state/multi_asset_schedule_state_v1.json`; no
reemplaza ni recorta el historial analítico.

Al recibir `SIGTERM` o `SIGINT`, el scheduler no inicia nuevos jobs y el job activo observa un
control cooperativo no persistido en los límites seguros de red, páginas y unidades completas de
evidencia. El intento conserva su identidad y termina como `failed` con categoría
`interrupted_job`; el proceso espera el cierre cooperativo del scheduler y falla explícitamente si
el hilo sigue vivo al alcanzar el deadline de apagado. Las operaciones manuales conservan su cola
y su estado independiente.

Las operaciones manuales nuevas pueden usar `POST /api/v1/manual-operations` y consultar su estado
con `GET /api/v1/manual-operations/<operation_id>`. La respuesta de enqueue es `202`; el worker
durable ejecuta después la fachada síncrona compatible. Solicitudes activas equivalentes se
deduplican y un estado `running` encontrado al reiniciar se reencola con recovery auditado.

## Alertas operativas silenciosas

Después de cada intento completado, el monitor evalúa cuatro reglas versionadas: actualización
fallida, interrumpida, omitida o con cobertura incompleta. Cada condición produce `met`, `not_met`
o `not_evaluable`; una ausencia nunca se convierte en cero. La identidad depende de regla, versión
e intento, por lo que repetir el replay no duplica resultados ni eventos.

La tarjeta de programación resume trabajos fallidos, con cobertura incompleta o desactualizados.
Cada job conserva el último intento y el último chequeo exitoso por separado, de modo que un fallo
nuevo no borra la referencia de frescura anterior. En la bandeja se puede marcar una alerta como
vista, descartada o resuelta. Cada cambio crea una transición append-only con hora, estado anterior
y estado nuevo; repetir el mismo estado es idempotente. Un intento automático posterior que termine
correctamente y con cobertura completa resuelve también, con actor `system_recovery`, las alertas
anteriores del mismo job. Esta recuperación no elimina la evidencia histórica ni permite que un
éxito parcial o de otro job cierre una incidencia.

La primera entrega no evalúa oportunidades financieras ni envía Telegram, correo o notificaciones
del sistema. La bandeja local recomienda revisar la evidencia operativa; no recomienda comprar o
vender. El monitor trabaja con el intento ya persistido: no carga gráficos, no abre el frontend, no
consulta proveedores y funciona con el navegador cerrado.

## Reglas y replay analítico

El panel **Reglas de screening** carga bajo demanda el registro local. Permite cambiar el estado,
los umbrales de entrada y salida, las confirmaciones y la espera entre candidatos. El servidor
valida el contrato completo, rechaza floats para valores financieros, usa el fingerprint mostrado
como lock optimista y crea una revisión append-only solo si el contenido cambió. Restaurar los
valores iniciales crea otra revisión; no elimina el historial.

El botón **Replay** usa el activo seleccionado y hasta 200 cortes point-in-time persistidos. Es una
consulta de solo lectura: muestra cortes, coincidencias, candidatos simulados y condiciones no
evaluables. No consulta proveedores, no modifica resultados operativos y no estima retornos ni
precisión predictiva. Una regla modificada se utiliza automáticamente en el siguiente intento con
evidencia nueva; los intentos ya recibidos permanecen intactos.

El panel **Notificaciones locales** consulta `GET /api/v1/candidate-notifications` bajo demanda.
Muestra sólo el identificador del candidato, regla, activo y hora de la recepción local; no carga
condiciones completas ni evidencia financiera. `POST /api/v1/candidate-notifications/acknowledge`
acepta exclusivamente `notification_id`, registra como máximo un acuse append-only y es idempotente.
El acuse no cambia el estado del candidato y no envía notificaciones del navegador, sistema o red.

## Servicio persistente con systemd

El instalador genera una unidad privada y revisable. No ejecuta `systemctl`, no utiliza `sudo` y no
inicia procesos por sí mismo:

```bash
cd ~/projects/investment-analyst
.venv/bin/python scripts/install_local_service.py
```

La salida indica la ruta `~/.config/systemd/user/investment-analyst.service` y los comandos
siguientes. Revisa primero la unidad sin mostrar credenciales:

```bash
systemd-analyze --user verify ~/.config/systemd/user/investment-analyst.service
systemctl --user daemon-reload
systemctl --user enable --now investment-analyst.service
systemctl --user status investment-analyst.service
```

La unidad usa el `.venv`, repositorio y workspace absolutos, `UMask=0077`, `NoNewPrivileges`, un
directorio temporal privado y reinicio tras fallos. El `.env` se referencia como `EnvironmentFile`;
sus valores nunca se copian al archivo de unidad. Para mantener un renderizado inequívoco de
`systemd`, estas rutas absolutas no pueden contener espacios.

Comandos de operación:

```bash
journalctl --user -u investment-analyst.service -f
systemctl --user restart investment-analyst.service
systemctl --user disable --now investment-analyst.service
```

En WSL, la unidad depende de que `systemd` y el administrador de usuario estén disponibles. Tras un
reinicio completo de Windows puede ser necesario iniciar la distribución WSL para que su entorno
de usuario vuelva a ejecutarse. Esta versión no crea una tarea en Windows Task Scheduler.

## Despliegue desacoplado por releases independientes

Para operar el servicio permanente de forma desacoplada de cualquier checkout de desarrollo,
el gestor de releases (`scripts/deploy_local_release.py`) materializa releases inmutables del SHA
integrado vivo de `origin/main` en una ubicación independiente del sistema:

- Raíz de runtime: `~/.local/share/investment-analyst/runtime/`
- Mirror/cache Git bare: `~/.local/share/investment-analyst/runtime/mirror.git`
- Releases inmutables: `~/.local/share/investment-analyst/runtime/releases/<full-sha>/`
- Manifest determinista: `~/.local/share/investment-analyst/runtime/releases/<full-sha>/manifest.json`
- Estado de despliegues: `~/.local/share/investment-analyst/runtime/deployment_state.json`
- Configuración privada: `~/.config/investment-analyst/service.env` (permisos 0600)

Cada release contiene un entorno virtual no editable construido estrictamente desde `uv.lock` con
Python 3.12 y `uv==0.11.29`, sin dependencias de desarrollo y sin referencias a ningún worktree ni
`.venv` local.

Antes de publicar una release nueva, y también al aceptar una release preexistente, `stage` ejecuta
un sondeo hermético desde el Python instalado de esa release, con el checkout y `PYTHONPATH`
neutralizados. El sondeo comprueba que el import proviene de `site-packages`, que están empaquetados
el catálogo JSON, la migración SQL y los tres estáticos frontend, y que
`AssetCatalogService.load_default()` y `ApplicationRuntime.create_default()` pueden inicializar un
workspace/storage temporal. Si falla, la release nueva no se publica y una release preexistente no
se repara ni se sobrescribe.
Pasar este probe y estar integrado en `main` no equivale por sí solo a aceptación operacional:
el cutover humano posterior debe validar el host, systemd, endpoints y estabilidad del runtime.

### Sondeo de readiness acotado

`activate`, `update`, `bootstrap` y `rollback` verifican la salud del servicio HTTP mediante sondeo
de readiness acotado. El sondeo reintenta ante `ConnectionRefusedError`, `URLError`, `TimeoutError`
y estados HTTP distintos de 200 hasta que todos los endpoints requeridos respondan 200 o expire un
deadline configurable (120 s por defecto, con intervalo de 0,25 s). Cada probe limita su timeout al
tiempo restante del deadline total. Un timeout real sigue fallando cerrado.

El sondeo se aplica simétricamente al arranque de la nueva release y al reinicio de la release
previa durante un recovery rollback automático. `status()` permanece como consulta puntual sin
sondeo. Cuando no existe release previa (`previous is None`), un fallo doble de readiness reporta
que el candidato fue detenido, la unidad legacy unmanaged se restauró sólo como configuración y el
servicio quedó inactivo; no intenta reiniciar el legacy ni hace rollback a `None`.

### Comandos de operación del release runtime

Bootstrap inicial (adopta credenciales, materializa release, retargetea unidad y activa):

```bash
python3 scripts/deploy_local_release.py bootstrap \
  --sha <full-sha-integrado> \
  --env-source ~/projects/investment-analyst/.env \
  --readiness-deadline-seconds 120
```

Consulta de estado operativo e inspección del SHA desplegado:

```bash
python3 scripts/deploy_local_release.py status
# O en formato JSON estructurado:
python3 scripts/deploy_local_release.py status --json
```

Actualización a un nuevo SHA integrado de `main` (descarga, construye venv, retargetea, reinicia y verifica health):

```bash
python3 scripts/deploy_local_release.py update \
  --sha <nuevo-full-sha> \
  --readiness-deadline-seconds 120
```

Rollback al despliegue anterior verificado (restaura unidad anterior, reinicia y verifica health):

```bash
python3 scripts/deploy_local_release.py rollback --readiness-deadline-seconds 120
```


Comandos paso a paso equivalentes:

```bash
# 1. Adoptar configuración privada
python3 scripts/deploy_local_release.py adopt-env --source ~/.env

# 2. Materializar y verificar release inmutable
python3 scripts/deploy_local_release.py stage --sha <full-sha>

# 3. Retargetear la unidad systemd
python3 scripts/deploy_local_release.py retarget-unit --sha <full-sha>

# 4. Activar release con reinicio y comprobación de salud
python3 scripts/deploy_local_release.py activate \
  --sha <full-sha> \
  --readiness-deadline-seconds 120
```

`activate`, `update`, `bootstrap` y `rollback` aceptan un deadline total estricto entre 1 y 600
segundos mediante `--readiness-deadline-seconds`. Un valor inválido falla antes de retargetear o
reiniciar. En el primer adoption, si restart o readiness fallan, el candidato se detiene, la
unidad legacy unmanaged se restaura sólo como configuración, se ejecuta `daemon-reload` y se
confirma que el servicio queda inactivo; nunca se reinicia, habilita ni activa esa unidad legacy.

### Inspección del SHA activo en systemd

Para confirmar que el servicio activo ejecuta exclusivamente la release desacoplada y no el checkout:

```bash
systemctl --user show investment-analyst.service -p WorkingDirectory -p ExecStart -p EnvironmentFile
```

La salida esperada muestra el SHA completo en las rutas:

```text
WorkingDirectory=/home/marjuraru/.local/share/investment-analyst/runtime/releases/<full-sha>
EnvironmentFile=/home/marjuraru/.config/investment-analyst/service.env
ExecStart={ path=/home/marjuraru/.local/share/investment-analyst/runtime/releases/<full-sha>/.venv/bin/python ; argv[]=/home/marjuraru/.local/share/investment-analyst/runtime/releases/<full-sha>/.venv/bin/python ... }
```

## Sonda de readiness operacional

La sonda operacional es una consulta local independiente del health HTTP. No inicia el scheduler,
no ejecuta providers o refresh, no reconcilia observers y no crea archivos, locks ni timestamps:

```bash
.venv/bin/python scripts/check_operational_readiness.py \
  --workspace <workspace-inicializado> \
  --since 2026-08-01T00:00:00Z \
  --min-local-dates 3
```

Devuelve exit `0` con `PASS`, `3` con un `NOT_READY` válido y reason codes acotados, o `2` cuando
el input/estado es inválido, falta evidencia requerida o el snapshot cambia durante la lectura.
El reporte no imprime rutas del workspace, payloads, secretos ni mensajes de proveedor. El archivo
opcional de operaciones manuales ausente se representa como `present=false`; scheduler, alertas
operativas y receipts analíticos ausentes fallan cerrado. Véase el
[runbook de readiness y recuperación](operational_readiness.md).

## Archivos operativos

Todos permanecen dentro del workspace seleccionado:

- `state/aapl_daily_run.lock`: exclusión de una ejecución analítica;
- `state/aapl_daily_run_state.json`: última ejecución completa Apple iniciada manualmente;
- `state/aapl_local_service.lock`: exclusión del proceso UI/scheduler;
- `state/multi_asset_schedule_state_v1.json`: historial de intentos por job y su evidencia compacta;
- `state/manual_operation_state_v1.json`: cola manual durable, resultados compactos y recovery;
- `state/asset_preferences_state_v1.json`: revisiones de watchlist, favoritos y refresh programado
  con fingerprint y control optimista;
- `state/asset_preferences_state_v1_archives/`: segmentos históricos inmutables y hash-bound
  creados antes de los límites de 1 000 revisiones o 4 MiB;
- `state/operational_alert_state_v1.json`: resultados trivaluados y eventos deduplicados de la
  bandeja local;
- `state/analytical_screening_state_v1.json`: resultados, recibos, candidatos y transiciones
  analíticas append-only;
- `state/candidate_notification_outbox_state_v1.json`: recepciones locales inmutables y acuses
  append-only, independientes del ciclo de vida del candidato;
- `state/analytical_rule_registry_state_v1.json`: revisiones locales completas de reglas con
  fingerprints encadenados.

Los archivos de estado son contratos operativos versionados y privados. La evidencia financiera
append-only continúa en el almacenamiento normal del workspace; las alertas no la sustituyen ni
modifican.

## Panel de valoración

El panel «Valoración» aparece solo cuando el descriptor del catálogo declara empresa corporativa,
mercado/fundamentales y base de unidad del título. Se carga bajo demanda mediante
`GET /api/v1/valuation`; muestra precio y edad, ejercicio/filing, moneda/factor, estados por métrica,
fórmula e IDs de inputs. La exportación JSON conserva los textos Decimal exactos. ETF y cripto no
reciben ratios corporativos; la API puede devolver `not_applicable` sin abrir writer ni proveedor.

La historia materializada usa por separado `GET /api/v1/valuation-history` con corte UTC, rango
inclusivo, base `latest_annual` y límite explícito. Sólo se solicita al pulsar «Cargar historia»:
el panel ofrece selector de serie, resumen Decimal, tabla accesible y exportación del JSON exacto.
No amplía `/api/overview`, no llama proveedores, no toma writer y no convierte fechas sin artefacto
persistido en cero o backfill.

La regla histórica se solicita también bajo demanda. El usuario define métrica, operador, umbral
Decimal y mínimo previo; el panel muestra estado trivaluado, fórmula de rango medio, cobertura,
conteos, evidencia expandible y exportación JSON exacta. Describe contexto configurado y nunca
"barato", "caro", compra, venta o recomendación.
