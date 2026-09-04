# Cazatiburones Institutional Events (13F) Persistidos

## Propósito y Alcance

Este documento define la especificación canónica y el contrato de persistencia de eventos descriptivos institucionales (13F) en la capa analítica de `investment-analyst`.

Los eventos institucionales derivan determinísticamente de las métricas institucionales de capa 3 (`cazatiburones-institutional-metrics-v1`) calculadas entre cierres declarados contiguos de formularios 13F-HR / 13F-HR/A ante la SEC. Cada evento representa la satisfacción de una regla fija y descriptiva sobre una métrica institucional, sin rankings, sin scores agregados, sin verdicts predictivos y sin recomendaciones operativas.

## Invariantes Fundamentales

1. **Puramente descriptivo**: Los eventos reportan exclusivamente incrementos o reducciones verificados en métricas institucionales declaradas.
2. **Point-in-time estricto**: La evaluación utiliza únicamente métricas disponibles en el corte temporal solicitado (`available_at <= known_at`).
3. **Identidades deterministas**: `event_id`, `candidate_id` y `snapshot_id` se generan mediante UUIDv5 sobre hashes JSON canónicos ordenados. Diferentes relojes de cómputo producen exactamente las mismas identidades.
4. **Persistencia atómica e inmutable**: Los snapshots se almacenan en archivos JSON bajo `processed/cazatiburones_institutional_events_v1/`. La re-materialización idéntica es idempotente; cualquier divergencia de contenido bajo la misma identidad falla cerrado (`InstitutionalEventRepositoryError`).
5. **Cooldown posicional**: Un evento aprobado impone un período de enfriamiento de 86,400 segundos (24 horas) para la misma posición declarada (`asset_id`, `rule_id`, `manager_cik`, `cusip`, `title_of_class`, `put_call`). Un evento subsiguiente dentro de dicha ventana genera un candidato `suppressed` con evidencia trazable del evento supresor.
6. **Separación de capas**:
   - `cazatiburones_activity_events_v1`: eventos derivados de formularios 3, 4 y 5 (insiders).
   - `cazatiburones_institutional_events_v1`: eventos derivados de formularios 13F (institucionales).
   - Ambas familias permanecen estrictamente desacopladas en directorios y contratos independientes.

## Catálogo Fijo de Reglas

La política `cazatiburones-persisted-institutional-events-v1` define exactamente 10 reglas fijas (dos por cada una de las 5 métricas de capa 3):

| Rule ID | Metric Key | Dirección | Unidad |
| :--- | :--- | :--- | :--- |
| `institutional-reported-shares-increased` | `cazatiburones.institutional.delta_reported_shares` | increased | shares |
| `institutional-reported-shares-reduced` | `cazatiburones.institutional.delta_reported_shares` | reduced | shares |
| `institutional-reported-fair-value-increased` | `cazatiburones.institutional.delta_reported_fair_value` | increased | USD |
| `institutional-reported-fair-value-reduced` | `cazatiburones.institutional.delta_reported_fair_value` | reduced | USD |
| `institutional-reported-shares-delta-ratio-increased` | `cazatiburones.institutional.reported_shares_delta_ratio` | increased | ratio |
| `institutional-reported-shares-delta-ratio-reduced` | `cazatiburones.institutional.reported_shares_delta_ratio` | reduced | ratio |
| `institutional-reported-fair-value-delta-ratio-increased` | `cazatiburones.institutional.reported_fair_value_delta_ratio` | increased | ratio |
| `institutional-reported-fair-value-delta-ratio-reduced` | `cazatiburones.institutional.reported_fair_value_delta_ratio` | reduced | ratio |
| `institutional-average-price-per-share-delta-ratio-increased` | `cazatiburones.institutional.average_price_per_share_delta_ratio` | increased | ratio |
| `institutional-average-price-per-share-delta-ratio-reduced` | `cazatiburones.institutional.average_price_per_share_delta_ratio` | reduced | ratio |

No existen umbrales configurables por el usuario ni heurísticas probabilísticas.

## Fronteras Negativas Explícitas

- NO se combinan métricas ni eventos en scores, ránkings, percentiles o semáforos.
- NO se infieren intenciones comerciales, señales de compra/venta ni recomendaciones.
- NO se agregan posiciones entre múltiples gestores institucionales en carteras efectivas conjuntas.
- NO se modifican registros históricos ya persistidos.

La entrega local deduplicada de los candidatos persistidos se define en
[`cazatiburones_notifications.md`](cazatiburones_notifications.md). Conserva la procedencia 13F
sin mezclarla con la familia de actividad declarada.
