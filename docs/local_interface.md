# Interfaz local y operación continua

La interfaz local convierte los flujos existentes de mercado, fundamentales y BTC-USD en una
herramienta básica utilizable desde el navegador. Un scheduler derivado del catálogo puede
actualizar la watchlist por trabajos independientes. No añade scores combinados, recomendaciones,
Trading API ni un LLM activo.

## Capacidades

La página permite:

- revisar el workspace, la última ejecución, la trazabilidad y la programación;
- revisar una bandeja persistente de incidencias operativas evaluadas en modo silencioso;
- mantener ese estado actualizado automáticamente cada 30 segundos, sin ejecutar proveedores ni
  recargar gráficos;
- ver simultáneamente la hora de Lima y Wall Street, junto con el estado de la ventana regular
  09:30–16:00 ET de NYSE;
- cargar automáticamente el último reporte elegible al abrir la página;
- seleccionar desde el catálogo central AAPL, BTC-USD y una lista inicial de acciones y ETF
  estadounidenses sin mantener símbolos duplicados en la interfaz;
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
- inspeccionar la fórmula, versión, valor exacto y UUID de cada observación usada por esas métricas;
- exportar los intervalos de mercado, hechos SEC y métricas derivadas como CSV, además del reporte diagnóstico
  completo como JSON versionado;
- alternar entre un tema oscuro de baja luminancia, predeterminado, y el tema claro;
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

Las cinco celdas compactas de datos, ejecución, programación, trazabilidad y alertas se consultan
mediante `GET /api/overview` al abrir la página y cada 30 segundos. Solo leen estado local: no
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
una distribución compacta: gráfico, fecha seleccionada y estadísticas técnicas comparten la vista
principal; la evolución y ficha fundamental permanecen en un panel propio para no mezclar su
significado con mercado. La actualización de fuentes permanece en una sección operativa secundaria. La
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

El endpoint `/api/market-assets` entrega `market-asset-universe-v3`, generado directamente desde el
catálogo central y las configuraciones tipadas de proveedores. El navegador construye el selector
con esa respuesta; no mantiene otra lista de símbolos. Cada descriptor declara identidad canónica,
símbolo del proveedor, fuente, esquema de gráfico, fecha inicial soportada, unidad de volumen,
capacidad intradía, tipo de actualización y un perfil analítico por familia. La interfaz decide si
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

El lock `state/aapl_local_service.lock` impide dos servicios para el mismo workspace. El estado de
todos los intentos se guarda atómicamente en `state/multi_asset_schedule_state_v1.json`; no
reemplaza ni recorta el historial analítico.

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

## Archivos operativos

Todos permanecen dentro del workspace seleccionado:

- `state/aapl_daily_run.lock`: exclusión de una ejecución analítica;
- `state/aapl_daily_run_state.json`: última ejecución completa Apple iniciada manualmente;
- `state/aapl_local_service.lock`: exclusión del proceso UI/scheduler;
- `state/multi_asset_schedule_state_v1.json`: historial de intentos por job y su evidencia compacta;
- `state/operational_alert_state_v1.json`: resultados trivaluados y eventos deduplicados de la
  bandeja local;
- `state/analytical_screening_state_v1.json`: resultados, recibos, candidatos y transiciones
  analíticas append-only;
- `state/analytical_rule_registry_state_v1.json`: revisiones locales completas de reglas con
  fingerprints encadenados.

Los archivos de estado son contratos operativos versionados y privados. La evidencia financiera
append-only continúa en el almacenamiento normal del workspace; las alertas no la sustituyen ni
modifican.
