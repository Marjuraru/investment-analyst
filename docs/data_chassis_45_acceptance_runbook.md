# DATA-CHASSIS-45: ejecución histórica del arranque v2 nuevo

> **Supersesión de ruta (2026-10-22):** este documento conserva evidencia histórica de
> 45/closure y no se reabre como receta. El bloque 47 (#353) está implementado; la secuencia
> vigente y sus límites viven en [data_chassis_continuity_plan.md](data_chassis_continuity_plan.md).


> Ejecutado e integrado por #346 MERGED. Estado vigente en el
> [registro de continuidad](data_chassis_continuity_plan.md).

Decisión PLAN de 2026-10-21. Estado observado 2026-10-22: v2 activo observado en la release `daad58d` (2026-10-22), v1
detenido y preservado; scheduler habilitado con13 jobs/3 activos; F11/F12 integrados; backup final
c0080d96 restaurado de forma independiente sobre `252.456.960 B`; delta posterior `29.360.128 B`
pendiente de backup nuevo. Work Block histórico #345, PR346, rama codex/data-chassis-45.
El builder debe refrescar HEAD/CI/bindings; nunca ejecutar desde los SHA históricos de este texto.

## Una sola receta

1. Leer [el plan de arranque](data_chassis_v2_fresh_start_plan.md), Issue/manifest y guard vivo.
2. Incorporar estos cambios documentales al candidato junto con la mínima implementación/pruebas.
3. Seguir la enmienda de estabilización del plan: fix read-only → regresiones → nuevo SHA/CI →
   actualizar el release conservando UUID → consumidores/replay → scheduler/recuperabilidad.
   Los ocho pasos originales describen el arranque; no reinicializar ni volver a adquirir por rutina.
4. Registrar en esta cabecera estado, comando literal ejecutado, SHA/tree/runtime, UUID, input,
   resultado/receipt y siguiente acción. No repetir una etapa acreditada si sus bindings no cambian.
5. Preparar [la reincorporación posterior](data_chassis_v1_reintegration_plan.md); no ejecutarla aquí.

## Estado de entrada preservado

| Hecho | Estado / evidencia | Uso permitido |
| --- | --- | --- |
| main | 969a8e7540244bea562055d5ab814103c59fb9df en el probe PLAN | Verificar fresco antes de BUILD |
| PR346 | 5fb973d9883ace4c4d55cf39bdc587e0b8dc66bc en el probe | Candidato, no integrado |
| F5 | HEALTHY_V1 tras aborto OOM; intento aa8dde20-448f-4c31-b571-d14d534a83ae | Historia preservada, no reanudar |
| checkpoint F5 | ops45/cut-f5-20261021/human-checkpoint-20261021T073756Z.json; SHA fcff88713a88a65482f57ac1168f6b60a3c806de89631355c8c31e1733489c75 | No borrar ni editar |
| v2 parcial | SUCCESSOR_BOUND revocado; DB2707959808B/SHA37cd1c606347f49b7e4f27374c61cb1022aba4cb9a1044ea0b92bab6baef9981 en receipt | Fuente archivada; no destino del arranque |
| fix relaciones | 399,288126s; pico2197442560B sobre imagen parcial | Evidencia acotada, no sello ni aceptación nueva |
| nuevo v2 | ACTIVO; UUID70c64668-1738-4dfe-ba1f-7e72026bb7fe | Misma raíz active-v2-20261021; no reinicializar |
| aceptación nueva | PENDIENTE | No PASS/READY por este documento |

ops45=~/.local/share/investment-analyst/ops/data-chassis-45.

## Comandos y APIs

`scripts/init_workspace.py --workspace <raíz-nueva> --format-version 2` ya está implementado en
el candidato y la raíz está creada; conservar su evidencia, no ejecutar otra inicialización.
BUILD materializa los comandos exactos de adquisición desde parsers/capabilities actuales y guarda
sus parámetros efectivos sin secretos antes de providers. No pegar --full para llenar historia.

Despliegue: `scripts/deploy_local_release.py candidate-stage --pr-number 346 --sha <SHA-completo>`
y `candidate-update --pr-number 346 --sha <mismo-SHA> --workspace <raíz-nueva>` son APIs existentes;
completar parámetros de unidad, entorno, readiness y scheduler con el parser real y el servicio.
No ejecutar esos templates sin gates, no skip-systemd/skip-health-check en la aceptación real.
Backup/restore: WorkspaceV2BackupService y CLI remota existente, con origen nuevo y destino propio.
No usar `close_data_chassis.py cut` ni controles cut-go F1–F5 para esta activación.

## Historia y alcance retirado de la ruta crítica

La [receta anterior archivada](archive/data_chassis_45_migration_runbook_20261021.md) conserva
comandos, estados y receipts de la migración. Sus “vigentes”, E4/GO, cuarto/quinto intento, siete
 días y COMPLETES son históricos; NO son instrucciones actuales. Reparar el sello de F5, reconstruir
G, transferir el delta F antiguo o limpiar v1 no son requisitos de este arranque nuevo.

Q1–Q7 y los gates globales del protocolo no se borran: se resolverán en la continuación de
DATA-CHASSIS. Manifest nuevo reemplaza la aceptación operativa del bloque; no convierte la antigua
en PASS. AUDIT revisa todo el diff del PR y mantiene regresiones de código retenido.

## Estado de cierre 2026-10-22

F10 coverage200 en37 assets, con DB/WAL/tablas sin cambios. F11/F12 están en el candidato y sus
probes se acreditan en el marker BUILD exact-SHA: HTTP AAPL+AMD200 con fachada real, PIT/lineage y
sin writes; colector v2 contrastado con delta DB/WAL independiente y replay sin cambios. Scheduler
habilitado con13 jobs/3 activos (AAPL, AMD, BTC +13F/SMV); 11 current y2 13F incomplete por backlog
contractual; FRED external-unavailable. Replay local de5 familias con ventana90d y digests exactos:
0 creadas/6.996 reutilizadas, DB+WAL0. Las14.758 métricas de un ensayo full-range distinto se
conservan como carga distinta; no son idempotencia PASS ni se deben reescribir. Los lotes históricos
77.594.624/48.234.496B conservan su fallo frente al criterio anterior de30MB. El backup final que
incluya todas las escrituras de aceptación queda pendiente hasta su receipt/restore independiente.
No repetir OAuth, init ni migración; no retirar v1/F5 ni borrar13F.
