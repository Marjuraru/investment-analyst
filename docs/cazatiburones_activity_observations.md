# Observaciones normalizadas de actividad declarada (capa 2)

Este módulo materializa la capa 2 del modelo de capas de `docs/cazatiburones.md` —observación
normalizada de tenencia, propiedad o transacción— para Forms 3/4/5 (insiders) y Schedules 13D/13G
(propiedad beneficiaria). Proyecta un valor ya declarado y ya persistido a un
`NormalizedObservation` append-only; no deriva, no compara, no agrega y no crea eventos ni
candidatos. Form 13F queda explícitamente fuera de alcance.

## Catálogo de campos (`sec-declared-activity-observation-v1`)

| `field_name` | familia | atributo de origen | unidad | atributo de fecha declarada |
| --- | --- | --- | --- | --- |
| `insider_transaction_shares` | insider | `entry.shares` | shares | `entry.transaction_date`, si ausente `statement.period_of_report` |
| `insider_transaction_price_per_share` | insider | `entry.price_per_share` | USD/share | ídem |
| `insider_shares_owned_following` | insider | `entry.shares_owned_following` | shares | ídem |
| `beneficial_shares_owned` | beneficial_ownership | `statement.shares_beneficially_owned` | shares | `statement.event_date` |
| `beneficial_percent_of_class` | beneficial_ownership | `statement.percent_of_class` | percent | `statement.event_date` |

El normalizador no emite ningún campo fuera de este catálogo. Cada definición declara también
`source_id` literal, `frequency=event`, `quality=valid`, formularios aplicables y limitaciones
explícitas; ver `src/investment_analyst/evidence/sec_declared_activity_observations/definitions.py`.

Para insiders, la fecha declarada usada resuelve primero `entry.transaction_date` (mapea a
`observed_at`) y, si esa entrada no la declara, `statement.period_of_report` (mapea a `period_end`,
pues describe el período reportado y no un instante exacto). Para propiedad beneficiaria la única
fuente declarada es `statement.event_date` (mapea a `observed_at`); no existe un período de
respaldo. Si ninguna fuente declarada aplicable tiene valor, no se genera observación.

## Identidad y trazabilidad

Una observación se identifica por (`source_id`, `statement_id`, `entry_id` cuando aplica,
`field_name`, `transformation_version`) mediante UUID5 sobre un namespace declarado. La identidad
excluye el reloj de normalización, el valor y cualquier snapshot: una reejecución sobre la misma
evidencia con un `normalized_at` distinto reutiliza la identidad existente; una enmienda —con
`statement_id` distinto porque cambia de revisión y accession— produce una identidad distinta y
conserva íntegra la original.

`raw_record_id` es literalmente el del `OwnershipStatement` o `BeneficialOwnershipStatement` de
origen: la observación no crea un `RawRecord` nuevo. `source.record_key` registra, en JSON
canónico, `statement_id`, `entry_id` (o `null`), `field_name`, el atributo de fecha declarada
efectivamente usado y `transformation_version`, de modo que la observación es reconstruible hasta el
valor declarado original.

## Semántica point-in-time

`available_at` de cada observación es exactamente el `available_at` de la evidencia origen —la
aceptación SEC declarada por el documento—; nunca se sustituye por `normalized_at`, la fecha de
filing o la fecha de descarga. El servicio de normalización sólo considera evidencia con
`available_at <= known_at`; una consulta con `known_at` anterior no selecciona esa evidencia y no
genera observaciones para ella.

## Idempotencia y conflicto

Al reejecutar, la identidad ya existente se compara por contenido semántico —todo excepto el reloj
de normalización— antes de decidir si reutilizar o rechazar: si coincide, no se reescribe nada y se
cuenta como reutilizada; si el contenido declarado difiere, la ejecución falla explícitamente con un
error de identidad contradictoria y no escribe esa observación. Un fallo a mitad de ejecución
conserva las observaciones ya guardadas anteriormente en esa misma corrida: no existe rollback
global.

## Omisiones

Un valor declarado ausente nunca se convierte en `0` ni en un valor por defecto: se omite y se
cuenta con el motivo `missing_value`. Una fecha declarada ausente en la única fuente aplicable (o en
ambas, para los campos de insiders) también se omite y se cuenta con el motivo `missing_date`. El
resumen de ejecución (`DeclaredActivityObservationRunSummary`) expone `values_examined`,
`observations_generated`, `observations_created`, `observations_reused`, `skipped_total` y
`skipped_by_reason`, y valida internamente que esos conteos cuadren.

## Límites explícitos

- Sin cálculo, sin agregación, sin comparación entre declaraciones, sin delta, sin ratio, sin
  concentración, sin percentil ni baseline.
- Sin evento, sin candidato, sin score, sin veredicto, sin confianza, sin ranking, sin
  deduplicación ni cooldown.
- Form 13F queda bloqueado: sus posiciones conservan CUSIP y clase declarados sin `asset_id`
  verificado, y `NormalizedObservation.asset_id` es obligatorio.
- Insiders y propiedad beneficiaria permanecen en familias, `source_id` y `field_name` separados;
  ninguna ruta de este módulo los une entre sí, ni con 13F, ni con mercado, fundamentales o
  valoración.
- El servicio exige almacenamiento escribible; no escribe en ninguna tabla distinta de
  `normalized_observations`.

## Uso

`CazatiburonesActivityObservationsApplication.create_default().normalize(asset_id=..., known_at=...,
location=...)`, o el CLI `scripts/normalize_declared_activity_observations.py --asset-id
equity:us:aapl --known-at <ISO-8601 UTC> [--workspace <ruta> | --root <ruta>]`. Ambos abren
almacenamiento en modo lectura-escritura y devuelven el resumen de ejecución como JSON tipado.
