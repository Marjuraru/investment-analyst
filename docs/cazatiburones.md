# Cazatiburones

`Cazatiburones` es el dominio previsto para describir actividad declarada de participantes
relevantes. No es un sinónimo de volumen alto ni una señal automática de compra o venta.
Actualmente solo existe el espacio arquitectónico; todavía no calcula métricas ni diagnósticos.

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

## Identidades y modelo point-in-time

El dominio no se construirá sobre símbolos sueltos. Debe distinguir:

- emisor y clase de valor;
- persona insider y relación declarada con el emisor;
- propietario beneficiario y tipo de Schedule;
- gestor institucional, entidad declarante y número de archivo;
- CIK, accession number, CUSIP, ISIN y `asset_id` cuando exista una correspondencia verificada;
- fecha económica, fecha del período reportado, fecha de filing y `available_at`.

Una posición 13F sin correspondencia inequívoca permanece sin vincular; no se fuerza contra el
símbolo vigente. Enmiendas, correcciones y filings tardíos son revisiones distintas. Nunca se
atribuirá al mercado información antes de que el filing estuviera disponible.

Las capas previstas permanecen separadas:

1. documento y revisión SEC como `RawRecord`;
2. observación normalizada de tenencia, propiedad o transacción;
3. métrica descriptiva versionada;
4. evento o diagnóstico del dominio Cazatiburones;
5. regla de screening opcional que referencia evidencia exacta.

## Ruta de implementación

### Etapa T0 — corpus común SEC

- catálogo de formularios, filings y documentos por emisor o declarante;
- descarga reanudable, hash, accession number, enmiendas y texto direccionable;
- `event_at`, `published_at`, `available_at` y `retrieved_at`;
- búsqueda y línea temporal local;
- reutilización posterior por fundamentales, eventos e IA.

### Etapa T1 — insiders

- Forms 3, 4 y 5;
- códigos de transacción, derivado/no derivado, cantidad, precio reportado y naturaleza de
  propiedad;
- propiedad directa e indirecta y notas al pie;
- adquisiciones, disposiciones y cambios reportados sin inferir intención;
- comparación por insider, emisor y clase de valor.

Esta etapa es la primera vertical porque está ligada al emisor analizado, suele ser más oportuna que
13F y reutiliza el catálogo SEC ya disponible.

### Etapa T2 — propiedad beneficiaria

- Schedules 13D, 13G y enmiendas;
- porcentaje y cantidad declarados;
- naturaleza activa o pasiva según el formulario, sin reinterpretarla;
- aparición, modificación y salida de umbrales reportados;
- diferencia entre fecha del evento y fecha de disponibilidad.

### Etapa T3 — posiciones institucionales

- Form 13F y sus enmiendas;
- catálogo verificable de gestores y declarantes;
- posiciones por trimestre, clase, CUSIP y valor reportado;
- entradas, salidas y variaciones entre dos cierres;
- concentración descriptiva por posición y cartera;
- demora del reporte claramente visible.

Una variación trimestral no se presenta como operación fechada ni se compara con precio sin alinear
el corte point-in-time.

### Etapa T4 — extensiones separadas

- participaciones significativas y comunicaciones SMV para BVL, si la fuente y licencia lo
  permiten;
- contratos on-chain para cripto, sin reutilizar categorías de insiders o 13F;
- fuentes adicionales solo con identidad, cobertura y disponibilidad documentadas.

## Integración con screening, interfaz e IA

Cazatiburones tendrá una sección propia en la ficha del activo y una línea temporal. Sus reglas
podrán crear candidatos por hechos explícitos —por ejemplo, un nuevo Schedule 13D o una transacción
insider declarada—, pero no se combinarán con mercado o fundamentales en un score.

La IA se incorpora después de normalizar y validar los filings. Podrá resumir notas, comparar
enmiendas y relacionar el evento con métricas existentes, siempre con citas. No identificará
motivaciones, beneficiarios reales no declarados ni una oportunidad de inversión por sí sola.

## Validación mínima

- fixtures por formulario y enmienda;
- identidad estable e idempotencia;
- rechazo de correspondencias ambiguas de CUSIP, clase o emisor;
- reconstrucción antes y después del `available_at`;
- conservación de notas, propiedad indirecta y códigos de transacción;
- ausencia explícita, nunca convertida en cero;
- replay sin evidencia futura;
- deduplicación de eventos y candidatos;
- fallo tardío sin pérdida del progreso anterior;
- smoke real en workspace temporal antes de tocar el permanente.
