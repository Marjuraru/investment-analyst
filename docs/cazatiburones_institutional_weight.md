# Peso declarado institucional 13F

Cada resultado `cazatiburones.institutional.declared_*_position_value_weight` es el valor de una
posición declarada dividido por el valor total del mismo cierre 13F efectivo. El cierre se resuelve
con `sec-13f-composition-policy-v1`; no compara períodos ni construye una cartera efectiva.

El denominador se suma desde las filas as-filed del artefacto semántico efectivo, no desde las
observaciones visibles de un activo. Después recibe `sec-13f-monetary-policy-v1` con el
`filing.accepted_at` de ese artefacto, exactamente como el numerador. Antes de 2023-01-03 el
resultado conserva calidad `partial`; el cociente no oculta esa precisión declarada.

La métrica de acciones consume exclusivamente `institutional_reported_fair_value` y la de opciones
`institutional_option_fair_value`. Ambas usan unidad `ratio`, pero no se afirma que los resultados
sumen uno: pueden faltar posiciones sin correspondencia. No existe peso por acciones, porque las
cantidades entre emisores no son aditivas.

Se omite, sin sustituir por cero, cuando el cierre no es `original_complete` o `amended`, falta o
vale cero el total, falta una posición o campo, o hay filas duplicadas. Cada resultado conserva el
artefacto, accession, fila, revisiones y hashes de cover e information table y versión monetaria en
sus parámetros; la identidad también incorpora el corte `known_at` y nunca `computed_at` ni el valor.

Esto no calcula concentración, recuento de posiciones, mayor peso, HHI, ranking, score, señal ni
recomendación.
