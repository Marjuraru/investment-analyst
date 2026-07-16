# Interfaz local y operación continua

La interfaz local convierte los flujos existentes de Apple en una herramienta básica utilizable
desde el navegador. El mismo proceso puede ejecutar una programación diaria. No añade fórmulas,
scores combinados, recomendaciones, Trading API ni un LLM activo.

## Capacidades

La página permite:

- revisar el workspace, la última ejecución, la trazabilidad y la programación;
- cargar automáticamente el último reporte elegible al abrir la página;
- ejecutar manualmente el bootstrap completo de SEC EDGAR y Alpaca Market Data IEX;
- consultar el reporte diario point-in-time en modo trimestral o anual;
- seleccionar opcionalmente fechas `as-of` independientes para mercado y fundamentales;
- ver diagnósticos, métricas, frescura, limitaciones y el contrato JSON versionado;
- mantener una ejecución diaria mientras el servicio está activo.

Mercado y fundamentales se muestran en tarjetas separadas. La interfaz no calcula ni muestra un
veredicto, confianza, calidad, recomendación o ranking combinado.

## Criterios de presentación

La interfaz está orientada al análisis, no al trading. Mantiene el contexto de Apple visible,
prioriza el diagnóstico y sus métricas, y deja la actualización de fuentes en una sección operativa
secundaria. La navegación, estados, veredictos, calidad, métricas, mensajes y limitaciones se
presentan en español; se conservan nombres técnicos ampliamente usados como AAPL, SEC EDGAR, IEX,
SIP, SMA, JSON y point-in-time cuando aportan precisión.

Los valores financieros exactos no se modifican ni se vuelven a persistir. El navegador aplica
redondeo únicamente para presentación:

- puntuaciones independientes: hasta un decimal;
- cobertura de evidencia: porcentaje con hasta un decimal;
- retornos, volatilidad y ratios porcentuales: porcentaje con hasta dos decimales;
- volumen relativo y pasivos sobre patrimonio: múltiplo con hasta dos decimales;
- medias móviles en USD: dos decimales;
- conteos: enteros con separador de miles.

Los ceros decimales innecesarios se omiten, salvo en importes monetarios. El contrato JSON desplegable
conserva el `Decimal` completo, las unidades, fórmulas, parámetros, identidades y timestamps para
auditoría.

La estructura usa HTML semántico, un enlace para saltar al contenido, controles de al menos 44 px,
foco visible, texto adicional al color, diseño adaptable y compatibilidad con reducción de movimiento
y colores forzados. Estos criterios reducen barreras, pero no sustituyen una auditoría formal con
tecnologías de asistencia.

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
