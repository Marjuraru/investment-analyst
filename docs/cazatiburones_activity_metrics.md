# Métricas descriptivas persistidas de actividad declarada (capa 3)

Este módulo materializa la capa 3 del modelo de capas de `docs/cazatiburones.md` —métrica
descriptiva versionada— persistiendo como `MetricResult` un conjunto cerrado de métricas
derivadas de insiders (Forms 3/4/5) y propiedad beneficiaria (Schedules 13D/13G), calculadas
exclusivamente sobre `NormalizedObservation` de la capa 2 ya integrada. No crea eventos,
candidatos, umbrales, anomalías, score, veredicto ni recomendación. Form 13F queda explícitamente
fuera de alcance.

## Catálogo (`cazatiburones-activity-metrics-v1`)

| `metric_key` | familia | fórmula | unidad | campo de capa 2 consumido |
| --- | --- | --- | --- | --- |
| `cazatiburones.insider.holding_delta_ratio` | insider | `(post_holding - prior_holding) / prior_holding` | `ratio` | `insider_shares_owned_following` |
| `cazatiburones.beneficial.delta_percent_of_class` | beneficial_ownership | `percent_actual - percent_anterior` | `percentage_points` | `beneficial_percent_of_class` |
| `cazatiburones.beneficial.delta_shares_beneficially_owned` | beneficial_ownership | `shares_actual - shares_anterior` | `shares` | `beneficial_shares_owned` |

Cada métrica exige exactamente dos observaciones de capa 2 —la declaración inmediatamente
anterior y la actual del mismo grupo— ya leídas del almacenamiento. Ninguna métrica fuera de este
catálogo puede ser emitida por el motor ni persistida por el pipeline; ver
`src/investment_analyst/analytics/cazatiburones/activity_metric_definitions.py`.

## Agrupación y precedente

- Insiders agrupan por (`owner_cik`, `security_title`, `table`) sobre entradas de tipo
  `transaction`, ordenadas por (`transaction_date` o `period_of_report`, `available_at`,
  `entry_id`).
- Propiedad beneficiaria agrupa por (`subject_cik`, `reporting_person_cik`), ordenada por
  (`event_date` o `available_at.date()`, `available_at`, `statement_id`).
- El precedente es la declaración inmediatamente anterior del mismo grupo disponible al corte. La
  primera declaración de una serie no produce métrica y se cuenta con motivo
  `not_evaluable_no_precedent`.

## Identidad y trazabilidad

Un `MetricResult` se identifica por UUID5 sobre un namespace propio, con preimagen JSON canónica
de (`asset_id`, `metric_key`, `algorithm_version`, `unit`, `as_of`, `available_at`, `known_at`,
`parameters`, `input_observation_ids`), claves ordenadas, separadores compactos y `allow_nan=False`.
La identidad excluye `computed_at` y el valor calculado, e incluye `known_at`: una reejecución
sobre el mismo corte con un reloj distinto reutiliza la identidad existente; un corte posterior que
revela una declaración presentada tarde produce una identidad distinta y conserva íntegro el
resultado del corte anterior.

`input_observation_ids` son literalmente los dos `observation_id` de capa 2 leídos del
almacenamiento —nunca una identidad esperada asumida sin verificar—; desde ellos se llega a cada
`NormalizedObservation`, a su `raw_record_id`, al `RawRecord` persistido y al valor declarado
original.

## Semántica point-in-time

`available_at` de cada resultado es el máximo de los `available_at` de las dos observaciones
consumidas —la aceptación SEC declarada por la evidencia—; nunca se sustituye por `computed_at`, la
fecha de filing ni la fecha de descarga. El pipeline sólo considera statements con
`available_at <= known_at`; una consulta con `known_at` anterior no selecciona esa evidencia y no
produce métricas.

## Idempotencia y conflicto

Al reejecutar, una identidad ya existente se compara por contenido semántico —todo excepto el
reloj de cómputo— antes de decidir si reutilizar o rechazar: si coincide, no se reescribe nada y se
cuenta como reutilizada; si el contenido difiere, la ejecución falla explícitamente con un error de
identidad contradictoria y no escribe ese resultado. Un fallo a mitad de ejecución conserva las
métricas ya persistidas en esa misma corrida: no existe rollback global.

## Omisiones

| motivo | cuándo |
| --- | --- |
| `not_evaluable_no_precedent` | primera declaración de una serie |
| `missing_input_observation` | la observación de capa 2 esperada para el precedente o la actual no existe en el almacenamiento (valor o fecha declarados ausentes en origen) |
| `not_evaluable_zero_prior` | sólo `holding_delta_ratio`: la tenencia previa declarada es cero |

Un valor omitido nunca se convierte en `0` ni en un valor por defecto. El resumen de ejecución
(`ActivityMetricRunSummary`) expone `values_examined`, `metrics_generated`, `metrics_created`,
`metrics_reused`, `skipped_total` y `skipped_by_reason`, y valida internamente que esos conteos
cuadren.

## Límites explícitos

- Form 13F queda bloqueado: sus posiciones conservan CUSIP y clase declarados sin `asset_id`
  verificado, y `MetricResult.asset_id` es obligatorio.
- No se persisten `acquisition_count`, `disposition_count`, `clustered_transaction_count`,
  `participant_recurrence`, `is_amendment`, `filing_delay_days`, `threshold_appearance` ni
  `threshold_exit`: derivan de metadatos de declaración que la capa 2 no normaliza como
  observación, o son booleanas sin representación `Decimal` autorizada.
- Insiders y propiedad beneficiaria conservan `metric_key`, unidades y `parameters` separados;
  ninguna ruta de este módulo los une entre sí, ni con 13F, ni con mercado, fundamentales o
  valoración. No existe agregación entre métricas, participantes, emisores o familias.
- Sin `DiagnosticResult`, score, veredicto, confianza ni ranking.
- El pipeline exige almacenamiento escribible; no escribe en ninguna tabla distinta de
  `metric_definitions` y `metric_results`.

## Uso

`CazatiburonesActivityMetricsApplication.create_default().compute(asset_id=..., known_at=...,
location=...)`, o el CLI `scripts/compute_declared_activity_metrics.py --asset-id equity:us:aapl
--known-at <ISO-8601 UTC> [--workspace <ruta> | --root <ruta>]`. Ambos abren almacenamiento en modo
lectura-escritura, requieren la capa 2 ya normalizada mediante
`scripts/normalize_declared_activity_observations.py`, y devuelven el resumen de ejecución como
JSON tipado.
