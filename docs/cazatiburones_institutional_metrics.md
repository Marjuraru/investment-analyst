# Métricas institucionales descriptivas 13F

`cazatiburones-institutional-metrics-v1` persiste deltas de acciones y valor reportados, de
acciones subyacentes y valor de opciones, y el ratio de cambio de acciones. Cada métrica compara
sólo dos cierres consecutivos resueltos (`original_complete` o `amended`) al `known_at` pedido.

La identidad UUID5 incluye activo, clave, gestor, posición declarada, períodos, corte e inputs; no
incluye el reloj de cómputo ni el valor. Filas duplicadas, posiciones ausentes, campos ausentes,
cierres no resueltos y ratio con previo cero se omiten con motivo tipado. No suma filas, no trata
ausencia como cero y no construye una cartera efectiva. Las observaciones monetarias parciales
mantienen calidad `partial`; el importe principal queda excluido por unidad no comparable.

El peso declarado dentro de un cierre efectivo se documenta y persiste separadamente en
[`cazatiburones_institutional_weight.md`](cazatiburones_institutional_weight.md). Los eventos
descriptivos derivados se documentan en
[`cazatiburones_institutional_events.md`](cazatiburones_institutional_events.md). No hay
concentración, score, señal, recomendación ni modificación de evidencia u observaciones.

## Operación programada sobre dos cierres (`SEC-CORPUS-31`)

La ventana de dos cierres adyacentes ejecuta esta implementación sin cambiar fórmulas, pesos ni
umbrales: tras materializar los dos cierres comparables de un gestor común, el ciclo programado
`sec:institutional:13f-history` crea o reutiliza las métricas ya definidas y expone los estados no
evaluables con su motivo tipado. Si no existe una posición comparable, el resultado declara por qué y
no fabrica evento ni notificación.
