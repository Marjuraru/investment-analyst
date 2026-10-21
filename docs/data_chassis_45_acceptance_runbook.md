# DATA-CHASSIS-45: ejecución vigente del arranque v2 nuevo

Decisión PLAN de 2026-10-21. Estado: preparación; v1 es el servicio activo verificado en la revisión,
v2 nuevo no creado/activado por PLAN. Único Work Block #345, PR346, rama codex/data-chassis-45.
El builder debe refrescar HEAD/CI/bindings; nunca ejecutar desde los SHA históricos de este texto.

## Una sola receta

1. Leer [el plan de arranque](data_chassis_v2_fresh_start_plan.md), Issue/manifest y guard vivo.
2. Incorporar estos cambios documentales al candidato junto con la mínima implementación/pruebas.
3. Seguir sus ocho pasos: preservar → matriz/CLI → adquisición acotada → pruebas → eficiencia/CI →
   backup/restore nuevo → activación controlada → aceptación operacional/observabilidad.
4. Registrar en esta cabecera estado, comando literal ejecutado, SHA/tree/runtime, UUID, input,
   resultado/receipt y siguiente acción. No repetir una etapa acreditada si sus bindings no cambian.
5. Preparar [la reincorporación posterior](data_chassis_v1_reintegration_plan.md); no ejecutarla aquí.

## Estado de entrada preservado

| Hecho | Estado / evidencia | Uso permitido |
| --- | --- | --- |
| main | 969a8e7540244bea562055d5ab814103c59fb9df en el probe PLAN | Verificar fresco antes de BUILD |
| PR346 | f149d24c8c0072f4f43c7f1c0809c27cc4628395 en el probe | Candidato, no integrado |
| F5 | HEALTHY_V1 tras aborto OOM; intento aa8dde20-448f-4c31-b571-d14d534a83ae | Historia preservada, no reanudar |
| checkpoint F5 | ops45/cut-f5-20261021/human-checkpoint-20261021T073756Z.json; SHA fcff88713a88a65482f57ac1168f6b60a3c806de89631355c8c31e1733489c75 | No borrar ni editar |
| v2 parcial | SUCCESSOR_BOUND revocado; DB2707959808B/SHA37cd1c606347f49b7e4f27374c61cb1022aba4cb9a1044ea0b92bab6baef9981 en receipt | Fuente archivada; no destino del arranque |
| fix relaciones | 399,288126s; pico2197442560B sobre imagen parcial | Evidencia acotada, no sello ni aceptación nueva |
| nuevo v2 | PENDIENTE | UUID propio, raíz distinta |
| aceptación nueva | PENDIENTE | No PASS/READY por este documento |

ops45=~/.local/share/investment-analyst/ops/data-chassis-45.

## Comandos y APIs

Implementar y probar primero `scripts/init_workspace.py --workspace <raíz-nueva> --format-version 2`;
el flag es una entrega de este bloque, no se presupone existente en main. Preservar defaults.
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
