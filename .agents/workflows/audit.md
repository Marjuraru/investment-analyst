---
description: Alias de Antigravity IDE para AUDIT y, cuando la policy AUTO lo permita, FINALIZE mecánico
---

Al invocar `/audit`, leer completo `.agents/skills/audit/SKILL.md` y seguirlo exactamente. AUDIT es
read-only respecto del candidato; sólo tras PASS de la invocación actual y policy AUTO puede ejecutar
FINALIZE según `docs/development_protocol.md`. Nunca edita source, usa admin/bypass ni fusiona con
policy HUMAN.
