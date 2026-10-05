# Apple operational runner

`scripts/run_aapl_daily.py` is the single operational entry point for one complete Apple refresh.
It wraps the stable application facade with a per-workspace process lock, atomic latest-run state,
bounded error output, and a read-only health command. It is suitable for manual execution now and
for an external scheduler or local interface later.

The runner is intentionally one-shot. It does not remain resident, choose a trading-calendar date,
install a scheduler, or retry providers indefinitely. The caller supplies the inclusive market
range for every execution. The separate local interface and calendar scheduler are documented in
[`local_interface.md`](local_interface.md).

## Platform and configuration

Run the command with the repository's Python environment inside WSL. The process lock uses the
Linux `fcntl` contract and is not a native Windows-Python entry point.

The run command requires these inherited environment variables:

- `ALPACA_API_KEY`
- `ALPACA_API_SECRET`
- `SEC_USER_AGENT`

The script does not load `.env`. A trusted, local, Git-ignored `.env` can be exported explicitly in
the invoking shell:

```bash
set -a
source .env
set +a
```

Credentials are passed only to the existing provider clients. They are not written to operational
state or included in JSON and error output.

## Execute one complete run

From the repository root:

```bash
.venv/bin/python scripts/run_aapl_daily.py run \
  --market-start 2025-01-01 \
  --market-end 2026-07-15 \
  --fundamental-frequency quarterly
```

The standard workspace precedence applies. Select another initialized or new permanent workspace
with `--workspace PATH`. `--refresh-mode auto` is the default; `--refresh-mode full` requests the
whole market range without deleting history. Dates are inclusive.

By default, both independent diagnostics are required. `--allow-partial` permits the existing
partial consolidated status when only one side can be produced. It does not combine market and
fundamental results or weaken their independent evidence.

Without `--known-at`, the underlying bootstrap chooses one UTC cutoff after ingestion. For a
reproducible retrospective execution, pass an explicit timezone-aware value:

```bash
.venv/bin/python scripts/run_aapl_daily.py run \
  --market-start 2025-01-01 \
  --market-end 2026-07-15 \
  --fundamental-frequency quarterly \
  --known-at 2026-07-16T15:46:09.048264Z
```

Each invocation still queries SEC Submissions and Company Facts. In automatic refresh mode,
already-covered Alpaca market data is skipped while SEC and analytical stages continue. Existing
deterministic records and results are reused, and successful earlier stages remain persisted if a
later stage fails.

## Lock and atomic state

Only one runner may write a workspace at a time. The advisory lock is retained at:

```text
<workspace>/state/aapl_daily_run.lock
```

The file's PID and run metadata are diagnostic only. The held operating-system lock is the
authority; a retained file after process exit does not block the next run.

The latest bounded state is atomically replaced at:

```text
<workspace>/state/aapl_daily_run_state.json
```

Its versioned `aapl-daily-run-state-v1` contract records the request, timestamps, status, compact
counters, point-in-time cutoff, refresh mode, consolidated completeness, and traceability. It never
contains provider documents, observations, headers, credentials, or tracebacks. This file is the
latest operational summary, not the analytical history; append-only evidence remains in workspace
storage.

The lifecycle is:

- `running`: written before the application facade starts;
- `succeeded`: atomically replaces `running` only after the complete result and traceability check;
- `failed`: atomically replaces `running` with a bounded safe category and message.

An abrupt termination can leave `running` after the operating-system lock is released. Health
reports that combination as degraded and explicitly identifies the run as interrupted. The next
run may safely resume through existing idempotent bootstrap behavior; no workspace cleanup or
manual database editing is required.

## Read-only health

Inspect workspace integrity and the latest run without initializing or modifying the workspace:

```bash
.venv/bin/python scripts/run_aapl_daily.py health
```

Use `--workspace PATH` to inspect another workspace. Health statuses are:

- `ready`: workspace is valid and no failed or interrupted latest run exists;
- `running`: valid workspace with a matching process lock currently held;
- `degraded`: incomplete workspace, failed latest run, or interrupted `running` state.

A valid workspace with no operational state remains `ready` and reports that no run has yet been
recorded. This lets existing workspaces adopt the runner without a migration.

## Exit codes

For `run`:

- `0`: completed successfully;
- `1`: unexpected internal failure with sanitized output;
- `2`: missing configuration or an expected validation, provider, workspace, storage, or state
  failure;
- `3`: the strict bootstrap finished without both required independent diagnostics;
- `4`: another process already holds the workspace run lock.

For `health`, `0` means `ready` or actively `running`, `3` means `degraded`, and `2` means health
could not read valid workspace or operational state.

The runner performs descriptive analysis only. It does not use an LLM, execute orders, manage
money, or produce an investment recommendation.

## Muestreo operacional de memoria y adopción del probe

El sampler y cycle probe de DATA-CHASSIS-36 son herramientas operativas locales. El sampler lee una lista fija de propiedades de systemd, /proc y cgroup v2; no abre DuckDB ni el workspace. El reporte normal del ciclo consulta el overview HTTP local, captura systemd/proc/cgroup y lee estado/journal y la BD en read-only; espera el cierre del scheduler, así que se reserva para la aceptación operacional posterior al merge. El BUILD sólo ejecuta la identidad read-only; no copia scripts al runtime ni instala/modifica unidades.

Desde la raíz del repositorio, estas comprobaciones no escriben muestras ni reportes:

    PYTHONPATH=src .venv/bin/python scripts/cycle_probe.py runtime-identity
    PYTHONPATH=src .venv/bin/python scripts/memory_sampler.py --no-write --json

runtime-identity imprime identidad actual y metadata del último reporte, sin abrir la BD/workspace ni escribir. --no-write captura y muestra una muestra y sale. El probe comparó el runtime vivo observado el 2026-10-04 con el SHA e276526871bd2efa7ddae3a4efc35d0d769b2c71; la referencia cycle-2026-10-04.json no tenía runtime_identity, por lo que el resultado fue inconcluso. No usar el modo report como diagnóstico rápido: espera el cierre diario y escribe cycle-<día-Lima>.json/.md; repetirlo el mismo día reemplaza esos dos archivos. cycle_probe.py despacha por primer argumento y no define --help; usar sólo los modos documentados runtime-identity, baseline o report.

### Adopción después del merge y aprobación operativa

Instalar los dos scripts juntos en el directorio ops del runtime aceptado, porque cycle_probe carga memory_sampler desde su mismo directorio. Antes de activarlos, comparar los archivos de origen y destino contra el commit exacto integrado:

    sha256sum scripts/cycle_probe.py scripts/memory_sampler.py
    sha256sum /home/marjuraru/.local/share/investment-analyst/ops/cycle_probe.py /home/marjuraru/.local/share/investment-analyst/ops/memory_sampler.py

Los hashes de cada par deben coincidir con el SHA integrado. Revisar localmente las unidades de systemd para confirmar que ejecutan esos paths, que no modifican la unidad del producto ni sus límites, y que no contienen credenciales. Consultar sólo metadatos no secretos de la unidad/timer:

    systemctl --user show investment-analyst-memory-sampler.timer --property=LoadState,ActiveState,LastTriggerUSec,NextElapseUSecRealtime,Result
    systemctl --user list-timers --all investment-analyst-memory-sampler.timer

Primero verificar desde el directorio del repositorio, sin escribir:

    PYTHONPATH=src .venv/bin/python /home/marjuraru/.local/share/investment-analyst/ops/cycle_probe.py runtime-identity
    PYTHONPATH=src .venv/bin/python /home/marjuraru/.local/share/investment-analyst/ops/memory_sampler.py --no-write --json

Una muestra puntual escrita, sólo durante esa aceptación:

    PYTHONPATH=src .venv/bin/python /home/marjuraru/.local/share/investment-analyst/ops/memory_sampler.py --once --json

Se anexa a ops/samples/mem-<fecha-America-Lima>.jsonl; el default conserva 14 días de muestras. El reporte de ciclo se ejecuta una vez después de que el scheduler pruebe que terminó el día:

    PYTHONPATH=src .venv/bin/python /home/marjuraru/.local/share/investment-analyst/ops/cycle_probe.py report

Ese reporte escribe en ops/reports; consulta el overview local y lee estado/journal y DuckDB en read-only. Tras el cierre mide una vez los bytes UTF-8 lógicos de `metric_results` con un agregado; el baseline manual conserva esa medición explícita. Los intentos programados sólo cuentan filas y consultan stat de DB/WAL: no escanean `document_json` por job. No convertir el reporte en una tarea frecuente: la lectura lógica completa pertenece al reporte posterior al cierre, no al sampler por muestra ni al hook de cada intento.

La comprobación reproducible offline del colector y los enlaces al ciclo usa sólo DuckDB y jobs
sintéticos en un directorio temporal:

    PYTHONPATH=src .venv/bin/python scripts/smoke_operational_observability.py --output /tmp/data-chassis-38-smoke.json

Debe imprimir JSON con los perfiles `operational_observability_cost` y
`operational_observability_series` en estado `pass`; el fichero queda disponible para adjuntarlo al
registro del BUILD. El smoke no invoca proveedores ni abre el workspace permanente.

Si el timer elegido usa Persistent=true, debe ser de calendario con OnCalendar=: systemd ignora Persistent para timers sólo monotónicos. Tras reanudar una laptop o activar un timer con intervalos vencidos, varios vencimientos durante la inactividad producen una sola activación; se registra un punto nuevo, no se reconstruye el historial ausente. No basar una comparación en huecos rellenados. Revisar además AccuracySec para que su tolerancia sea compatible con la cadencia configurada. Referencia: [systemd.timer(5)](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html).

### Rollback

Para la unidad con el nombre adoptado, detener y deshabilitar sólo el timer del sampler:

    systemctl --user disable --now investment-analyst-memory-sampler.timer
    systemctl --user stop investment-analyst-memory-sampler.service

Si la unidad tiene otro nombre, sustituirlo por el nombre verificado en systemd. Conservar ops/samples y ops/reports para auditoría; no borrar reportes, muestras, estado del scheduler, workspace ni base de datos. Este rollback no cambia ni reinicia investment-analyst. El despliegue y la activación siguen pendientes de la aceptación exact-SHA posterior al merge.
