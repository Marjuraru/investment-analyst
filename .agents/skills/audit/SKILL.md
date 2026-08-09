---
name: audit
description: Audita el único PR activo y publica evidencia exact-SHA. Requiere /audit o $audit; AUDIT es read-only y sólo una policy AUTO puede habilitar FINALIZE mecánico posterior.
---

# Audit Work Block

1. Leer completos `AGENTS.md`, `docs/development_protocol.md` y `.agents/rules/investment-analyst-core.md` en un chat Gemini nuevo para cada Work Block. Resolver fail-closed el único Work Block y su único PR abierto por expected branch/base; fijar y refrescar el SHA vivo.
2. AUDIT es read-only respecto de source, branch, candidato y workspace permanente. Contabilizar todos los archivos del diff base...SHA, inspeccionar materialmente el diff completo y contexto crítico, justificar cada exclusión no material y mapear acceptance, invariantes, negativos, compatibilidad, scope, secretos y superficies protegidas. BUILD/CI/smoke o filenames nunca sustituyen esta revisión semántica.
3. Reutilizar `Python 3.12 quality` verde y ejecutar a lo sumo una prueba focalizada por riesgo concreto. Verificar SHA vivo, BUILD único PASS, smoke, requested changes y threads. Reconciliar todos los markers AUDIT según el algoritmo global antes de publicar/actualizar el único PASS/FAIL con rango, cobertura, evidencia, hallazgos y riesgo.
4. BLOCKER, MAJOR, evidencia crítica ausente o diff material incompleto impiden PASS. Con FAIL o policy HUMAN, terminar sin mutar PR. Con PASS y policy AUTO, FINALIZE puede empezar sólo en esta invocación y ejecuta los dos snapshots, confirmación de head y squash con `--match-head-commit`; nunca usa admin/bypass y reconcilia Issue/label idempotentemente.

Devolver únicamente `AUDIT PASS`/`CHANGES REQUIRED` o `COMPLETE`/`FINALIZATION BLOCKED`, SHA, hallazgos materiales, riesgo y siguiente acción.
