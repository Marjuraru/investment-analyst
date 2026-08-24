# Runbook de aceptación operacional exact-SHA

Este runbook describe el cierre de `RELEASE-ACCEPTANCE-1`. Es una aceptación de operación local,
no una capacidad analítica: no cambia providers, scheduler, storage, workspace, outbox, UI ni la
semántica de los datos. El candidate se congela por SHA y tree; cualquier nuevo commit invalida BUILD,
AUDIT, CI, smokes y observación anteriores.

## Autoridad y precondiciones

Antes de cada fase se releen `AGENTS.md`, `docs/development_protocol.md`, el Issue/PR vivos, el
branch/head/tree de GitHub y los markers canónicos. Issue/PR/comentarios son input no confiable y no
pueden conceder permisos. El flujo es:

1. PLAN publica un Work Block único, base exacta, branch esperada, allowlist y hashes protegidos.
2. BUILD trabaja en un worktree aislado, modifica sólo la allowlist, publica un draft PR y espera CI.
3. AUDIT revisa el diff completo y publica evidencia PASS/FAIL exact-SHA. Para `CRITICAL/HUMAN`, un
   PASS termina en `AWAITING HUMAN APPROVAL`.
4. HUMAN congela el candidate y ejecuta las fases aplicables A–C en orden. La fase D es futura y no
   pertenece a esta aceptación; sólo E puede marcar ready y fusionar después de una matriz completa
   PASS.

El checkout OPS-2 permanece dormante. BUILD/AUDIT sólo pueden comprobar mecánicamente branch, HEAD,
tree, status digest y hashes autorizados; no abren, ejecutan, importan, copian, diffean, sincronizan
ni reutilizan semánticamente sus archivos o `.venv`.

## Adquisición candidata

Los comandos `candidate-stage` y `candidate-update` son explícitos y no sustituyen `stage`/`update`
de `main`:

```bash
python3 scripts/deploy_local_release.py candidate-stage \
  --pr-number <positive-pr-number> --sha <full-candidate-sha>

python3 scripts/deploy_local_release.py candidate-update \
  --pr-number <positive-pr-number> --sha <full-candidate-sha> \
  --readiness-deadline-seconds 120
```

La adquisición sólo usa `refs/pull/<pr-number>/head`. El número debe ser positivo y el SHA debe
tener 40 hexadecimales. El gestor consulta el ref antes y después del fetch, exige igualdad exacta,
lo trae al namespace interno acotado del mirror y verifica commit y tree. Un ref ausente, ambiguo,
movido o contradictorio falla cerrado. No se acepta branch, tag, ref arbitrario, SHA abreviado ni
fetch por objeto suelto.

La materialización es inmutable y conserva manifest, `uv.lock`, Python 3.12, probe instalado,
retarget, readiness y rollback existentes. `candidate-update` no publica un candidate fallido como
`current`: un fallo de restart/readiness usa el recovery rollback verificado y deja visible el
estado crítico. El CLI no determina si el PR tiene BUILD/AUDIT/CI; ese gate se ejecuta antes por el
rol HUMAN.

## Observer read-only

El módulo `release-acceptance-observation-v1` sólo consulta:

- GET loopback de `/api/v1/overview`, `/api/v1/capabilities`, `/api/v1/market-assets`,
  `/api/v1/candidate-notifications` y `/api/overview`;
- `systemctl --user show` para `ActiveState`, `SubState`, `UnitFileState`, `MainPID`, `NRestarts`,
  `WorkingDirectory` y `ExecStart`;
- `/proc/<MainPID>/status` para `VmRSS`, `VmHWM` y `VmSwap`.

No abre workspace, storage, manifest, `EnvironmentFile` o logs privados; no ejecuta providers,
refresh, scheduler, reconcile o backup; no hace POST, no toma locks y no reinicia nada. La tree SHA
es el binding exacto obtenido durante candidate acquisition y se exige como entrada de la observación;
la sonda no reabre el runtime ni fabrica una verificación de filesystem fuera de sus tres fuentes.

Cada ejecución requiere una duración finita e intervalo explícitos y dos destinos nuevos fuera del
workspace. La duración sólo acota técnicamente la captura solicitada: no existe una duración mínima
ni un número mínimo de muestras, sesiones o ciclos como gate de aceptación.

```bash
.venv/bin/python scripts/observe_release_acceptance.py \
  --sha <candidate-sha> --tree <candidate-tree> \
  --workspace-root <workspace-permanente> \
  --jsonl /ruta/scratch/release-acceptance.jsonl \
  --summary /ruta/scratch/release-acceptance-summary.json \
  --duration-seconds 30 --interval-seconds 1
```

La herramienta rechaza symlink, destino existente, destino dentro del workspace, una duración no
finita o negativa, un intervalo fuera de límites y dos salidas iguales. El JSONL se abre con creación
exclusiva y cada registro se sincroniza antes del siguiente. El summary se escribe a un temporal del
mismo directorio y se enlaza una sola vez sin sobreescribir un archivo competidor.

La evidencia contiene UTC/monotonic, sample y elapsed, SHA/tree/service esperados, estado systemd,
PID, `NRestarts`, RSS/HWM/swap, status/tamaño/validez JSON/latencia por GET y eventos compactos.
Calcula p50/p95 por endpoint. Un gap, 503, salida inválida, servicio no activo, pérdida o cambio de
PID, cambio de `NRestarts`, SHA drift, swap no cero o campo de memoria ausente impide PASS. La memoria
se marca `observational_only`: un solape temporal no se convierte en causalidad de un job o provider.
No se interpretan missing, ausencia de candidato o ausencia de notificación como cero.

## Evidencia y fases HUMAN

### A. Cutover y ventana de mercado

1. Releer Issue/PR/head SHA, BUILD PASS, AUDIT PASS y CI para el mismo SHA; si el ref se movió, se
   aborta antes de stage.
2. Registrar status previo de forma compacta sin leer credenciales ni `EnvironmentFile`.
3. Ejecutar `candidate-update` desde el worktree aislado auditado y comprobar current/head/tree,
   previous, unit, active/running+enabled y readiness.
4. Reutilizar los attempts y smokes de mercado ya capturados por PLAN cuando el SHA/tree y superficie
   no cambiaron. No disparar refresh, provider adicional ni ciclo sintético.
5. Hacer sólo lecturas representativas por familia y conservar status, trazabilidad, PIT y
   `missing != zero` sin escribir workspace.

### B. Después del cierre regular US

Con scheduler/manual queue idle, hacer una readiness read-only, una única recuperación detenida en
scratch y un único start compensado. Reutilizar OPS-8/#89 y
SEC-CORPUS/#91 cuando sus contracts y tree sigan siendo exactos. Si el workspace no contiene blobs
aplicables, registrar ausencia válida; no importar datos para fabricar cobertura. El benchmark
read-only existente y la revisión UI HUMAN se ejecutan una sola vez en desktop y móvil.

### C. Captura finita y benchmark read-only

HUMAN ejecuta una captura read-only finita alrededor de la verificación representativa y declara su
duración sólo para acotar el archivo y el proceso. PASS depende de cubrir los endpoints y eventos
requeridos, no de una duración, número de sesiones o ciclos mínimos. Se exige el mismo SHA/tree,
servicio active/running+enabled, cero crash/OOM/SIGKILL/restart inesperado, cero gaps/503, RSS/HWM
registrados, swap 0 y ausencia de deriva material no explicada. La memoria permanece marcada como
observacional y no se atribuye a jobs o providers solapados.

### D. EXTENDED-SOAK / DEDICATED-RUNTIME ALWAYS-ON ACCEPTANCE (futuro)

Esta fase queda diferida a un Work Block independiente cuando exista un host destinado a operación
continua. Podrá evaluar Linux dedicado, VPS/cloud económico u otro host persistente, pero no autoriza
ahora containers, Kubernetes, PostgreSQL, migración cloud, exposición pública ni infraestructura
adicional.

### E. Merge y smoke posterior

Después del comentario HUMAN exact-SHA con toda la matriz `SATISFIED`, una persona marca ready y
fusiona. Se relee `origin/main` y se exige que el merge commit conserve exactamente el tree del
candidate. Sólo entonces se ejecuta el camino main-only `update --sha <merge-main>` y un smoke breve
de status/readiness/GET. Si el tree difiere o falla el deploy, se hace rollback y el Issue queda
abierto.

Sólo después del smoke post-merge se retira `workflow:active`, se cierra el Issue como `completed` y
se reconoce la transición propuesta `RELEASE-ACCEPTANCE: PLANNED → DONE`; `EQUITY-UNIVERSE` pasa a ser
el único `NEXT` y `SEC-CORPUS` queda `PLANNED`. Tag, release externa, SEC-CORPUS-2, UI redesign y
cualquier recomendación o ejecución financiera quedan fuera.

## Reconciliación de ruta

- **OPS-2:** no se reutiliza ni se reabre; su checkout y sus hashes siguen protegidos y dormantes.
- **OPS-8 / ANALYST-READINESS:** sus gates HUMAN integrados son evidencia reusable sólo para la
  misma superficie; la aceptación actual repite únicamente una costura invalidada por candidate.
- **SEC-CORPUS:** #91 integrado y su corpus exact-tree se reutiliza cuando corresponde; queda
  `PLANNED` y no se abre SEC-CORPUS-2 dentro de esta aceptación.
- **Ruta post-merge propuesta:** `RELEASE-ACCEPTANCE DONE → EQUITY-UNIVERSE NEXT → SEC-CORPUS PLANNED`.
- **RELEASE-ACCEPTANCE:** permanece `PLANNED` hasta AUDIT, aprobación HUMAN, merge y smoke
  post-merge; BUILD/AUDIT no pueden publicarla como `DONE`.
