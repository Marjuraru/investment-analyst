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

La cartera de órdenes no forma parte de las fuentes actuales. Quotes, trades o profundidad futura
pertenecen a un contrato de microestructura separado y tampoco identifican por sí mismos al
participante institucional.

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

## Pipeline híbrido local

```text
SEC/SMV autorizado
       |
       v
Raw + normalización PIT -> DuckDB/Parquet -> features locales
                                                   |
                         +-------------------------+-------------------------+
                         |                                                   |
               reglas/estadística robusta                       modelo local aceptado
                         |                                          + TreeSHAP
                         +-------------------------+-------------------------+
                                                   |
                                      evento/candidato trazable
                                                   |
                              JSON condensado + citas, si hay presupuesto
                                                   |
                                       narrativa LLM opcional
```

DuckDB calcula ventanas, deltas, ratios y matrices; Parquet almacena snapshots grandes. El LLM no
calcula anomalías, correlaciones, SHAP ni probabilidades y no recibe la historia completa.

### Features admisibles

- transacción insider frente a tenencia previa y acciones en circulación;
- agrupación temporal de transacciones declaradas y recurrencia por participante;
- cambio de porcentaje en 13D/13G y naturaleza declarada del formulario;
- delta, entrada, salida y concentración trimestral en 13F;
- enmienda, demora del filing y calidad/completitud de la correspondencia del instrumento.

Cada feature conserva fórmula, unidad, inputs, período y `available_at`. Ninguna feature convierte
un filing tardío en actividad observada en la fecha de la operación.

### Motor de anomalías

1. comenzar con umbrales descriptivos, medianas/MAD, percentiles y reglas por formulario;
2. evaluar un método no supervisado solo si el baseline deja un problema medible;
3. usar XGBoost o LightGBM exclusivamente cuando haya un label y objetivo defendibles;
4. comparar candidatos mediante purged walk-forward y holdout cronológico;
5. calcular SHAP localmente solo para el modelo seleccionado y los candidatos activados;
6. ejecutar en shadow mode antes de crear alertas visibles.

Un detector no supervisado entrega `anomaly_score` o percentil. `P(anomalía)` solo se publica si
un clasificador fue calibrado fuera de muestra. El umbral de activación se versiona y se elige por
precision/recall, falsos positivos y presupuesto; `P > 0,85` puede ser una configuración inicial
evaluada, pero no una constante arquitectónica.

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
- evidencia local point-in-time integrada sólo para XML estructurado de `filings.recent`; la
  vinculación de CUSIP, instrumentos y clases permanece fuera de esta etapa.
- diferencia entre fecha del evento y fecha de disponibilidad.

### Etapa T3 — posiciones institucionales

- Form 13F y sus enmiendas;
- importación por CIK de gestor explícitamente declarado, sin incorporarlo al catálogo de activos;
- evidencia point-in-time integrada para `13F-HR` y `13F-HR/A` de `filings.recent`, limitada a
  portada e information table XML estructuradas;
- posiciones por trimestre, clase, CUSIP y valor reportado, conservadas sin correspondencia a
  `asset_id`;
- entradas, salidas y variaciones entre dos cierres, pendientes de una correspondencia verificada;
- concentración descriptiva por posición y cartera, pendiente de la misma correspondencia;
- demora del reporte claramente visible.

Una variación trimestral no se presenta como operación fechada ni se compara con precio sin alinear
el corte point-in-time.

### Etapa T4 — extensiones separadas

- participaciones significativas y comunicaciones SMV para BVL, si la fuente y licencia lo
  permiten;
- contratos on-chain para cripto, sin reutilizar categorías de insiders o 13F;
- fuentes adicionales solo con identidad, cobertura y disponibilidad documentadas.

## Integración con screening, interfaz e IA

La correspondencia CUSIP declarada es evidencia separada y point-in-time. Puede proyectar
posiciones institucionales existentes hacia un activo ya catalogado, pero no cambia la evidencia
13F, no crea identidad de emisor por inferencia y no alimenta un score, diagnóstico, recomendación
ni instrucción de ejecución.

Cazatiburones tendrá una sección propia en la ficha del activo y una línea temporal. Sus reglas
podrán crear candidatos por hechos explícitos —por ejemplo, un nuevo Schedule 13D o una transacción
insider declarada—, pero no se combinarán con mercado o fundamentales en un score.

La IA se incorpora después de normalizar y validar los filings. Podrá resumir notas, comparar
enmiendas y relacionar el evento con métricas existentes, siempre con citas. No identificará
motivaciones, beneficiarios reales no declarados ni una oportunidad de inversión por sí sola.

La puerta de llamada exige simultáneamente evidencia nueva y completa, candidato local activado,
deduplicación, cooldown y presupuesto. El paquete incluye valores originales, resultado del modelo
o probabilidad calibrada cuando existan, baseline, principales SHAP, limitaciones, IDs de evidencia
y fragmentos citables. El objetivo inicial es hasta 800 tokens de evidencia condensada y una salida
breve; también se limitan tokens facturados totales, llamadas y coste diario. Una llamada fallida no
pierde el evento.

## Validación mínima

- fixtures por formulario y enmienda;
- identidad estable e idempotencia;
- rechazo de correspondencias ambiguas de CUSIP, clase o emisor;
- reconstrucción antes y después del `available_at`;
- conservación de notas, propiedad indirecta y códigos de transacción;
- ausencia explícita, nunca convertida en cero;
- replay sin evidencia futura;
- purged walk-forward y prohibición de `KFold` aleatorio para modelos;
- calibración antes de nombrar una salida como probabilidad;
- comparación contra baseline, estabilidad por período y shadow mode;
- identidad local verificable `expected_value + ΣSHAP = salida del modelo` en la escala declarada y dentro de tolerancia;
- deduplicación de eventos y candidatos;
- fallo tardío sin pérdida del progreso anterior;
- smoke real en workspace temporal antes de tocar el permanente.

Referencias de alcance:

- [SEC Insider Transactions Data Sets](https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets)
- [SEC Form 13F Data Sets](https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets)
- [Alpaca: IEX frente a SIP](https://docs.alpaca.markets/us/docs/historical-stock-data-1)
