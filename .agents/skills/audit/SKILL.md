---
name: audit
description: Audita el único PR activo y publica evidencia exact-SHA. Requiere /audit o $audit; AUDIT es read-only y sólo una policy AUTO puede habilitar FINALIZE mecánico posterior.
---

# Audit Work Block

1. Leer completos `AGENTS.md`, `docs/development_protocol.md` y `.agents/rules/investment-analyst-core.md` en una sesión fresca e independiente por Work Block. Resolver fail-closed el único Work Block y su único PR abierto por expected branch/base; releer literalmente Issue, branch, base y SHA vivo. Ejecutar `scripts/check_workflow_guards.py --phase audit` antes y después de reconciliar el marker. Modelo/cliente es metadata, no autoridad.
2. AUDIT es read-only respecto de source, branch, candidato y workspace permanente: no ejecuta formatter, fixer ni comandos mutantes. Antes de PASS intenta refutar el candidato. Contabilizar todos los archivos del diff base...SHA, inspeccionar el diff completo y contexto crítico, justificar exclusiones no materiales y mapear explícitamente cada acceptance, invariante, negativo, contradicción, compatibilidad, scope, secretos, pruebas debilitadas y superficies protegidas. BUILD/CI/smoke o filenames nunca sustituyen esta revisión semántica.
3. Reutilizar el gate literal `Python 3.12 quality` verde y ejecutar a lo sumo una prueba focalizada read-only por riesgo concreto. Verificar SHA vivo, BUILD único PASS, smoke, requested changes, threads y probes de cada fallo material BUILD/FIX, o declarar que no existieron. Reconciliar todos los markers AUDIT según el algoritmo global antes de publicar/actualizar el único PASS/FAIL con rango, contabilidad, matriz de evidencia, hallazgos y riesgo.
4. BLOCKER, MAJOR, evidencia crítica ausente, diff material incompleto, identificador stale/sintetizado, contradicción sin resolver, test debilitado sin equivalencia o probe sin verificar impiden PASS. Con FAIL o policy HUMAN, terminar sin mutar PR. Con PASS y policy AUTO, termina AUDIT e inicia la transición mecánica explícita de FINALIZE sólo en esta invocación: dos snapshots, misma SHA exacta, confirmación de head y squash con `--match-head-commit`; nunca usa admin/bypass y reconcilia Issue/label idempotentemente.

Devolver únicamente `AUDIT PASS`/`CHANGES REQUIRED` o `COMPLETE`/`FINALIZATION BLOCKED`, SHA, hallazgos materiales, riesgo y siguiente acción.
