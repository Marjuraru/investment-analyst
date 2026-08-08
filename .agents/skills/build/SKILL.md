---
name: build
description: Implementa y publica el único Work Block activo de investment-analyst. Usar únicamente cuando el usuario invoque explícitamente $build; autoriza implementación, validación, commit, push y un PR draft tras resolver Issue, base, rama y working tree sin ambigüedad, pero nunca merge.
---

# Build Work Block

1. Leer completos `AGENTS.md`, `docs/development_protocol.md` y el Issue resuelto.
2. Ejecutar el preflight Git obligatorio y consultar con JSON exactamente un Issue abierto con
   `workflow:active` usando el comando acotado del protocolo. Si hay cero o más de uno, detenerse
   sin escribir.
3. Extraer y verificar Work Block, base SHA, expected branch, owner, scope, profile y gates.
   Buscar PRs con `gh pr list --state open --head <expected-branch> --limit 2 --json
   number,headRefName,headRefOid,baseRefName,isDraft,url`. Ignorar PRs stale que no coincidan.
4. Verificar el contexto actual:
   - si el chat menciona otro bloque, detenerse con `BLOCK MISMATCH`;
   - si ya existe la expected branch, exigir que branch/worktree actual coincidan;
   - si no existe, crearla solo desde la base declarada y preservando todo trabajo local;
   - cualquier otro mismatch detiene la operación.
5. Ser el único writer e implementar el cambio cohesivo mínimo. No ampliar alcance ni tocar trabajo
   protegido. Corregir hallazgos existentes del mismo PR sin crear otro.
6. Ejecutar Ruff y Pytest focalizados durante la iteración. Ejecutar smoke real solo si el Issue lo
   exige. Usar `scripts/check.sh` únicamente bajo las excepciones del protocolo.
7. Revisar diff, secretos, compatibilidad y scope; stagear solo archivos previstos, commit y push.
8. Crear o actualizar un único PR draft contra la base declarada. Registrar Work Block, base/head
   SHA, checks, smoke y riesgos. Nunca fusionar ni cambiar a ready.
9. Esperar `Python 3.12 quality` del head SHA; no repetir localmente la suite completa verde.
10. Responder solo con estado, 2–4 cambios, PR, CI, smoke, riesgos y siguiente acción.

Si falla cualquier guard, responder `BUILD BLOCKED`, explicar una causa concreta y confirmar que no
se amplió el estado. Un nuevo commit invalida CI, smoke y audit previos.
