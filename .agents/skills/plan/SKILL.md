---
name: plan
description: Define y publica el siguiente Work Block de investment-analyst. Usar únicamente cuando el usuario invoque explícitamente $plan; no implementa ni publica código.
---

# Plan Work Block

1. Leer completos `AGENTS.md` y `docs/development_protocol.md`.
2. Sin `$plan`, permanecer read-only. Con `$plan`, operar control-plane-first: inspeccionar HEAD, árbol, Issues, PRs y labels; leer estrategia en `docs/product_roadmap.md`, ruta en `docs/basic_functional_release_plan.md`, reconciliarlas con main/GitHub vivos, formular hipótesis y luego usar exploración dirigida. La ruta no autoriza y toda desviación material requiere razón y evidencia viva en el Work Block. Si cambia la ruta, declarar `route_effect` (`NONE`/`ADVANCES`/`COMPLETES`), ítem, transición propuesta y siguiente esperado; sólo main es integrado y PLAN rechaza un `NEXT` stale cuando el cierre declarado ya está fusionado en main.
3. Resolver fail-closed el único Work Block activo según el protocolo, incluyendo candidatos abiertos/cerrados y relecturas directas. Si existe uno válido, no crear otro; si hay cero y la intención no es inequívoca, formular una sola pregunta material. Con decisión humana explícita, sólo puede superseder un bloqueo puramente temporal mediante el checkpoint, orden de mutaciones/relecturas y replan fresco definidos en el protocolo; nunca lo presenta como completion o BUILD terminal.
4. Crear un delta compacto: ID, objetivo, Scope/Risk/Profile, `finalize_policy`, base remota exacta, rama y writer role válido (`BUILD` o `UI_WORKER`), semantic scope o strict allowlist, invariantes, protected/forbidden surfaces con SHA-256 y el formato parser-owned, aceptación/negativos, capability delta y gates/excepciones. Modelo/cliente sólo es metadata de evidencia. R3 o superficie sensible requiere strict allowlist; una ruta de gobernanza requiere policy HUMAN.
5. Aplicar `workflow:active`, releer el Issue y comprobar que sigue habiendo exactamente uno. No crear rama, editar producto, commit, push, PR ni merge.

Ante cualquier preflight inválido devolver `PLAN GUARD FAILURE` sin modificar archivos.
