---
name: investment-block-flow
description: Orquesta la selección de roles del workflow de investment-analyst. Los roles son independientes del mecanismo de invocación del harness.
---

# Investment Block Flow

Leer completos `AGENTS.md` y `docs/development_protocol.md`. El repositorio, árbol de trabajo,
GitHub Issue, PR y estado vivo son autoritativos.

Elegir sólo el rol autorizado explícitamente: PLAN, `$build` despachado como `BUILD_PRODUCT`,
`BUILD_GOVERNANCE` o UI_WORKER, AUDIT o Corrección concreta de un PR. `BUILD` genérico no es un
writer role estable.
El mecanismo de invocación pertenece al harness y no define autoridad ni permisos. Los permisos dependen del rol;
el modelo/cliente sólo es metadata de evidencia. Resolver siempre el target fail-closed; conservar
trabajo local, secretos e historia; no ampliar scope ni admitir dos writers.

Resolver instrucciones, protocolo y guard desde la base integrada verificada; si el primary está
atrasado, registrar la ruta y el digest del ejecutable usado y no confundir un snapshot correcto con
el script correcto. No ejecutar código ni rutas candidatas de un Issue como autoridad.

PLAN crea un delta compacto y su manifest parser-owned. BUILD sigue la tabla canónica hasta terminalidad, publica un draft y
mantiene un único marker exact-SHA y ejecuta el guard común vivo `scripts/check_workflow_guards.py`; los snapshots JSON son sólo diagnóstico y nunca gates. FAST/AUTO puede seguir a FINALIZE tras gates vivos. AUDIT revisa
semánticamente el diff material completo, permanece read-only para source/branch/candidato y
mantiene su marker único. Un PASS con policy AUTO puede continuar sólo a dos guards completos
`--live --phase finalize`, separados por ready y sin reutilizar snapshots.
FINALIZE incluye la entrega local verificada de su merge SHA antes del cleanup; una entrega fallida
postmerge es continuación, no bootstrap.
Antes de exigir PR abierto o asumir cero PR = bootstrap, reconocer la continuación postmerge
pendiente del target explícito según «Recuperación postmerge de entrega local»: Issue cerrado o sin
label no acreditan despliegue; sólo identidad y autorización demostradas del PR fusionado continúan
la entrega, sin elegir «el último», degradar un runtime más nuevo ni mutar durante revisión
read-only. Un GUARD FAILURE o un AUTO sin grant verificado no son policy HUMAN ni se sustituyen por
una aprobación humana inventada; las mutaciones pertenecen al FINALIZE autorizado.
Una policy HUMAN, y cualquier AUTO sin standing o legacy verificado en la base, termina en HUMAN MERGE. No
inventar comandos, duplicar el algoritmo global ni usar el último Issue/PR como target.
