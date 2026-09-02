# Cambios institucionales descriptivos

La consulta local read-only compara cierres 13F consecutivos de un mismo gestor al `known_at` solicitado. Expone deltas de cantidad y valor, entrada, salida, concentración y baseline robusto (mediana, MAD y percentil) con `Decimal` exacto. Ausencia no equivale a cero y una muestra insuficiente es `not_evaluable`.

No persiste resultados ni modifica evidencia; tampoco produce umbrales, anomalías, eventos, candidatos, alertas, señales, recomendaciones ni comparación con precio.
## Nota sobre completitud 13F

`InstitutionalChangeService` conserva su comportamiento histórico y no clasifica originales,
enmiendas ni completitud. Para una selección point-in-time explícita de artefactos as-filed debe
consultarse primero la política `sec-13f-composition-policy-v1`, documentada en
`cazatiburones_institutional_composition.md`. Esta nota no migra ni altera la capa de cambios.
