# Eventos descriptivos de actividad declarada

Este artefacto persistido proyecta métricas ya existentes de actividad declarada a evaluaciones,
eventos y candidatos de revisión. Es descriptivo: no infiere intención de compra o venta, no es una
señal, no emite recomendaciones y no consulta proveedores.

Las seis reglas fijas comparan las tres métricas de actividad contra cero, conservan `Decimal`,
`available_at` e identidades de observaciones. Sólo `DataQuality.VALID` puede producir un evento.
Un valor cero es `not_met`; una calidad no válida es `not_evaluable`.

La materialización crea un snapshot JSON append-only bajo el directorio processed del workspace. Su
identidad depende del activo, corte, política, MetricResult y eventos, no del reloj de ejecución.
Los candidatos aplican un cooldown descriptivo de 86400 segundos por activo, regla y participante.
La consulta exige el `snapshot_id`; no selecciona por reloj, mtime ni UUID arbitrario.
