# Composición y completitud institucional 13F

`sec-13f-composition-policy-v1` es una proyección local, efímera y de sólo lectura sobre los
artefactos semánticos 13F as-filed. Resuelve cada `(manager_cik, report_period)` únicamente con los
artefactos cuyo `available_at <= known_at`; `computed_at` no participa. La unidad seleccionada se
identifica por el `artifact_id` y el `accession` ya persistidos, sin crear una identidad, una
cartera ni un resultado persistente nuevos.

La entrada por artefacto conserva literalmente `is_amendment`, `amendment_number`,
`amendment_type`, `declared_entry_total`, `declared_value_total` y las filas as-filed. El total
observado de entradas es el número de filas y el total observado de valor es la suma exacta
`Decimal` de `value_as_reported`. Si no hay filas semánticas disponibles, ambos totales observados
son ausentes: una ausencia nunca se convierte en cero.

Los tipos de enmienda admitidos son `RESTATEMENT` y `NEW HOLDINGS ENTRIES`. Un valor distinto o
ausente para una enmienda no recibe un valor por defecto; genera `ambiguous`. La cadena también
falla cerrada por número ausente o inválido, por repetición, por orden temporal contradictorio o
por empate de `available_at` entre artefactos distintos. Una cadena incompleta o sin original se
reporta como `insufficient`, sin cierre efectivo.

## Estados

| Estado | Significado |
| --- | --- |
| `original_complete` | Se seleccionó un original visible y sus dos totales declarados coinciden exactamente con los observados. |
| `amended` | Se seleccionó una enmienda visible de tipo declarado reconocido y la cadena/totales son coherentes. La razón distingue restatement de nuevas entradas. |
| `insufficient` | No existe evidencia visible suficiente para evaluar el período o la cadena está incompleta. |
| `not_evaluable` | Hay un artefacto seleccionado, pero falta un total declarado u observado, o los totales declarados y observados no coinciden. |
| `ambiguous` | Hay conflicto de identidad temporal, período, tipo de enmienda o cadena; nunca se elige un cierre efectivo. |

El resultado incluye los dos totales y sus comparaciones sólo cuando existe un artefacto efectivo.
Una comparación es `null` cuando uno de sus operandos es ausente; nunca implica que el valor sea
cero.

## Límites y uso

Esta proyección no compone enmiendas como una cartera efectiva, no produce posiciones, métricas,
calidad, score, ranking, señal, alerta, candidato, veredicto ni recomendación. Tampoco escribe en
el workspace, evidencia, observaciones o métricas. Se consulta con:

```bash
.venv/bin/python scripts/query_sec_institutional_composition.py \
  --workspace /ruta/al/workspace \
  --manager-cik 1067983 \
  --known-at 2026-09-01T00:00:00Z
```

El workspace es explícito; la consulta no depende del directorio actual ni muestra credenciales.
La disponibilidad y la fidelidad de las filas provienen de la capa semántica institucional
existente. Esta política no migra `InstitutionalChangeService`: esa capa sigue con su selección
histórica y no expresa los estados de completitud descritos aquí.

Las métricas persistidas de capa 3 consumen esta resolución sin cambiar su motor ni su guard
read-only; sus fórmulas y omisiones se documentan en `cazatiburones_institutional_metrics.md`.
