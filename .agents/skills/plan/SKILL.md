---
name: plan
description: Define y publica el siguiente Work Block de investment-analyst. Usar únicamente cuando el usuario invoque explícitamente $plan para continuar el roadmap o convertir una intención concreta en un Issue; no implementar código ni crear PRs.
---

# Plan Work Block

1. Leer completos `AGENTS.md` y `docs/development_protocol.md`.
2. Sin la invocación exacta `$plan`, cualquier análisis, comparación, investigación, revisión o
   recomendación permanece read-only: no crear ni actualizar Issues, labels, ramas, PRs o archivos.
3. Con `$plan`, inspeccionar HEAD, status, diff, roadmap, Issues, PRs y labels sin editar el
   repositorio. Consultar candidatos abiertos y cerrados con `gh issue list --state open --label
   workflow:active --limit 2 --json number,title,body,labels,url` y `gh issue list --state closed
   --label workflow:active --limit 2 --json number,title,body,labels,url`; releer directamente cada
   uno con `gh issue view <number> --json number,state,labels,title,body,url` y contar solo los
   abiertos que aún conserven el label. Verificar que estado, labels y metadata no cambiaron durante
   la resolución.
   - Si hay uno realmente activo, no crear otro; devolver su identidad y la acción compatible.
   - Si hay más de uno, o un candidato cambia durante la lectura, detenerse por guard failure.
   - Si hay un Issue cerrado que realmente conserva `workflow:active`, detenerse: no retirar el
     label implícitamente.
   - Si no hay ninguno realmente activo, continuar solo con una intención o prioridad inequívoca;
     de lo contrario formular una sola pregunta material.
4. Definir una capacidad cohesiva con Work Block ID, objetivo, alcance, fuera de alcance,
   invariantes, base SHA, expected branch, owner, Scope, Risk, Profile, gates, criterios, casos
   negativos, routing y trabajo protegido.
5. Tratar como R3 cualquier cambio de point-in-time, fórmula, identidad, persistencia,
   concurrencia, seguridad o semántica financiera.
6. Crear un único Issue, aplicar `workflow:active`, releerlo directamente y verificar después que
   existe exactamente un Issue realmente activo. No crear rama, editar producto, commit, push ni PR.
7. Dejar todo el detalle en el Issue y responder solo con capacidad, perfil, builder recomendado,
   audit, decisiones y siguiente acción.

No elegir por “último Issue” ni copiar contexto de chats anteriores. Si cualquier preflight falla,
responder `PLAN GUARD FAILURE` y confirmar que no se modificaron archivos.
