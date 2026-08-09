# Development Workflow v1

Este protocolo gobierna la infraestructura de ingeniería alrededor de `investment-analyst`. No
modifica producto, fórmulas, runtime ni workspace. `AGENTS.md`, el árbol de trabajo, el Work Block,
la rama, el PR y GitHub CI son las fuentes de verdad, en ese orden.

## Defaults globales y Work Blocks compactos

Un Work Block es una capacidad cohesiva. Sólo puede existir uno abierto con `workflow:active`.
PLAN publica deltas compactos; no copia estas reglas globales. Cada Issue declara: ID, objetivo,
Scope/Risk/Profile, `finalize_policy`, base, expected branch, owner, alcance, invariantes,
superficies protegidas/prohibidas, aceptación/negativos, capability delta y gates o excepciones.

`finalize_policy` sólo admite `AUTO` o `HUMAN`: FAST usa AUTO por defecto; STANDARD usa AUTO tras
AUDIT PASS; CRITICAL usa HUMAN. Un override HUMAN de FAST/STANDARD requiere justificación en PLAN.
AUTO en CRITICAL exige instrucción humana explícita y justificación en PLAN. DEV-3 conserva HUMAN.

Scope estima tamaño (`S1`–`S3`) y Risk gobierna el control (`R0`–`R3`). R3, y R2 que toque
seguridad/permisos, migraciones, schema/storage, identidad, fórmulas financieras, CI o contratos
point-in-time, usa strict allowlist. El resto usa objetivo, semantic scope y superficies
protegidas/prohibidas: BUILD puede modificar costuras cohesivas razonablemente necesarias, sin
ampliar la capacidad.

## Resolución fail-closed y estado remoto

Antes de escribir, PLAN, BUILD y AUDIT consultan candidatos con `gh issue list --state open --label
workflow:active --limit 2 --json number,title,body,labels,url`, releen cada candidato mediante `gh
issue view <number> --json number,state,labels,title,body,url` y cuentan sólo Issues abiertos que
conservan el label. Cero, múltiples, metadata ausente, un Issue cerrado aún etiquetado o snapshots
cambiantes son guard failure sin combinar lecturas.

El target se resuelve como `Issue activo → expected branch → único PR abierto de esa head branch →
SHA vivo`. BUILD/AUDIT consultan `gh pr list --state open --head <expected-branch> --limit 2 --json
number,headRefName,headRefOid,baseRefName,isDraft,url`. Un PR stale no determina el target. BUILD
exige rama y worktree compatibles; si la rama no existe puede hacer `fetch` y crearla sólo desde el
SHA base remoto declarado, sin stash, reset, overwrite ni checkout forzado. PLAN toma bases desde
GitHub/origin; no exige `main` local sincronizado. El estado remoto reconciliado define COMPLETE.

## Capability envelope y preflight BUILD

PLAN declara sólo capacidades no triviales que podrían interrumpir BUILD: provider/host real,
credencial por nombre, CLI/browser/servicio externo, escritura en workspace permanente, cambios de
dependencias y policy de finalización. Git/GitHub normal, repo-scoped writes, lectura CI y estado
efímero son defaults.

Antes de implementación material, BUILD resuelve Work Block/gates, deriva capacidades de scope,
capabilities y smoke, y comprueba ejecutables, configuración sin mostrar valores, permisos GitHub
y accesos previsibles. Agrupa una única solicitud inicial para autorizaciones externas previsibles y
acotadas; los prompts inevitables del runtime se registran separadamente y nunca se eluden. Una
capacidad imprevisible puede terminar `BUILD BLOCKED`.

## Roles y happy paths

PLAN sólo crea o actualiza el Work Block. BUILD es el único writer, implementa el mínimo cohesivo,
ejecuta checks focalizados, preserva trabajo protegido y publica un PR draft. Para FAST con AUTO,
BUILD puede continuar a FINALIZE sólo tras todos los gates vivos. AUDIT fija un SHA, permanece
read-only respecto de source/branch/candidato y publica PASS o FAIL. Para perfiles que exigen
auditoría, sólo un PASS de la invocación actual puede iniciar FINALIZE cuando la policy es AUTO; el
comentario es evidencia, no trust root.

- FAST: `$plan → $build → gates → FINALIZE → COMPLETE`.
- STANDARD: `$plan → $build → /audit → PASS → FINALIZE → COMPLETE`.
- CRITICAL: `$plan → $build → /audit → PASS → AWAITING HUMAN APPROVAL → HUMAN MERGE`.

No se crean comandos `$merge` ni `$finalize`. Un cambio material de objetivo, scope, arquitectura o
aceptación vuelve a PLAN; un mismatch de base o rama falla cerrado.

## Evidencia viva

El body del PR contiene sólo Work Block, objetivo/resumen, base, scope, riesgos y referencias.
Head/merge se leen de GitHub y CI de checks vivos. BUILD publica o actualiza un único comentario
machine-owned del SHA exacto para checks focalizados y smoke:

```html
<!-- development-workflow:build-v1
block=<WORK-BLOCK>
sha=<FULL-SHA>
status=PASS|FAIL
-->
```

AUDIT publica o actualiza un único comentario del SHA exacto:

```html
<!-- development-workflow:audit-v1
block=<WORK-BLOCK>
sha=<FULL-SHA>
status=PASS|FAIL
reviewer=<EXACT-MODEL>
-->
```

Un commit posterior invalida CI, smoke y auditoría. `Python 3.12 quality` es la autoridad de la
suite determinista para el SHA publicado. BUILD usa Ruff y pruebas focalizadas; `bash
scripts/check.sh` sólo se ejecuta si cambian CI/dependencias/lock/toolchain, CI no está disponible,
el bloque lo justifica o el usuario lo pide. Cambios de skills, aliases o descubrimiento además
requieren smoke real en un cliente soportado: `/skills` o equivalente lista `plan`, `build`,
`audit` e `investment-block-flow`, y `/audit` se resuelve sin auditar ni mutar producto.

## FINALIZE mecánico

Sólo con policy AUTO, FINALIZE relee: único Work Block activo y metadata coherente; PR único con
branch/base esperadas; head vivo igual al SHA auditado cuando auditoría sea requerida; CI requerido
PASS; smoke PASS/N/A; audit PASS actual sin BLOCKER/MAJOR cuando aplique; cero conversaciones o
requested changes pendientes; PR abierto, mergeable, con permisos y con protección de `main`
vigente. Si algo cambia, falla cerrado sin merge.

FINALIZE marca ready, relee el head y hace squash merge con `--match-head-commit`; nunca usa
`--admin`, bypass ni otra estrategia. Si falla tras marcar ready, intenta volver a draft sólo si el
head sigue intacto y devuelve `FINALIZATION BLOCKED`. AUDIT no modifica el candidato: FINALIZE es
una fase lógica posterior, de mutaciones GitHub estrechas, de la misma invocación autorizada.

Tras un merge, FINALIZE verifica PR `MERGED`, cierra el Issue si fuera necesario, retira
`workflow:active` con remove-label (nunca borra el label del repositorio), relee el Issue y confirma
cero Work Blocks activos. El cleanup es idempotente. PLAN sólo puede reparar un Issue cerrado aún
etiquetado si su PR y metadata prueban inequívocamente un merge correcto.

## Handoff, validación y salidas

GitHub es el bus de handoff. La evidencia vincula SHA, comando, entorno, resultado y timestamps.
No se repite una suite completa verde para el mismo SHA y entorno. Un auditor reutiliza CI verde y
ejecuta a lo sumo una prueba focalizada por riesgo concreto. No se accede al workspace permanente
sin autorización.

BUILD continúa mientras haya una acción ejecutable. Sus únicas salidas son `BUILD READY` (AUDIT
para STANDARD/CRITICAL o FINALIZE/HUMAN MERGE para FAST según policy), `BUILD BLOCKED`
(intervención/autoridad/recurso externo) y `BUILD GUARD FAILURE` (target o precondición
fail-closed). AUDIT devuelve PASS o CHANGES REQUIRED; FINALIZE devuelve COMPLETE o FINALIZATION
BLOCKED. Nunca se ocultan riesgos, scope creep, gates ausentes, cambios de metadata ni prompts de
sandbox.

## Diferido de V1

No se añaden Actions, bots, servicios, helpers/scanners de comentarios, estado persistente, comandos
nuevos, auto-merge nativo, branch deletion, worktrees automáticos, parallel authoring ni
sincronización destructiva de `main`.
