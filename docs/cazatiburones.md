# Cazatiburones

`Cazatiburones` es el dominio futuro para describir actividad de participantes relevantes. No es un
sinónimo de volumen alto ni una señal automática de compra o venta. Actualmente solo existe el
espacio arquitectónico; todavía no calcula métricas ni diagnósticos.

## Evidencia admisible

- **Form 13F:** posiciones declaradas por gestores institucionales sujetos a reporte. Una variación
  entre cierres trimestrales describe un cambio de tenencia; no revela la fecha, precio o motivo de
  las operaciones que lo produjeron.
- **Forms 3, 4 y 5:** propiedad y transacciones reportadas por insiders. Deben conservarse el código
  de transacción, propiedad directa o indirecta y fecha real de disponibilidad.
- **Schedules 13D y 13G:** propiedad beneficiaria relevante y sus enmiendas. Deben distinguirse la
  naturaleza activa o pasiva declarada y los cambios de participación.
- **Actividad de mercado:** solo podrá utilizar cobertura consolidada o una fuente cuyo alcance
  permita la inferencia concreta. El volumen parcial de Alpaca IEX queda expresamente excluido.

Fuentes oficiales de referencia:

- <https://www.sec.gov/rules-regulations/staff-guidance/division-investment-management-frequently-asked-questions/frequently-asked-questions-about-form-13f>
- <https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets>
- <https://www.sec.gov/search-filings/edgar-search-assistance/using-edgar-research-investments>

## Salidas y límites

Las salidas previstas son cambios de posiciones reportadas, entradas y salidas, concentración,
transacciones de insiders y cambios de propiedad beneficiaria, siempre con evidencia independiente.
No se agregará todo en un "score de tiburones", no se reconstruirán operaciones inexistentes y no se
presentará una publicación tardía como información conocida antes de su `available_at`.

Para cripto se necesitará otro contrato: movimientos on-chain, concentración, treasury y flujos de
exchanges tienen identidades y limitaciones distintas. Las etiquetas heurísticas de wallets nunca se
tratarán como identidad institucional confirmada sin evidencia.
