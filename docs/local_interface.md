# Interfaz local y operación continua

La interfaz local convierte los flujos existentes de Apple en una herramienta básica utilizable
desde el navegador. El mismo proceso puede ejecutar una programación diaria. No añade fórmulas,
scores combinados, recomendaciones, Trading API ni un LLM activo.

## Capacidades

La página permite:

- revisar el workspace, la última ejecución, la trazabilidad y la programación;
- cargar automáticamente el último reporte elegible al abrir la página;
- explorar el histórico point-in-time de AAPL con OHLC, VWAP, operaciones, tres SMA configurables y
  volumen;
- consultar en la misma vista el retorno diario, volatilidad diaria de 20 días con datos, volumen
  relativo de 20 días, distancias a las SMA, extremos, retorno, CAGR y máximo drawdown del rango
  consultado;
- ajustar las ventanas y colores de las tres SMA, con límites explícitos, valores iniciales 5/20/50
  y preferencia local persistente; también permite mostrar u ocultar cada SMA y el volumen;
- alternar el eje de precios entre escala lineal y logarítmica sin volver a consultar el workspace;
- alternar entre línea de cierre y velas OHLC sin repetir la consulta, conservando medias y volumen;
- consultar siempre todo el histórico local y cambiar únicamente el intervalo de cada punto o vela:
  automático, 1 día, 1 semana o 1 mes;
- ampliar el gráfico alrededor del cursor con la rueda del mouse o con `+` y `-`, y restablecer la
  vista con `0` o el control visible, sin consultar nuevamente el servicio;
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
- consultar el reporte diario point-in-time en modo trimestral o anual;
- seleccionar opcionalmente fechas `as-of` independientes para mercado y fundamentales;
- ver diagnósticos, métricas, frescura, limitaciones y el contrato JSON versionado;
- mantener una ejecución diaria mientras el servicio está activo.

Mercado y fundamentales se muestran en tarjetas separadas. La interfaz no calcula ni muestra un
veredicto, confianza, calidad, recomendación o ranking combinado.

## Criterios de presentación

La interfaz está orientada al análisis, no al trading. Mantiene el contexto de Apple visible y usa
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
- volumen del gráfico: entero en el detalle y notación compacta con un decimal en el resumen;
- operaciones: enteros con separador de miles;
- conteos: enteros con separador de miles.

Los ceros decimales innecesarios se omiten, salvo en importes monetarios. El contrato JSON desplegable
conserva el `Decimal` completo, las unidades, fórmulas, parámetros, identidades y timestamps para
auditoría. El endpoint local `/api/market-chart` entrega `aapl-market-chart-v5`: acepta
`interval=auto|1d|1w|1mo`, además de
`short_sma_window`, `long_sma_window` y `third_sma_window`. La interfaz exige ventanas crecientes
entre 2 y 400; el tercer parámetro conserva un valor predeterminado compatible para solicitudes
anteriores,
mantiene los
decimales como cadenas exactas y separa días fuente de puntos diarios, semanales o mensuales.
La interfaz solicita siempre `period=max`; los demás rangos permanecen únicamente en el contrato
HTTP por compatibilidad con clientes existentes. En modo automático, el historial completo se
presenta por meses; un intervalo explícito se calcula sobre toda la evidencia local disponible.
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
devueltos por la consulta.

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
permite elegir escala lineal o logarítmica, línea o velas e intervalo diario, semanal o mensual. Esta personalización
usa límites tipados, muestra los parámetros efectivos, conserva fórmula, valores exactos y evidencia
y no modifica resultados persistidos ni algoritmos canónicos. Quedan para expansiones posteriores
los parámetros de otras estadísticas y las plantillas reutilizables de indicadores.

## Intervalos intradía previstos

El workspace canónico actual conserva barras diarias de Alpaca Market Data IEX. Por ello, los
intervalos de 1, 5, 15, 30 y 45 minutos y de 1, 2, 4 y 5 horas no se muestran todavía: una barra
diaria no contiene evidencia suficiente para reconstruirlos. La expansión intradía deberá ingerir
barras base de un minuto con una identidad de fuente nueva, zona horaria de NASDAQ, límites de sesión
y disponibilidad point-in-time explícitos. A partir de esa base podrán agregarse de forma exacta los
intervalos mayores sobre todo el historial disponible, conservando OHLCV, cobertura y UUID de entrada
sin alterar la historia diaria.

## Expansión a cripto prevista

El catálogo y el pipeline histórico de Coinbase ya reconocen `crypto:btc-usd`, pero la interfaz y el
runner operativo actuales están acotados a Apple. La integración visual de cripto debe introducir un
selector de activo y un modo de análisis exclusivamente de mercado: no debe simular fundamentales
SEC, reutilizar identidades de AAPL ni interpretar sesiones bursátiles para un mercado continuo 24/7.
Esa ampliación se realizará después de estabilizar la estación analítica de Apple y antes del cierre
de personalización general.

## Programación diaria

La configuración predeterminada evalúa una ejecución a las `07:00` de `America/Lima`, con inicio
de mercado `2025-01-01`, frecuencia fundamental trimestral y fin de mercado igual al día calendario
anterior:

```text
fecha local de ejecución - 1 día calendario
```

Este cálculo es explícitamente calendario; no pretende identificar sesiones, feriados o cierres de
bolsa. El planificador de mercado existente sigue detectando únicamente bordes faltantes.

Si el proceso inicia después de la hora configurada y aún no existe un intento para esa fecha local,
ejecuta una recuperación inmediata. Persiste el intento antes de llamar al runner y no lo repite en
la misma fecha, incluso si falla o el proceso se reinicia. Un intento interrumpido se muestra como
incidencia y puede recuperarse manualmente desde la interfaz. Al día siguiente vuelve a intentar.

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

El lock `state/aapl_local_service.lock` impide dos servicios para el mismo workspace. El lock
operativo independiente continúa impidiendo dos writers. El estado más reciente del scheduler se
guarda atómicamente en `state/aapl_daily_schedule_state.json`; no reemplaza el historial analítico.

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
- `state/aapl_daily_run_state.json`: última ejecución manual o programada;
- `state/aapl_local_service.lock`: exclusión del proceso UI/scheduler;
- `state/aapl_daily_schedule_state.json`: último intento de la programación.

Los archivos de estado son resúmenes operativos acotados. El historial, las identidades
deterministas y la evidencia append-only continúan en el almacenamiento normal del workspace.
