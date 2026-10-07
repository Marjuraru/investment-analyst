# Cierre de la ruta DATA-CHASSIS

Este plan conserva la decisión humana de seis Work Blocks fijos, `DATA-CHASSIS-40` a
`DATA-CHASSIS-45`. Cuenta el bloque 40 como el primero. Los otros cinco son fronteras de
planificación, no autorización anticipada para crear o ejecutar Issues. Cada PLAN futuro debe
reconciliar `main`, GitHub, el workspace y la evidencia vigentes, y declarar desviaciones materiales.
No se agrega un séptimo bloque ni se rebajan gates automáticamente si el presupuesto no alcanza.

## Autoridad y estado de los objetivos

El registro canónico de objetivos, baselines, comparadores y snapshot Q1–Q7 está en
[`investment-analyst-core.md`](../.agents/rules/investment-analyst-core.md). Este documento no duplica
ni modifica sus estados, denominadores o metas. Resultados de scratch, integración o CI describen
implementación y no convierten objetivos operacionales en `MET`.

El Work Block 40 entrega un backend v2 seleccionable mediante un manifiesto de workspace nuevo y
adopta repositorios tipados y consumidores existentes en un workspace scratch. El formato v1 sigue
siendo el default. El smoke de este bloque no abre el workspace permanente, no hace migración real,
no activa v2 productivo y no acredita ahorro del host.

## Seis fronteras planificadas

| Work Block | Capacidad sustancial | Criterio de salida y evidencia |
| --- | --- | --- |
| 40 | Backend v2 compatible, compacto y recuperable: workspace versionado, lectura histórica y escrituras tipadas, archivo histórico sellado, repositorios compatibles y backup v2. | Mismos modelos, IDs, JSON, Decimal y cortes PIT en scratch restaurado; consumidores existentes; perfiles de fidelidad, recuperación y escala. Sin cutover real. |
| 41 | Corregir revisiones de metadata y adquisición SEC con disponibilidad point-in-time y entregar un colector acotado que conserve un resultado terminal por intento aunque falle la medición. | Cinco emisores: Submissions frescos, v3 sólo con bytes idénticos y v4 sólo con prueba byte-exact terminal; historia v1/v2/v3/v4 y restore verificados. Ciclo scratch de 102/102 intentos terminales con fallos inyectados, deadlines completos y comparación ABBA reproducible. No reescribe metadata ni resultados históricos. |
| 42 | Adoptar checkpoints diarios recursivos e incrementalidad exacta en mercado, derivados, fundamentales, valoración, instituciones y eventos junto con lecturas filtradas/paginadas, identidad semántica separada de snapshot y acceso de features/explicación desacoplado. | Full/incremental Decimal y deltas 0/1/3 con revisión; cero resultados equivalentes añadidos, cero modelos ajenos hidratados, lecturas por IDs candidatos, lotes ≤256 y snapshots/lineage exactos. V1 se conserva hasta el bloque 43. No introduce ML, LLM ni paneles nuevos. |
| 43 | Migración completa y cutover con inventario, restore, rollback, activación de v2 y sincronización de instrumentos con el release. | Inventario real completo; escenarios sin delta, barra nueva, revisión, interrupción/reanudación, arranque/lectura y restore/rollback; v2 activo y recuperable; inicia una serie UTC ordinaria separada del backfill. |
| 44 | Backups cifrados e incrementales en Google Drive, reanudables, verificables y restaurables. Drive es destino de archivo, nunca almacenamiento DuckDB activo. | Restore desde Drive a destino vacío con hashes, conteos, PIT y lineage exactos. Dependencias, permisos y cifrado requieren PLAN explícito antes de BUILD. |
| 45 | Aceptación final, correcciones residuales de gates y limpieza estrechamente autorizada de herramientas y copias. | Q1–Q7 y gates obligatorios satisfechos; recuperación retenida; inventario y autorización de limpieza; bytes lógicos, archivos, espacio Linux y espacio `C:` medidos por separado. |

La adopción de #337 está acotada al formato v2 opt-in y conserva el formato 1 como default hasta
resolver el inventario y el cutover en el bloque 43. La decisión humana `AUTO` aplica sólo a este
Work Block: requiere BUILD completo, CI del SHA exacto y AUDIT independiente PASS antes de FINALIZE;
no adelanta la migración, el backup Drive ni la limpieza. Su transición propuesta es `ADVANCES`, no
cierre de DATA-CHASSIS. Q1–Q7 mantienen sus estados, comparadores y denominador canónicos. Cualquier
oportunidad opcional se registra en PLAN y no extiende la aceptación de #337.

## Gates de cierre que atraviesan los bloques

- Igualdad Decimal entre ejecución completa e incremental v2; checkpoints persistidos e invalidados
  por revisiones, cambios de fuente, período o versión.
- Selección point-in-time y aislamiento por activo, fuente, dominio, frecuencia y corte; sin inferir
  huecos internos por días sin barras.
- Páginas y lotes de hasta 256 modelos y cero hidratación de filas ajenas en consultas filtradas.
- Inventario completo de migración, restore y rollback antes de activar v2; backup de Drive restaurado.
- Features y explicación desacopladas del almacenamiento físico y reproducibles desde evidencia PIT.
- Q1–Q7 conservan su comparador y nivel de evidencia; los gates no numéricos también son obligatorios.

El bloque 40 sólo demuestra comportamiento aislado con fixtures. El cutover, la migración del
workspace permanente, el backup Drive, la observación operacional y cualquier liberación física de
espacio pertenecen a bloques posteriores y requieren la autoridad que cada PLAN declare.
