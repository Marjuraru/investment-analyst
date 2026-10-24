# Reincorporación selectiva de v1 y de las semanas de transición

PLAN posterior al arranque v2 nuevo aprobado el 2026-10-21. No autoriza importar ni borrar ahora.
Autoridad presente: Issue #353 (`DATA-CHASSIS-47`), base `012bbc5e`, rama
`codex/data-chassis-47`. READ-1 queda absorbido en 47; no se publica ni ejecuta aparte. La
selección/histórico de 48 sigue futura. Autoridad previa: Issue #349 (`DATA-CHASSIS-46`), base `b5e714a`, y el
[registro de continuidad](data_chassis_continuity_plan.md). DATA-CHASSIS permanece NEXT y esta
secuencia no declara Q1–Q7 `MET`.

La ruta obligatoria es 46 → 47 → 48 → 49: 46 formaliza inventario y selección; 47 cubre el archivo
recuperable y ejecuta el piloto; 48 recupera selectivamente por consumidor con un manifiesto sellado
y un writer único; 49 retira físicamente y cierra. Los tramos 47–49 no son Issues activos ni
autoridad ejecutable anticipada.

## Objetivo y frontera

Conservar toda la historia original en archivo recuperable; incorporar al workspace activo sólo
lo que tenga un consumidor definido y todas sus dependencias. “No necesario en el activo” significa
ARCHIVE_ONLY, no permiso para destruir. No equiparar redescarga con preservación: revisiones,
available_at original, receipts, outbox/acks y estado local pueden no ser reproducibles.

## Inventario de partida y entregable previo a cualquier importación

Una tabla paginada por raíz/UUID/familia/activo/fuente/frecuencia/rango/corte, cantidades, bytes,
manifest/checksum, procedencia y recuperabilidad. Fuentes mínimas: v1 final tras stop, F5 parcial,
W/W2/G y complements/objetos remotos; workspace nuevo y sus revisiones desde el arranque. Usar
servicios/repositorios existentes, un reader bajo presupuesto y sin abrir el v1 que aún escriba.
Identificar qué cubre cada backup y las últimas semanas NO cubiertas, sin inferir cobertura por fecha.

Cada entrada recibe KEEP_ACTIVE, IMPORT_LATER, ARCHIVE_ONLY o CONFLICT, con motivo/consumidor,
dependencias, coste estimado, precedencia y evidencia. Son estados del plan de selección; no nuevos
campos de modelos financieros ni autoridad para mutar schema. UNKNOWN impide retiro.

## Política de selección

| Familia | Selección para activo | Historia que se conserva en archivo |
| --- | --- | --- |
| Raw y documentos oficiales | Evidencia de observaciones/resultados elegidos, documentos consultables seleccionados y receipts de intervalos necesarios | Resto original, incluida procedencia y revisiones |
| Observaciones | Ventanas de watchlist y análisis definidos + warm-up y revisiones PIT necesarias | Series fuera del uso declarado |
| Métricas/diagnósticos/snapshots | Artefactos referenciados y sus dependencias transitivas, con identidad original | Recomputaciones/historia no consumida; no deduplicar semántica reescribiendo IDs |
| Checkpoints incrementales | Sólo compatibles con activo/fuente/versión/prefijo verificado | Incompatibles, para trazabilidad; recalcular desde inputs si procede |
| Preferencias | Configuración vigente reaplicada mediante servicio público | Historial de preferencias, no copiar estados por overwrite |
| Scheduler/outbox/acks/intentos | No reintroducir como trabajo pendiente ni reenviar notificaciones antiguas | Historia consultable y receipts originales |
| Backups/controles | Referencias necesarias para demostrar recuperación/linaje | Manifests, journals, certificados, revocaciones y código productor |

El rango no es “últimos N días” universal: lo determina el consumidor, su warm-up y sus contratos.
Toda exclusión explicita qué consulta quedaría sin cobertura. Ninguna purga de catálogo ni cambios
de fórmulas/fuentes para reducir volumen. Orden por valor para el analista y coste, no por tamaño sólo.

## Orden futuro de ejecución y gates

1. **Archivo final verificable.** Con v1 cerrado, ligar su imagen final y las novedades de las últimas
   semanas a almacenamiento externo mediante backup incremental/streaming compatible. Reutilizar
   objetos ya certificados; no repetir 676 volúmenes. Si el formato no permite cubrir ese delta,
   declarar el hueco y presupuestar una solución explícita, nunca marcarlo respaldado por W2.
   Restore independiente de lo necesario para demostrar cobertura antes de cualquier retiro local.
2. **Plan de selección cerrado.** Tabla anterior, deduplicación por identidad/hash exactos, conflictos
   separados; estimar crecimiento, spill, tiempos y lote máximo. Identificar cierre transitivo completo
   de lineage/PIT. No importar primero y decidir después qué conservar.
3. **Piloto scratch no vacío.** Una vertical de mercado y otra documental/fundamental seleccionadas
   de las capacidades existentes, con solape entre historia y datos v2 nuevos; clocks distintos,
   revisión, conflicto, identidad repetida, fallo tras página y restart. Comparar consultas antes/
   después y todos los cortes PIT esperados. No contaminar el workspace activo para probar conflictos.
4. **Importación progresiva.** Sólo después de Work Block específico con Capability Boundary
   Declaration: APIs actuales de importación histórica no se presumen aptas para escribir sobre un
   v2 LIVE activo. Resolver por adelantado representación HISTORICAL/LIVE, preservación de IDs y
   aislamiento; mantenimiento de un writer con checkpoints y continuación, nunca copia de DB sobre
   el activo. Rechazar mismo ID con contenido distinto; no renombrar históricos como LIVE ni cambiar
   available_at/received_at por la fecha de importación. No rechazar automáticamente IDs tardíos.
5. **Verificación y publicación de la selección.** Fuente seleccionada⊆destino por conjuntos/hash
   canónico, conteos y lineage; resultados y revisiones nuevas v2 intactos. Publicar cobertura, rango
   y ausencias sólo tras verificación durable. Un único backup sucesor reutiliza bytes idénticos del
   parent y se valida antes de publicar; no se crea un backup por lote ni se bloquean consultas por
   un mínimo universal de 25 GB. No prometer lectura de historia que aún no está incorporada.
6. **Cierre/retención.** Lista de raíces cuyo último consumidor terminó, recuperación externa probada,
   coste físico medido y recibo de lifecycle. Después retirar payloads redundantes, conservando índices
   de archivo y evidencia. Cierre DATA-CHASSIS sólo cuando criterios vigentes se satisfagan o una
   decisión de alcance explícita y compatible sea publicada; no declarar Q1–Q7 MET por selección menor.

## Límites y prioridad

No se activa importación automática al arrancar v2. No se descargan décadas para “rellenar”.
Los lotes se eligen por watchlist, análisis pedidos y trazabilidad; toda fuente excluida conserva
motivo. Una mejora opcional, refactor general o proveedor nuevo no amplía estos lotes.

Siguiente PLAN debe producir una única capacidad cohesiva con rutas/hash de base, manifest y
prueba de recuperación, desde main y métricas frescas. Esta secuencia no crea seis bloques por
adelantado ni autoriza operadores a saltar las fronteras HISTORICAL/LIVE.
