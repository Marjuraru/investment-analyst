# Presupuesto de memoria por trabajo programado

Este bloque contiene el consumo de un trabajo programado sin atribuirle todavía una causa
interna. El presupuesto es una medida operativa y no un umbral analítico, una señal, una
recomendación ni una autorización de ejecución.

## Tres capas independientes

1. **Cota dura de la unidad.** La unidad `systemd --user` puede renderizar `MemoryAccounting=yes`
   y `MemoryMax=`. El límite pertenece al cgroup del servicio: si una asignación cruza la cota
   dura, systemd contiene la muerte al servicio en vez de dejar que el kernel elija la tarea más
   grande del entorno completo. `StartLimitIntervalSec=900s` y `StartLimitBurst=3` limitan los
   reinicios rápidos. La unidad sólo adquiere estos valores después de una reinstalación humana;
   el producto no llama a `systemctl`, no escribe la unidad instalada y no recarga el daemon.
2. **Presupuesto cooperativo del proceso.** `--memory-ceiling-mb` configura el scheduler. Durante
   cada `_run_job`, `JobMemoryBudget` muestrea exclusivamente `/proc/self/status`, conserva el
   mayor `VmRSS` observado y llama una sola vez a `OperationControl.cancel()` al superar la cota.
   El callback debe alcanzar una frontera cooperativa (`raise_if_cancelled`) para terminar antes
   de la cota dura. Sin techo configurado, la vigilancia sigue midiendo y nunca cancela.
3. **Clasificación y evidencia del intento.** Una cancelación cuyo vigilante declara `breached`
   termina como `memory_budget_exceeded`, con `retryable=false`; una cancelación ordinaria sigue
   siendo `interrupted_job`. `ProviderJobTelemetry` conserva `provider-job-telemetry-v1` y añade
   `peak_rss_kb` opcional. `None` significa que la fuente no estaba disponible o no produjo una
   muestra; nunca se transforma en cero. Los intentos históricos sin el campo siguen validando.

## Qué garantiza y qué no

La vigilancia termina su hilo antes de cerrar el intento, mide el pico observado y no crea un hilo
por muestra. La cota cooperativa no interrumpe una asignación indivisible ni mata el proceso: sólo
solicita cancelación mediante el control ya existente. En ese caso la cota `MemoryMax=` instalada es
la capa de contención efectiva.

El bloque no corrige ni atribuye el consumo del refresh diario Coinbase, no cambia su planner,
pipeline o cliente, y no cambia la política de recuperación de un proceso muerto por el kernel. Un
proceso terminado por cgroup puede reaparecer como `interrupted_job` en la recuperación siguiente,
acotado por la política de intentos existente. La causa concreta queda como trabajo posterior con
`peak_rss_kb` como evidencia de entrada.

La ausencia de `/proc/self/status` es compatible con plataformas sin procfs: el pico queda en
`None`, el vigilante permanece inerte y se conserva el comportamiento de la base. No se lee
`/proc/meminfo`, otro PID ni el cgroup del host.

## Configuración y reinstalación

Los dos valores del instalador son megabytes positivos. Cuando ambos están presentes, el techo
cooperativo debe ser estrictamente menor que la cota cgroup:

```bash
.venv/bin/python scripts/install_local_service.py \
  --workspace <workspace-inicializado> \
  --env-file <environment-file-privado> \
  --memory-max-mb <cota-cgroup> \
  --memory-ceiling-mb <techo-del-proceso> \
  --output <unit-file-privado>
```

El comando sólo genera la unidad. Una persona debe inspeccionar el diff de la unidad para confirmar
que no contiene credenciales y, fuera del producto, ejecutar `systemctl --user daemon-reload` y
habilitar/reiniciar la unidad según el procedimiento de readiness. No se debe escribir ni modificar
el `EnvironmentFile` durante esta operación.

## Refutación medida del diagnóstico de #178

El diagnóstico previo atribuyó el pico a la reconciliación de arranque que reproduce 1 479
intentos. La medición directa lo refuta: sobre una copia temporal de los ficheros de estado reales,
`OperationalAlertMonitor.reconcile` con los 1 479 intentos completos tardó 0,1 s y el proceso
completo alcanzó 67,6 MiB de RSS. Los intentos ya tenían 1 479 recibos analíticos y 5 916
screenings operativos; las rutas de reconciliación tomaron su salida temprana y no reconstruyeron
estado por intento.

La ventana de OOM verificada mostró cuatro muertes `global_oom` en el servicio, con el scheduler
ejecutando trabajos Coinbase diarios después de completar el arranque:

| Hora local (-05) | PID | anon-rss | total-vm |
| --- | ---: | ---: | ---: |
| 21:48:23 | 25575 | 7 059 420 kB | 17 603 960 kB |
| 21:52:38 | 29327 | 7 152 276 kB | 13 854 292 kB |
| 22:08:54 | 29746 | 6 946 336 kB | 17 821 776 kB |
| 22:17:19 | 1754 | 7 045 944 kB | 13 392 264 kB |

La unidad no tenía `MemoryAccounting=yes`, por lo que su `602.9M memory peak` no era evidencia
comparable con el `anon-rss` del kernel. La captura por intento y la cota cgroup corrigen esa
carencia de atribución y contención sin afirmar todavía qué objeto del refresh consume la memoria.
