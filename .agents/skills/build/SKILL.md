---
name: build
description: Implementa y publica el único Work Block activo de investment-analyst. Usar únicamente cuando el usuario invoque explícitamente $build; para FAST/AUTO también puede ejecutar FINALIZE tras todos los guards vivos.
---

# Build Work Block

1. Leer completos `AGENTS.md`, `docs/development_protocol.md` y el Issue resuelto. Ejecutar preflight Git y resolver fail-closed el único Issue activo, base, expected branch, profile, policy, scope y gates.
2. Resolver el PR sólo por expected branch. Si la rama existe, exigir branch/worktree compatibles; si no, crearla desde la base remota exacta sin alterar trabajo local. Un chat de otro bloque, snapshots cambiantes o metadata ambigua son `BUILD GUARD FAILURE` sin escribir.
3. Antes de implementación material, ejecutar el capability preflight: derivar las necesidades declaradas y previsibles, comprobar CLI/configuración sin secretos, permisos GitHub y accesos. Agrupar al inicio como máximo una solicitud externa previsible y acotada.
4. Ser el único writer. Para R3/superficie sensible, tocar exclusivamente la strict allowlist; en otro caso respetar semantic scope, superficies protegidas y prohibidas. Implementar el mínimo cohesivo y corregir sólo hallazgos del mismo PR.
5. Ejecutar Ruff y pruebas focalizadas; smoke real cuando el Issue lo exija. Usar `scripts/check.sh` únicamente según el protocolo. Revisar diff, secretos, compatibilidad, scope y trabajo protegido.
6. Stagear sólo el alcance previsto, commit, push y fijar el SHA completo. Crear o actualizar un único PR draft contra la base declarada; su body contiene sólo datos estables. Publicar/actualizar el comentario BUILD exact-SHA con checks focalizados y smoke.
7. Esperar `Python 3.12 quality` para ese SHA, realizar el smoke de descubribilidad exigido y reconciliar el handoff contra el head vivo. Sólo FAST/AUTO puede continuar a FINALIZE según el protocolo; cualquier otro perfil permanece draft y nunca cambia a ready.

Continuar mientras haya acción ejecutable. Responder sólo `BUILD READY`, `BUILD BLOCKED` o `BUILD GUARD FAILURE`, cambios, PR, CI/smoke y riesgos. Un commit invalida evidencia previa.
