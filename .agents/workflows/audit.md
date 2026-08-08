---
description: Alias de Antigravity IDE para auditar en read-only el PR del Work Block activo
---

Al invocar `/audit`, leer completo `.agents/skills/audit/SKILL.md` y seguirlo exactamente. Esta
invocación explícita tiene la misma autorización limitada que `$audit`: inspección read-only y un
único comentario estructurado para el SHA exacto; nunca edición, commit, push ni merge.
