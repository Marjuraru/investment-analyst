---
name: investment-block-flow
description: Compatibilidad para orientar el workflow explícito de investment-analyst mediante $plan, $build y /audit o $audit.
---

# Investment Block Flow

Leer completos `AGENTS.md` y `docs/development_protocol.md`. El repositorio, árbol de trabajo,
GitHub Issue, PR y estado vivo son autoritativos.

Elegir sólo el modo autorizado explícitamente: Arquitecto (`$plan`), Implementador (`$build`),
Auditor (`/audit` o `$audit`) o Corrección concreta de un PR. Resolver siempre el target
fail-closed; conservar trabajo local, secretos e historia; no ampliar scope ni admitir dos writers.

PLAN crea un delta compacto. BUILD publica un draft y su evidencia exact-SHA; FAST/AUTO puede seguir
a FINALIZE tras sus gates. AUDIT permanece read-only para source/branch/candidato; un PASS con
policy AUTO puede continuar a FINALIZE mecánico bajo los guards globales. Una policy HUMAN, y
siempre CRITICAL salvo excepción humana explícita, termina en HUMAN MERGE. No inventar comandos ni
usar el último Issue/PR como target.
