---
name: investment-block-flow
description: Compatibilidad para orquestar Issues y PRs explícitos de investment-analyst, corregir hallazgos, revisar CI o fusionar cuando el usuario lo autorice. Para el workflow activo normal usar $plan, $build y /audit.
---

# Investment Block Flow

Leer primero `AGENTS.md` y `docs/development_protocol.md` completos. Tratar el repositorio, GitHub y
el árbol de trabajo actual como fuentes autoritativas.

## Seleccionar el modo

Elegir únicamente el modo autorizado por la petición explícita:

1. **Arquitecto:** definir el siguiente bloque y crear su Issue; no implementar.
2. **Implementador:** implementar un Issue concreto y publicar o actualizar su PR; no fusionar.
3. **Auditor:** revisar Issue, PR, diff, informe y CI; permanecer en solo lectura salvo autorización explícita para fusionar.
4. **Corrección:** resolver únicamente comentarios o bloqueos concretos del PR; no ampliar alcance.

No convertir una revisión o auditoría en autorización para editar.

## Aplicar reglas comunes

- Mantener un Issue y un PR por capacidad cohesiva, con un único escritor por rama.
- Para el flujo sin números, delegar en `$plan`, `$build` y `/audit`; no inventar el último target.
- Clasificar el riesgo como R0 mecánico, R1 normal, R2 alto o R3 crítico. Tratar point-in-time, fórmulas, identidades, almacenamiento, concurrencia, seguridad y semántica financiera como R3.
- Preservar el workspace permanente, secretos, historial y cambios locales preexistentes.
- Detener cualquier ampliación de alcance y devolverla al arquitecto como bloque separado.
- Vincular toda evidencia al HEAD exacto, comando, entorno y resultado.
- No repetir una suite completa ya verde para el mismo HEAD y entorno. Repetirla solo si cambió el HEAD, cambió el entorno relevante, falló o se canceló CI, o la auditoría encontró un riesgo no cubierto.
- Si el mismo fallo aparece dos veces, dejar de reintentar y diagnosticar la causa.
- Evitar polling frecuente y no lanzar una segunda ejecución mientras la primera siga activa.

## Modo arquitecto

1. Inspeccionar el estado de `main`, roadmap, Issues y dependencias vigentes.
2. Definir objetivo, alcance, fuera de alcance, invariantes, criterios verificables, matriz de riesgos, especialista y evidencias requeridas.
3. Crear un único Issue completo cuando el usuario lo solicite.
4. Si el disparador es `continua al siguiente bloque`, responder únicamente con número del Issue, especialista y objetivo en una línea.
5. No editar código ni iniciar la implementación.

## Modo implementador

1. Leer el Issue y confirmar que la rama parte de `main` actualizado.
2. Ser el único escritor del PR y realizar el cambio cohesivo mínimo.
3. Usar pruebas focalizadas durante la iteración.
4. Ejecutar pruebas focalizadas y usar CI como gate determinista completo del HEAD publicado. Solo
   correr `scripts/check.sh` bajo las excepciones documentadas.
5. Ejecutar smoke real cuando el Issue afecte proveedores, persistencia, scheduler, interfaz o servicio.
6. Publicar o actualizar el PR y el informe de entrega con HEAD, pruebas exactas y riesgos.
7. Responder únicamente con PR, resultados exactos y riesgos pendientes.

## Modo auditor

1. Verificar que Issue, PR e informe corresponden al mismo alcance y HEAD.
2. Revisar primero contratos, diff, pruebas, CI, smoke, riesgos y comentarios abiertos.
3. Reutilizar CI verde del HEAD exacto; no volver a ejecutar localmente la suite completa.
4. Ejecutar como máximo pruebas focalizadas justificadas por un riesgo no cubierto.
5. Si no cumple, publicar o devolver solo bloqueos accionables.
6. Si cumple y el usuario autorizó la fusión, hacer squash-merge, sincronizar `main` y responder `ok`.

## Handoff entre modelos

Usar GitHub como memoria compartida. No copiar historiales de chat. El implementador deja evidencia en Issue o PR; el auditor reconstruye el estado desde GitHub y el repositorio. Un auditor de otra familia de modelos permanece en solo lectura y devuelve hallazgos bloqueantes con referencias concretas.
