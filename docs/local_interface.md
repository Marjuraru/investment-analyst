# Interfaz local y operación continua

La interfaz local convierte los flujos existentes de Apple en una herramienta básica utilizable
desde el navegador. El mismo proceso puede ejecutar una programación diaria. No añade fórmulas,
scores combinados, recomendaciones, Trading API ni un LLM activo.

## Capacidades

La página permite:

- revisar el workspace, la última ejecución, la trazabilidad y la programación;
- cargar automáticamente el último reporte elegible al abrir la página;
- explorar el histórico point-in-time de AAPL con OHLC, VWAP, operaciones, SMA 5, SMA 20 y volumen;
- consultar en la misma vista el retorno diario, volatilidad diaria de 20 sesiones, volumen relativo
  de 20 sesiones, distancias a las SMA, extremos, retorno, CAGR y máximo drawdown del rango visible;
- mostrar u ocultar SMA 5, SMA 20 y volumen sin volver a consultar el almacenamiento;
- comparar ocho trimestres o cinco años de ingresos y resultado neto, junto con la ficha de balance
  del último período y los ratios fundamentales seleccionados;
- consultar 18 métricas fundamentales derivadas del último período, agrupadas por rentabilidad,
  calidad de beneficios, caja y reinversión, liquidez y distribución al accionista;
- comparar cada métrica con el período disponible anterior y mostrar CAGR anual cuando sea válido;
- inspeccionar la fórmula, versión, valor exacto y UUID de cada observación usada por esas métricas;
- exportar los intervalos de mercado, hechos SEC y métricas derivadas como CSV, además del reporte diagnóstico
  completo como JSON versionado;
- alternar entre un tema oscuro de baja luminancia, predeterminado, y el tema claro;
- cambiar entre 22, 66, 132, 260, 520 y hasta 1.300 sesiones fuente, además de todo el histórico
  local, mediante las vistas 1M, 3M, 6M, 1A, 2A, 5A y Máx.; hasta 2A se muestran puntos diarios,
  5A usa semanas ISO completas y Máx. meses calendario UTC;
- ejecutar manualmente el bootstrap completo de SEC EDGAR y Alpaca Market Data IEX;
- consultar el reporte diario point-in-time en modo trimestral o anual;
- seleccionar opcionalmente fechas `as-of` independientes para mercado y fundamentales;
- ver diagnósticos, métricas, frescura, limitaciones y el contrato JSON versionado;
- mantener una ejecución diaria mientras el servicio está activo.

Mercado y fundamentales se muestran en tarjetas separadas. La interfaz no calcula ni muestra un
veredicto, confianza, calidad, recomendación o ranking combinado.

## Criterios de presentación

La interfaz está orientada al análisis, no al trading. Mantiene el contexto de Apple visible y usa
una distribución compacta: gráfico, sesión seleccionada y estadísticas técnicas comparten la vista
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
- volumen del gráfico: entero en el detalle y notación compacta con un decimal en el resumen;
- operaciones: enteros con separador de miles;
- conteos: enteros con separador de miles.

Los ceros decimales innecesarios se omiten, salvo en importes monetarios. El contrato JSON desplegable
conserva el `Decimal` completo, las unidades, fórmulas, parámetros, identidades y timestamps para
auditoría. El endpoint local `/api/market-chart` entrega `aapl-market-chart-v2`: mantiene los
decimales como cadenas exactas y separa sesiones fuente de puntos diarios, semanales o mensuales.
Cada punto agregado conserva todos los `raw_record_ids`, inputs de volumen, operaciones y VWAP,
además de las observaciones exactas de apertura, máximo, mínimo y cierre. Apertura y cierre proceden
de la primera y última sesión; máximo y mínimo son los extremos exactos; volumen y operaciones
completas se suman; VWAP solo se publica cuando todos los inputs existen y se pondera por volumen.
La calidad usa de forma conservadora el estado más restrictivo del intervalo: `suspect`, `partial`,
`delayed` y `valid`, en ese orden de precedencia.
Las SMA se recalculan sobre cierres de la resolución visible. Las estadísticas de rango conservan la
fórmula versionada, CAGR cuando existen al menos 365 días entre extremos y los inputs exactos del
máximo drawdown basado en cierres de esa resolución. La última sesión y sus estadísticas diarias se
mantienen separadas. La tabla OHLC se construye solo al abrirla para no cargar miles de nodos DOM.
El endpoint `/api/fundamental-research` entrega el contrato exacto
`aapl-fundamental-research-v1`, incluidas fórmulas, versiones, limitaciones e identidades de inputs.
La matriz compacta presenta el último período; su exportación conserva todos los períodos acotados
devueltos por la consulta.

El endpoint `/api/fundamental-research-history` envuelve ese contrato sin modificarlo y añade media,
mínimo, máximo, rango, cambio frente al período disponible anterior, cambio del horizonte y CAGR.
Los ratios se comparan mediante diferencias absolutas; no se expresa su cambio relativo. El CAGR
solo se calcula para series anuales en USD con extremos positivos y utiliza días transcurridos. Las
series trimestrales no se anualizan porque los flujos discretos pueden ser estacionales.

La estructura usa HTML semántico, un enlace para saltar al contenido, controles táctiles principales
de al menos 44 px,
foco visible, texto adicional al color, diseño adaptable y compatibilidad con reducción de movimiento
y colores forzados. El gráfico SVG se puede recorrer con las flechas, `Inicio` y `Fin`; una tabla
desplegable expone los intervalos OHLC, VWAP, SMA, volumen y operaciones sin depender de la imagen. Estos criterios reducen barreras,
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

La preferencia visual se guarda únicamente en `localStorage` con una clave versionada. No se envía
al servidor, no altera cálculos y puede restablecerse borrando los datos locales del sitio.

Las exportaciones se construyen en el navegador a partir de la respuesta exacta que ya está visible.
No crean archivos dentro del workspace ni realizan una nueva consulta al proveedor. Los CSV usan
UTF-8, conservan decimales como texto e incluyen identidades de evidencia, versiones de algoritmo,
`known_at` y trazabilidad. El JSON conserva íntegramente el contrato del reporte diario.

## Personalización analítica prevista

La versión de cierre deberá permitir personalizar indicadores que admitan parámetros, en especial
ventana, color y visibilidad de medias móviles, y rangos de estadísticas compatibles. Esa
personalización deberá usar límites tipados, mostrar fórmula y parámetros efectivos, conservar los
valores exactos y no modificar resultados persistidos ni algoritmos canónicos de forma silenciosa.
No forma parte todavía de este incremento.

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
