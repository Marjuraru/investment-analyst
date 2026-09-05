# Actividad declarada de insiders y propiedad beneficiaria

Esta consulta read-only calcula features descriptivas y point-in-time de Forms 3/4/5 y Schedules
13D/13G. La versión `declared-activity-features-v1` conserva fórmula, unidad, parámetros,
limitaciones, revisiones de evidencia y `available_at`; no persiste resultados ni modifica evidencia.

## Insiders

La unidad es (`asset_id`, `owner_cik`, `security_title`, `table`) y nunca agrega entre
participantes, instrumentos, tablas o emisores. Para cada transacción declarada,
`transaction_shares` y `post_holding` son las acciones declaradas y `prior_holding` es la tenencia
de la declaración anterior del mismo grupo; su unidad es acciones. `holding_delta_ratio` es
`(post_holding - prior_holding) / prior_holding`, sin unidad, y queda `missing` si la tenencia previa
es ausente o cero.

`acquisition_count` y `disposition_count` son conteos unitarios derivados sólo de los códigos `A` y
`D` declarados. `clustered_transaction_count` cuenta transacciones del mismo grupo dentro de
`window_days=30` desde la fecha de transacción declarada. `participant_recurrence` cuenta
declaraciones distintas del participante disponibles al corte. La primera declaración de una serie
declara las métricas que requieren precedente como `not_evaluable`; no se interpola una declaración
ausente. Sin fecha de transacción, las features que la requieren y `filing_delay_days` quedan
`missing`.

## Propiedad beneficiaria

La unidad es (`asset_id`, `subject_cik`, `reporting_person_cik`) y las comparaciones se limitan a
declaraciones consecutivas del mismo declarante. `delta_percent_of_class` y
`delta_shares_beneficially_owned` son `actual - anterior`, con unidades puntos porcentuales y
acciones, respectivamente. `threshold_appearance` y `threshold_exit` son booleanos de presencia
declarada, nunca un umbral inferido. `declared_nature` conserva literalmente `form` (`SC 13D`,
`SC 13D/A`, `SC 13G` o `SC 13G/A`) sin interpretar intención. La primera declaración expone la falta
de historia con `not_evaluable`; un porcentaje o cantidad ausente es `missing`, no cero.

## Tiempo, revisiones y límites

`is_amendment` usa literalmente el sufijo `/A`. `filing_delay_days`, en días completos, es
`available_at.date - fecha_de_hecho_declarada`: fecha de transacción para insiders y `event_date`
para 13D/13G. La salida conserva ambas fechas separadas. `available_at` es el máximo de las
declaraciones usadas y la consulta sólo devuelve evidencia disponible en `known_at`. Si original y
enmienda coexistentes no tienen una resolución declarable, la consulta rechaza la comparación en vez
de elegir una arbitrariamente.

No hay persistencia, umbrales, anomalías, eventos, candidatos, alertas, señales, recomendaciones,
comparación con precio de mercado ni ratio contra acciones en circulación.

## Ruta HTTP local

La consulta se expone en la interfaz web local en modo de solo lectura mediante:

```text
GET /api/v1/cazatiburones/declared-activity?asset_id=equity:us:aapl&known_at=2026-07-16T15:47:00Z
```

Parámetros soportados:
- `asset_id`: obligatorio; identifica un activo corporativo con configuración SEC en el catálogo.
- `known_at`: obligatorio; corte point-in-time UTC (ISO-8601).

Devuelve `DeclaredActivityQueryResult` (`asset_id`, `known_at`, `insider_features`, `beneficial_features`,
`total_statements`, `truncated`). Las características de insiders y propiedad beneficiaria se entregan
en listas separadas sin mezclar ni consolidar en una métrica única. Los valores de tipo Decimal se
serializan sin conversión a float para preservar precisión exacta.

Cualquier parámetro adicional, la omisión de parámetros obligatorios o activos sin configuración SEC
se rechazan con error HTTP 400 (`invalid_request`).
