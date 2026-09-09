# Registro de feedback y decisiones de producto — septiembre de 2026

Este registro resume disposiciones de feedback externo y del usuario que orientan UI-15. No es una
fuente de autoridad para contratos ni reproduce respuestas externas completas. La autoridad vigente
es, en este orden operativo, el `main` remoto verificado, `AGENTS.md`, el protocolo de desarrollo,
la ruta táctica y los contratos vivos del repositorio.

## Revisión externa

- **Adoptado:** conservar evidencia point-in-time (`available_at`), Decimal, append-only,
  trazabilidad y separación entre capas; priorizar el uso auditable del analista sobre adornos;
  resolver primero proyecciones/rangos; hacer explícitas las ambigüedades de correspondencia; y
  exigir label, baseline y validación temporal antes de cualquier ML.
- **Corregido por evidencia viva:** el incidente de eficiencia de Coinbase no se trata como causa
  completamente desconocida: `RUNTIME-EFFICIENCY-4` corrigió la materialización no acotada
  identificada. La medición de RSS/filas y la optimización de otros read paths siguen siendo
  trabajo separado y no se maquillan en la interfaz.
- **Parcial o diferido:** la deuda nominal `Aapl*`, la consolidación de CLI, CUSIP, BVL y cualquier
  cambio de dominio requieren sus propios bloques y compatibilidad explícita.
- **Rechazado como atajo:** no retirar BVL por falta de barras, no reescribir almacenamiento en
  masa, no cambiar fórmulas o ponderaciones, y no anticipar Postgres, microservicios, XGBoost/SHAP,
  LLM o dependencias nuevas.
- **Límite adoptado:** “no más UI” rige después de esta corrección post-completion; UI-15 sólo
  corrige densidad, desbordes y colecciones ya integradas.

## Feedback del usuario

- **Integrado previamente:** limpieza de copy y limitaciones, reloj y countdown BVL, densidad,
  símbolos distinguibles, alcance por tablero, navegación contextual, tablas avanzadas ocultas,
  Sistema al final y legibilidad fundamental en UI-13/UI-14.
- **Decidido para UI-15:** sustituir etiquetas crudas por copy humano manteniendo IDs canónicos en
  handlers/datasets; usar filas compactas; mover `run-form` a mantenimiento avanzado de Sistema;
  agrupar Valoración y Comparación responsivamente; y presentar colecciones largas con ventana
  inicial de diez elementos.
- **Fuera de alcance:** cobertura desigual, ausencia point-in-time, evidencia Cazatiburones,
  caché/persistencia incremental, BVL y CUSIP pertenecen a sus dominios. No se añaden contratos,
  endpoints, polling, almacenamiento ni promesas de frescura desde la UI.

## Correcciones verificadas contra el repositorio vivo

1. La base remota y el árbol se verificaron antes de editar; `main` permaneció limpio y el writer
   usa un worktree aislado.
2. La implementación reutiliza payloads, endpoints, catálogo, filtros locales, formularios y
   handlers existentes. El único estado nuevo es efímero y cuenta filas visibles; no se persiste.
3. La presentación progresiva no reduce límites de servidor, no reordena datos, no elimina campos
   de exportación y no escoge un sustituto cuando una identidad contextual está fuera de la primera
   página.
4. El mantenimiento avanzado conserva validación, IDs, último activo y rutas existentes; el
   resumen cerrado no afirma proveedor ni actualidad universal.

## Límite de UI-15

`route_effect: NONE`: `LOCAL-INTERFACE` sigue `DONE` y `SEC-CORPUS` sigue siendo la única ruta
`NEXT`. No se modifican backend, HTTP, dominio, almacenamiento, proveedores, scheduler, fórmulas,
identidades, dependencias ni el workspace permanente.

## Excepción UI-16 posterior a UI-15

La indicación humana posterior autoriza únicamente una reparación geométrica observada en Mesa,
Valoración y Comparación: retirar el nombre accesible duplicado como copy visible, separar las
regiones de las filas contextuales, recuperar alturas naturales de los grupos de Valoración y reunir
los dos grupos de Comparación en una superficie. No autoriza rediseñar Revisar o Cazatiburones ni
ampliar UI, rutas, contratos, datos, endpoints, polling, persistencia, proveedores o tipografías.
La ruta continúa con `route_effect: NONE`: `LOCAL-INTERFACE` queda `DONE` y `SEC-CORPUS` es el único
`NEXT`.
