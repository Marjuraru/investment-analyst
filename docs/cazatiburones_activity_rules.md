# Reglas descriptivas por formulario (Cazatiburones)

`definition_version: cazatiburones-activity-rules-v1`

Este documento describe el catálogo de reglas descriptivas que evalúa, de forma determinista,
read-only y point-in-time, las features ya integradas de Forms 3/4/5 (insiders), Schedules 13D/13G
(propiedad beneficiaria) y Form 13F (posiciones institucionales). Cada regla es un hecho declarado
observado: no persiste, no deduplica, no notifica, no agrega entre reglas, participantes, emisores
o familias, no puntúa y no recomienda. El contrato de resultado no admite `score`, `verdict`,
`confidence` ni `ranking`.

## Estado trivaluado

- `met`: la feature está `available` y satisface la condición declarada.
- `not_met`: la feature está `available` y no satisface la condición declarada.
- `not_evaluable`: la feature está `missing`, `not_evaluable`, o ausente de la evidencia reutilizada
  para esta regla; nunca se convierte en `not_met`. Cada `not_evaluable` declara un motivo.

`available_at` de cada evaluación es el de la feature consumida; `computed_at` nunca lo sustituye.
Una consulta con `known_at` anterior a esa disponibilidad no devuelve la evaluación.

## Insiders (Forms 3, 4, 5)

| `rule_id` | Feature | Condición | Unidad | Límites |
| --- | --- | --- | --- | --- |
| `insider-acquisition-declared` | `acquisition_count` | `== 1` | count | código de transacción declarado únicamente; indicador de una sola entrada, no una magnitud |
| `insider-disposition-declared` | `disposition_count` | `== 1` | count | código de transacción declarado únicamente; indicador de una sola entrada, no una magnitud |
| `insider-holding-reduced` | `holding_delta_ratio` | `< 0` | ratio | ausente para prior cero o ausente; exige una declaración previa para el mismo owner/security/table |
| `insider-clustered-activity` | `clustered_transaction_count` | `>= 2` | count | cuenta transacciones del mismo participante/security/table en una ventana declarada de 30 días calendario, incluyéndose a sí misma; describe agrupación temporal, no intención |
| `insider-filing-delay-beyond-threshold` | `filing_delay_days` | `> 2` | days | días calendario completos entre `available_at` y la fecha declarada de la transacción; puramente descriptivo, nunca un juicio de cumplimiento legal; ausente sin fecha declarada |

## Propiedad beneficiaria (Schedules 13D, 13G)

| `rule_id` | Feature | Condición | Unidad | Límites |
| --- | --- | --- | --- | --- |
| `beneficial-threshold-appearance` | `threshold_appearance` | `== true` | boolean | presencia declarada únicamente; exige una declaración previa para el mismo par sujeto/reportante |
| `beneficial-threshold-exit` | `threshold_exit` | `== true` | boolean | presencia declarada únicamente; exige una declaración previa para el mismo par sujeto/reportante |
| `beneficial-percent-increase` | `delta_percent_of_class` | `> 0` | percentage_points | sólo declaraciones consecutivas del mismo par sujeto/reportante |
| `beneficial-percent-decrease` | `delta_percent_of_class` | `< 0` | percentage_points | sólo declaraciones consecutivas del mismo par sujeto/reportante |

## Posiciones institucionales (Form 13F)

| `rule_id` | Feature | Condición | Unidad | Límites |
| --- | --- | --- | --- | --- |
| `institutional-position-entry` | `entry` | `== true` | boolean | ver "Limitación de identidad por posición" abajo |
| `institutional-position-exit` | `exit` | `== true` | boolean | ver "Limitación de identidad por posición" abajo |
| `institutional-position-concentration` | `position_concentration` | `> 0.10` | ratio | posición individual más grande declarada sobre el total de cartera declarado; ausente cuando `declared_value_total` es nulo o cero |
| `institutional-robust-percentile-high` | `robust_percentile` | `> 0.90` | ratio | requiere una serie histórica de al menos `MINIMUM_BASELINE_SAMPLE` (3) observaciones construida por `institutional_change_engine.robust_baseline`; ver limitación siguiente |

### Limitación de identidad por posición

`InstitutionalChangeService.query` devuelve `InstitutionalChangeResult.metrics` como una tupla
plana: cada posición cuyo estado cambia entre dos cierres consecutivos añade sus propios
`delta_quantity`, `delta_value`, `entry` y `exit` a esa misma tupla, sin conservar el CUSIP ni la
clase de la posición. Por eso:

- `entrada` y `salida` se declaran a nivel de manager+período: `true` cuando **cualquier** posición
  de la comparación declarada aparece o desaparece, respectivamente. No identifican qué posición
  cambió y no son un score ni un ranking entre valores: es un hecho existencial sobre el filing, no
  una agregación ponderada.
- `delta_quantity` y `delta_value` **no** se exponen como reglas: al repetirse por posición sin
  identidad, cualquier combinación numérica entre ellas (suma, promedio, comparación) constituiría
  una agregación entre emisores distintos, prohibida por invariante. Unir 13F con un emisor
  exigiría además la correspondencia de instrumento, que permanece fuera de alcance.

### Limitación del percentil robusto

`institutional_change_engine.robust_baseline` existe y está probado de forma unitaria, pero
`InstitutionalChangeService.query` sólo compara cierres consecutivos por pares (`compare`) y nunca
invoca `robust_baseline`. En consecuencia, `robust_median`, `robust_mad` y `robust_percentile`
nunca aparecen en `InstitutionalChangeResult.metrics` hoy: la regla `institutional-robust-percentile-high`
es siempre `not_evaluable` con el mínimo declarado citado en su motivo, hasta que un Work Block
futuro conecte esa serie histórica. Se declara aquí para catalogar la regla de forma
forward-compatible, no para fabricar disponibilidad que no existe.

### Formulario declarado para 13F

`InstitutionalChangeResult` no conserva el formulario literal de la presentación (`13F-HR` o
`13F-HR/A`) ni el indicador de enmienda. Cada evaluación institucional declara `form="13F-HR"` de
forma estática por esta razón; no distingue enmiendas en esta capa. `revision_ids` queda vacío para
estas evaluaciones por el mismo motivo: el contrato reutilizado no expone identificadores de
revisión, sólo `available_at`.

## Fuera de alcance

Sin persistencia, deduplicación, cooldown, histéresis, notificación, outbox ni candidato. Sin panel
ni interfaz. Sin motor de anomalías estadísticas, modelo, SHAP, calibración, shadow mode ni
probabilidad. Sin score, ranking, veredicto, confianza ni recomendación, y sin combinación con
mercado, fundamentales o valoración. Sin unión entre 13F y un emisor mediante correspondencia de
instrumento, y sin ratio contra acciones en circulación o precio de mercado.
