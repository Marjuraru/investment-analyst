---
name: audit
description: Audita el único PR activo y publica evidencia exact-SHA. Requiere /audit o $audit; AUDIT es read-only y sólo una policy AUTO puede habilitar FINALIZE mecánico posterior.
---

# Audit Work Block

1. Leer completos `AGENTS.md`, `docs/development_protocol.md` y `.agents/rules/investment-analyst-core.md`. Resolver fail-closed el único Work Block y su único PR abierto por expected branch/base; fijar y refrescar el SHA vivo.
2. AUDIT es read-only respecto de source, branch, candidato y workspace permanente. Verificar CI `Python 3.12 quality`, smoke y comentario BUILD para el mismo SHA; revisar alcance, invariantes, negativos, secretos y trabajo protegido. Reutilizar CI y ejecutar a lo sumo una prueba focalizada justificada.
3. Publicar o actualizar un único comentario `development-workflow:audit-v1` con PASS/FAIL para ese SHA. BLOCKER y MAJOR impiden PASS; MINOR sólo cuando incumple aceptación o contrato crítico.
4. Con FAIL o policy HUMAN, terminar sin mutar PR. Con PASS y policy AUTO, FINALIZE puede empezar sólo en esta invocación y debe ejecutar todos los guards y el procedimiento de squash + `--match-head-commit` del protocolo. Nunca usa admin/bypass; tras merge reconcilia Issue/label de forma idempotente.

Devolver únicamente `AUDIT PASS`/`CHANGES REQUIRED` o `COMPLETE`/`FINALIZATION BLOCKED`, SHA, hallazgos materiales, riesgo y siguiente acción.
