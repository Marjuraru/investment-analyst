---
name: investment-block-flow
description: Compatibilidad para orientar el workflow explícito de investment-analyst mediante $plan, $build, /ui y /audit o $audit.
---

# Investment Block Flow

Leer completos `AGENTS.md` y `docs/development_protocol.md`. El repositorio, árbol de trabajo,
GitHub Issue, PR y estado vivo son autoritativos.

Elegir sólo el rol autorizado explícitamente: PLAN (`$plan`), BUILD (`$build`), UI_WORKER (`/ui`),
AUDIT (`/audit` o `$audit`) o Corrección concreta de un PR. Los permisos dependen del rol; el
modelo/cliente sólo es metadata de evidencia. Resolver siempre el target fail-closed; conservar
trabajo local, secretos e historia; no ampliar scope ni admitir dos writers.

PLAN crea un delta compacto. BUILD sigue la tabla canónica hasta terminalidad, publica un draft y
mantiene un único marker exact-SHA; FAST/AUTO puede seguir a FINALIZE tras gates vivos. AUDIT revisa
semánticamente el diff material completo, permanece read-only para source/branch/candidato y
mantiene su marker único. Un PASS con policy AUTO puede continuar a los dos snapshots de FINALIZE.
Una policy HUMAN, y siempre CRITICAL salvo excepción humana explícita, termina en HUMAN MERGE. No
inventar comandos, duplicar el algoritmo global ni usar el último Issue/PR como target.
