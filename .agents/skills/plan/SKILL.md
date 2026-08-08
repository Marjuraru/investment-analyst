---
name: plan
description: Define y publica el siguiente Work Block de investment-analyst. Usar únicamente cuando el usuario invoque explícitamente $plan para continuar el roadmap o convertir una intención concreta en un Issue; no implementar código ni crear PRs.
---

# Plan Work Block

1. Leer completos `AGENTS.md` y `docs/development_protocol.md`.
2. Inspeccionar HEAD, status, diff, roadmap, Issues, PRs y labels sin editar el repositorio.
3. Consultar Issues abiertos con `gh issue list --state open --label workflow:active --limit 2
   --json number,title,body,labels,url` y validar el conteo antes de interpretar el body.
   - Si hay uno, no crear otro; devolver su identidad y la acción compatible solicitada.
   - Si hay más de uno, detenerse por ambigüedad.
   - Si no hay ninguno, continuar solo con una intención o prioridad inequívoca; de lo contrario
     formular una sola pregunta material.
4. Definir una capacidad cohesiva con Work Block ID, objetivo, alcance, fuera de alcance,
   invariantes, base SHA, expected branch, owner, Scope, Risk, Profile, gates, criterios, casos
   negativos, routing y trabajo protegido.
5. Tratar como R3 cualquier cambio de point-in-time, fórmula, identidad, persistencia,
   concurrencia, seguridad o semántica financiera.
6. Crear un único Issue, aplicar `workflow:active` y verificar después que existe exactamente un
   Issue activo. No crear rama, editar producto, commit, push ni PR.
7. Dejar todo el detalle en el Issue y responder solo con capacidad, perfil, builder recomendado,
   audit, decisiones y siguiente acción.

No elegir por “último Issue” ni copiar contexto de chats anteriores. Si cualquier preflight falla,
responder `PLAN BLOCKED` y confirmar que no se modificaron archivos.
