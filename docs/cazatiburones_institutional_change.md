# Cambios institucionales descriptivos

La consulta local read-only compara cierres 13F consecutivos de un mismo gestor al `known_at` solicitado. Expone deltas de cantidad y valor, entrada, salida, concentración y baseline robusto (mediana, MAD y percentil) con `Decimal` exacto. Ausencia no equivale a cero y una muestra insuficiente es `not_evaluable`.

No persiste resultados ni modifica evidencia; tampoco produce umbrales, anomalías, eventos, candidatos, alertas, señales, recomendaciones ni comparación con precio.
