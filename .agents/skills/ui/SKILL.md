---
name: ui
description: Ejecuta BUILD como UI_WORKER para Work Blocks de interfaz; requiere una invocación explícita del rol UI_WORKER y nunca crea una vía paralela.
---

# UI Worker Work Block

1. Leer completos `AGENTS.md`, `docs/development_protocol.md` y el Issue resuelto. Ejecutar exactamente BUILD y seguir su preflight y tabla en `.agents/skills/build/SKILL.md`.
2. Resolver fail-closed el único Issue activo, base, expected branch, único PR, profile, policy y worktree; comprobar hashes protegidos antes de escribir.
3. Ejecutar el capability preflight sin mostrar secretos. `UI_WORKER` es una especialización restringida de `BUILD_PRODUCT`, nunca governance; modelo/cliente no amplía ni reduce autoridad y el repo, Issue, PR y GitHub siguen siendo la fuente de verdad.
4. Ser el único writer del bloque. Modificar sólo la frontera UI declarada (`src/investment_analyst/frontend/static/**`, tests y docs enumerados); no tocar `local_web.py` sin allowlist explícita.
5. Detener y volver a PLAN ante lógica financiera, analytics, PIT/`available_at`, Decimal, identidades, storage, providers, scheduler, API semantics, diagnósticos, screening, valoración, señales, recomendaciones o trading.
6. Mantener el mismo branch/PR draft, marker BUILD exact-SHA con manifest SHA-256, gates, smoke, auditoría y FINALIZE que BUILD; releer GitHub después de cada mutación y usar scratch externo al worktree con limpieza verificable.
7. No usar subagentes writers, worktrees paralelos ni handoff dentro del bloque. Ejecutar Ruff, pruebas focalizadas y smoke real hasta terminalidad; devolver sólo `BUILD READY`, `BUILD BLOCKED` o `BUILD GUARD FAILURE`.
