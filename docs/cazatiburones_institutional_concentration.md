# Concentración declarada institucional 13F

`sec-13f-concentration-policy-v1` proyecta localmente y en sólo lectura la concentración
de un único cierre 13F efectivo de un gestor. No crea `MetricResult`, observaciones, evidencia ni
ninguna identidad persistida: su unidad de dominio es `(manager_cik, report_period, known_at,
effective_artifact_id)`, no un activo.

## Universo y selección

El universo es exactamente el conjunto de filas as-filed del artefacto que
`sec-13f-composition-policy-v1` resuelve como cierre efectivo. Incluye opciones cuando el propio
Form 13F las incluye; no separa acciones y opciones porque hacerlo requeriría otro universo y otro
denominador. Sólo los cierres `original_complete` y `amended` calculan magnitudes. Los estados
`insufficient`, `not_evaluable` y `ambiguous` se omiten con un motivo tipado y sin magnitudes.

Todas las filas provienen del mismo `effective_artifact_id` y `effective_accession`. Una fila del
artefacto superado por una enmienda no participa. Más de una fila para la misma posición declarada
`(cusip, title_of_class, put_call)` omite todo el cierre: no suma ni elige por discreción, porque esa
agregación sería otra decisión de representación.

## Fórmulas y precisión

Para cada fila, el valor y el total del cierre reciben `sec-13f-monetary-policy-v1` con el
`accepted_at` del mismo artefacto. El denominador es el total de cierre efectivo; cada peso es
`valor_de_fila / total_de_cierre`. La política expone:

- recuento de posiciones declaradas;
- mayor peso declarado;
- acumulado de los cinco y diez mayores pesos, sólo si existen al menos cinco o diez posiciones;
- HHI: suma exacta de los cuadrados de todos los pesos declarados, con `Context(prec=28)`.

El cociente no cambia con la escala, pero la precisión declarada sí: cierres aceptados antes de
`2023-01-03` mantienen calidad `partial`. Un total ausente, cero, o un cierre sin filas, se omite;
nunca se reemplaza por cero ni se divide por defecto. Una fila declarada con valor cero sí participa
como peso cero.

## Límites

No compone una cartera efectiva de enmiendas, no compara períodos ni gestores, y no produce
percentiles, umbrales, rankings, scores, veredictos, candidatos, alertas, señales ni recomendaciones.
La consulta local recibe `manager_cik`, `known_at` y un workspace explícito; abre el almacenamiento
sólo en modo lectura y no depende del directorio de trabajo.
