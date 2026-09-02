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
[`cazatiburones_institutional_weight.md`](cazatiburones_institutional_weight.md). No hay
concentración, score, señal, recomendación ni modificación de evidencia u observaciones.
