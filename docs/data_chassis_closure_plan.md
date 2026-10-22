# DATA-CHASSIS: arranque v2 y cierre posterior

> Documento histórico de la decisión de 2026-10-21. #345/#346 ya está MERGED; el estado vigente y
> la secuencia obligatoria 46→47→48→49 están en el
> [registro de continuidad](data_chassis_continuity_plan.md).

Decisión humana de 2026-10-21: primero un workspace v2 nuevo, probado y observable; después,
incorporación de historia necesaria desde un archivo recuperable. Esta decisión sustituye el
compromiso anterior de cerrar toda la ruta dentro de seis bloques 40–45. No autoriza destruir v1,
retirar el estado F5, crear varios Issues activos ni llamar terminado a un objetivo pendiente.

## Ruta y autoridad

DATA-CHASSIS-45 / #345 / PR346 pasa de propuesta COMPLETES a ADVANCES. DATA-CHASSIS permanece como
único NEXT; FUNDAMENTALS-COVERAGE sigue PLANNED. El cierre global conserva el protocolo y Q1–Q7:
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
| 45 integrado | V2 activo en `daad58d`; UUID `70c64668-1738-4dfe-ba1f-7e72026bb7fe`; scheduler 13 jobs/3 activos; backup c0080d96 restaurado sobre `252.456.960 B`; delta posterior `29.360.128 B` y backlog 13F pendientes | [Continuidad](data_chassis_continuity_plan.md) y [runbook](data_chassis_45_acceptance_runbook.md) |
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
