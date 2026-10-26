# DATA-CHASSIS: arranque v2 y cierre posterior

> **Supersesión de ruta (2026-10-22):** este documento conserva evidencia histórica de
> 45/closure y no se reabre como receta. El bloque 47 (#353) está implementado; la secuencia
> vigente y sus límites viven en [data_chassis_continuity_plan.md](data_chassis_continuity_plan.md).


> Documento histórico de la decisión de 2026-10-21. #345 cerrado y #346 MERGED; `DATA-CHASSIS` está
> `DONE`. La autoridad vigente son
> [`product_roadmap.md`](product_roadmap.md), [`basic_functional_release_plan.md`](basic_functional_release_plan.md)
> y el Issue `workflow:active`; este documento no es autoridad ejecutable.

Decisión humana de 2026-10-21: primero un workspace v2 nuevo, probado y observable; después,
incorporación de historia necesaria desde un archivo recuperable. Esta decisión sustituye el
compromiso anterior de cerrar toda la ruta dentro de seis bloques 40–45. No autoriza destruir v1,
retirar el estado F5, crear varios Issues activos ni llamar terminado a un objetivo pendiente.

## Ruta y autoridad

DATA-CHASSIS-45 / #345 / PR346 pasó de propuesta COMPLETES a ADVANCES. DATA-CHASSIS permaneció como
único `NEXT` en ese corte; FUNDAMENTALS-COVERAGE siguió `PLANNED`. El cierre global conserva el protocolo y Q1–Q7:
comparadores no equivalentes permanecen UNKNOWN. Una base nueva pequeña no acredita reducción
sobre el inventario histórico completo. La propuesta se integra sólo al fusionar el candidato.

El ledger AUTO integrado sólo vincula 45 con COMPLETES. Al cambiar a ADVANCES se usa HUMAN;
ningún documento candidato amplía ese ledger. BUILD y AUDIT pueden completar su trabajo sin pedir
aprobaciones intermedias; la integración HUMAN requiere el receipt posterior a AUDIT exact-SHA.

## Capacidades conservadas y trabajo siguiente

| Tramo | Situación | Siguiente uso |
| --- | --- | --- |
| 40–42 | Backend v2, incrementalidad y consultas implementados | Reutilizar; probar el recorrido nuevo y optimizar defectos medidos |
| 43–44 | Migración/backup/restore y transporte previamente acreditados | Conservar archivo/evidencia; no repetir 676 volúmenes |
| 45 anterior | Cinco cortes abortados, mejoras y progreso parcial preservados | No activar F5 ni fingir cierre; historia consultable |
| 45 integrado | V2 activo observado en `daad58d` (2026-10-22); UUID `70c64668-1738-4dfe-ba1f-7e72026bb7fe`; scheduler 13 jobs/3 activos; backup c0080d96 restaurado sobre `252.456.960 B`; delta posterior `29.360.128 B` y backlog 13F pendientes | [Continuidad](data_chassis_continuity_plan.md) y [runbook](data_chassis_45_acceptance_runbook.md) |
| Continuación | Secuencia obligatoria 46→47→48→49 | [Continuidad](data_chassis_continuity_plan.md) y [reincorporación](data_chassis_v1_reintegration_plan.md); 46 no importa ni borra |
| Cierre de DATA-CHASSIS | Pendiente | Gates operacionales, recuperación/retención y Q1–Q7 comparables |

## Fronteras que no se relajan

Decimal/IDs/PIT/append-only y separación de dominios; lectura completa dentro de cobertura declarada;
full/incremental equivalentes; checkpoints/revisión; páginas≤256 y cero hidratación ajena; backup
restaurado; un writer y recuperación que preserve escrituras nuevas. No reetiquetar históricos como
LIVE, copiar DB antigua sobre v2, saltar trazabilidad ni declarar ausencia como cero.

Los gates de migración completa siguen siendo criterios del cierre global, no condiciones para
probar o arrancar una raíz nueva que no afirma contener esa migración. Los resultados antiguos
mantienen sus bindings, no acreditan por sí solos la nueva raíz ni una cobertura mayor.
