---
name: build
description: Implementa y publica el único Work Block activo de investment-analyst. Usar únicamente cuando el usuario invoque explícitamente $build; autoriza implementación, validación, commit, push y un PR draft tras resolver Issue, base, rama y working tree sin ambigüedad, pero nunca merge.
---

# Build Work Block

1. Leer completos `AGENTS.md`, `docs/development_protocol.md` y el Issue resuelto.
2. Ejecutar el preflight Git obligatorio. Consultar candidatos con `gh issue list --state open
   --label workflow:active --limit 2 --json number,title,body,labels,url`, releer directamente cada
   uno con `gh issue view <number> --json number,state,labels,title,body,url` y contar solo los
   abiertos que aún conserven el label. Verificar que candidatos, estado, labels y metadata no
   cambien durante la resolución. Si hay cero o más de uno realmente activo, un Issue cerrado aún
   etiquetado o un snapshot cambiante, terminar con `BUILD GUARD FAILURE` sin escribir.
3. Extraer y verificar Work Block, base SHA, expected branch, owner, scope, profile y gates.
   Buscar PRs con `gh pr list --state open --head <expected-branch> --limit 2 --json
   number,headRefName,headRefOid,baseRefName,isDraft,url`. Ignorar PRs stale que no coincidan.
4. Verificar el contexto actual:
   - si el chat menciona otro bloque, detenerse con `BLOCK MISMATCH`;
   - si ya existe la expected branch, exigir que branch/worktree actual coincidan;
   - si no existe, crearla solo desde la base declarada y preservando todo trabajo local;
   - cualquier otro mismatch detiene la operación.
5. Ser el único writer e implementar el cambio cohesivo mínimo. No ampliar alcance ni tocar trabajo
   protegido. Corregir hallazgos existentes del mismo PR sin crear otro. Un reemplazo de builder
   solo procede tras terminar el anterior, releer Issue/PR/base/head/worktree y preservar cambios y
   evidencia válidos; no vuelve a PLAN salvo cambio material de objetivo, scope, arquitectura o
   aceptación.
6. Ejecutar Ruff y Pytest focalizados durante la iteración. Ejecutar smoke real solo si el Issue lo
   exige. Usar `scripts/check.sh` únicamente bajo las excepciones del protocolo.
7. Revisar diff, secretos, compatibilidad y scope; stagear solo archivos previstos, commit y push.
   Fijar el full head SHA después del push y refrescar el head vivo del PR.
8. Crear o actualizar un único PR draft contra la base declarada. Esperar `Python 3.12 quality` y
   ejecutar el smoke exigido para ese mismo SHA; no repetir localmente la suite completa verde.
   Actualizar y verificar el handoff final del PR contra el head vivo con SHA, checks, smoke, riesgos
   y siguiente gate. Nunca fusionar ni cambiar a ready.
9. Continuar mientras quede una acción ejecutable dentro del contrato. Tests corregibles, CI
   pendiente, integración pendiente o gates ejecutables no son bloqueos humanos. Responder solo
   `BUILD READY` (next gate AUDIT para STANDARD/CRITICAL o HUMAN MERGE para FAST), `BUILD BLOCKED`
   (intervención, autorización o recurso externo real) o `BUILD GUARD FAILURE` (precondición o
   target fail-closed), más 2–4 cambios, PR, CI, smoke y riesgos.

Un nuevo commit invalida CI, smoke y audit previos. Un guard failure no se presenta como bloqueo
humano ni se cierra con un mero handoff de progreso.
