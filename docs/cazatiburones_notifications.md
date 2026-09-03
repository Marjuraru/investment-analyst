# Notificación local de candidatos Cazatiburones

## Propósito y frontera

`cazatiburones-notification-outbox-v1` es una entrega local durable de candidatos descriptivos
ya persistidos por las verticales de actividad declarada e institucional 13F. No evalúa reglas,
no recalcula métricas, no consulta proveedores ni lee artefactos 13F crudos. Tampoco convierte un
candidato en señal, predicción, recomendación, score, veredicto, ranking o percentil.

La unidad de dominio es `(family, candidate_id)`, donde `family` es `activity` o `institutional`.
Las familias conservan contratos, procedencia e identidades separadas. La outbox es un artefacto
distinto de `candidate_notifications.py` y no comparte su estado, cooldown ni directorio.

## Contrato de entrega

Cada `CazatiburonesNotification` usa `schema_version: cazatiburones-notification-v1` y
`channel: local_app`. Conserva `family`, `notification_id`, `candidate_id`, `event_id`,
`metric_result_id`, `snapshot_id`, activo, regla, métrica, `algorithm_version`, unidad, valor `Decimal`,
`available_at`, `created_at` y las observaciones de entrada. `created_at` es exactamente el
`available_at` del evento: el reloj de reconciliación no participa en el contenido, orden ni
identidad.

La notificación institucional conserva además `manager_cik`, `report_period`,
`prior_report_period`, `cusip`, `title_of_class` y `put_call`. La notificación de actividad no
acepta esa procedencia. En eventos de actividad, que históricamente no guardan el algoritmo de la
métrica como campo propio, se conserva un `algorithm_version` presente en los parámetros del
evento o, de forma determinista, la `policy_version` del snapshot de eventos.

`notification_id` es UUID5 de un namespace propio sobre `family|v1|candidate_id`. El acuse es
append-only: `acknowledgement_id` es UUID5 de otro namespace propio sobre
`notification_id|pending|acknowledged|recorded_at ISO UTC|local_user`. `recorded_at` es una
entrada explícita del operador y el segundo acuse de una notificación es idempotente.

## Reconciliación y persistencia

La reconciliación enumera snapshots mediante métodos read-only de los dos repositorios, resuelve
cada candidato dentro de su propio snapshot y proyecta sólo `status == "eligible"`. Los candidatos
`suppressed` se ignoran y nunca se encolan. Un candidato repetido en cortes distintos conserva una
sola entrega; se elige de forma determinista la primera procedencia por corte y `snapshot_id`, sin
reescribir una entrega ya persistida.

El estado `cazatiburones-notification-outbox-state-v1` valida identidades únicas, familias
separadas, referencias de acuse, orden determinista y transiciones únicas. Se carga fail-closed:
un JSON malformado o una divergencia semántica bajo la misma identidad produce un error tipado y
no repara ni sobrescribe historia. Las nuevas escrituras usan un archivo temporal en el mismo
directorio y `os.replace` atómico.

La aplicación abre la evidencia del workspace con `READ_ONLY`. Sólo escribe en la ruta de outbox
que el operador entrega explícitamente. Los tres CLI exigen una localización explícita (`--workspace`
o `--legacy-root`) y `--outbox-state`; no derivan la ruta de outbox del layout del workspace ni
dependen del directorio de trabajo actual.

## Límites

- No hay transporte, red, correo, webhook, scheduler, servidor ni endpoint HTTP.
- No se modifica la outbox analítica ni su contrato.
- No se fusionan las familias ni se agregan candidatos en un artefacto común de análisis.
- No se modifican snapshots, observaciones, métricas, eventos ni el schema DuckDB.
- El smoke usa exclusivamente un workspace y una outbox temporales; el workspace permanente no se
  abre.
