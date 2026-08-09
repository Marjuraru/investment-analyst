---
name: plan
description: Define y publica el siguiente Work Block de investment-analyst. Usar únicamente cuando el usuario invoque explícitamente $plan; no implementa ni publica código.
---

# Plan Work Block

1. Leer completos `AGENTS.md` y `docs/development_protocol.md`.
2. Sin `$plan`, permanecer read-only. Con `$plan`, operar control-plane-first: inspeccionar primero HEAD, árbol, roadmap, Issues, PRs y labels, y luego usar exploración dirigida sólo sobre las superficies necesarias.
3. Resolver fail-closed el único Work Block activo según el protocolo, incluyendo candidatos abiertos/cerrados y relecturas directas. Si existe uno válido, no crear otro; si hay cero y la intención no es inequívoca, formular una sola pregunta material.
4. Crear un delta compacto: ID, objetivo, Scope/Risk/Profile, `finalize_policy`, base remota exacta, rama, owner, semantic scope o strict allowlist, invariantes, protected/forbidden surfaces con SHA-256 de bytes del worktree cuando corresponda, aceptación/negativos, capability delta y gates/excepciones. R3 o superficie sensible requiere strict allowlist.
5. Aplicar `workflow:active`, releer el Issue y comprobar que sigue habiendo exactamente uno. No crear rama, editar producto, commit, push, PR ni merge.

Ante cualquier preflight inválido devolver `PLAN GUARD FAILURE` sin modificar archivos.
