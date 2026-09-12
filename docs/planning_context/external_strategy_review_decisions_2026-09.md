# Decisiones de revisión estratégica externa — septiembre de 2026

`SEC-CORPUS-25` mantiene el corpus SEC como evidencia documental independiente: la adquisición
incremental de documentos primarios no se convierte en análisis fundamental, extracción de texto,
embeddings, ranking, señal ni recomendación. La política cubre sólo el último filing elegible por
form declarado, conserva Submissions como lineage y falla cerrado ante ambigüedad o corrupción.

La operación diaria prioriza trazabilidad y costo acotado: una rerun verifica almacenamiento antes
de Archives y sólo una accession nueva habilita un GET. La automatización queda local, con fuente
oficial SEC, un único writer y sin superficie HTTP adicional.

`SEC-CORPUS-26` responde a las prioridades humanas registradas sin ampliar la frontera: la actividad
declarada de Forms 3/4/5 y Schedules 13D/13G se adquiere de forma incremental y programada por emisor
SEC seleccionado, con un GET Submissions por ejecución, sin Company Facts, sin scraping y sin
convención de gestor. La selección declarada (`sec-declared-activity-selection-v1`) mantiene el
baseline histórico fuera de alcance, no avanza el watermark ante estado parcial, no reintenta
rechazos terminales versionados y declara backlog cuando el delta excede el límite por familia. La
familia 13F, la expansión de activos, cripto on-chain y BVL siguen fuera: cada una exige fuente,
licencia/retención, identidad y contrato propios antes de cualquier BUILD.

`SEC-CORPUS-27` sustituye la ausencia de un universo de gestores Form 13F por un artefacto local,
point-in-time y reproducible (`sec-13f-manager-universe-v1`) derivado exclusivamente de los Form 13F Data
Sets trimestrales oficiales de la SEC. Descubre gestores institucionales únicamente para CUSIP ya
declarados en el catálogo (AAPL / `037833100`), conserva el ZIP exacto por SHA-256 en el document store
y publica una selección determinista acotada a los 25 mayores gestores por valor reportado en el
período más reciente del dataset (`sec-13f-manager-universe-selection-v1`). No importa todavía filings 13F,
no declara correspondencias de clase, no altera Cazatiburones ni el catálogo y prepara la adquisición
dirigida de `SEC-CORPUS-28`.

`SEC-CORPUS-28` convierte ese universo en evidencia 13F real sin ampliar la frontera: selecciona una
página determinista de gestores ya seleccionados, realiza un único GET Submissions por gestor, importa
sólo los `13F-HR`/`13F-HR/A` del período de reporte objetivo aceptados al corte, reutiliza sin volver a
descargar los accessions ya materializados y enriquece la semántica integrada al mismo corte. El
`accession` del dataset queda como lineage de descubrimiento y nunca como autoridad de importación; el
backlog es exacto y declarado, y un fallo intermedio conserva el progreso ya persistido.

`SEC-CORPUS-29` cierra la costura evidencia→activo→observación manteniendo la disciplina anterior: la
coincidencia se prueba por **fila** (`sec-institutional-row-correspondence-v1`), con vigencia cerrada
al período reportado y lineage completo `snapshot → candidato → reporte → artefacto → fila`. No se
infiere por ticker, nombre de emisor, FIGI, ISIN ni similitud, no se extiende la vigencia fuera del
trimestre y no se inventa un fallback: la ausencia y la ambigüedad se declaran. La declaración humana
`instrument-correspondence-v1` se conserva como camino compatible, y la materialización
(`sec-institutional-observation-materialization-v1`) no realiza red y reporta estados explícitos en
lugar de cero sintético.

`SEC-CORPUS-30` integra la operación programada y reanudable del ciclo institucional Form 13F (#27 universo →
#28 adquisición dirigida → #29 correspondencia y observaciones) con una sola conexión writer, archivo de estado
atómico con checksum SHA-256 (`sec_institutional_cycle_state_v1.json`), presupuesto de red estricto (sondeo diario
de catálogo HTML, descarga ZIP sólo ante snapshot ausente, período/URL nuevo o expiración de 7 días; cero GET ZIP
en cache hit ordinario), outcomes rechazados terminales reutilizados y scheduler job `sec:institutional:13f-cycle`.

`SEC-CORPUS-31` cierra la profundidad histórica mínima sin autorizar backfill abierto: la ventana de dos cierres
adyacentes (`sec-institutional-history-cycle-v1` y `sec-institutional-history-state-v1`) selecciona exactamente
los dos períodos oficiales más recientes, persiste como máximo un ZIP faltante por intento en
`sec_institutional_history_state_v1.json`, intersecta los gestores comunes de ambos cierres, materializa ambas
páginas candidatas con una única revisión Submissions compartida, ejecuta las métricas, pesos y eventos
institucionales ya integrados sin cambiar fórmulas y reconcilia la outbox local antes de avanzar el cursor, bajo
el job adicional `sec:institutional:13f-history` (desfase 120 min). `RUNTIME-EFFICIENCY-5`/#223 ya retiró la
resolución implícita de activo del camino de lectura y `RUNTIME-EFFICIENCY-6` retira los contratos Apple
productivos `aapl-market-chart-v5` y `AaplDailyRunRequestSnapshot`, con un adaptador de compatibilidad
versionado para el estado operativo ya persistido. El siguiente bloque de `EQUITY-UNIVERSE` es
`RUNTIME-EFFICIENCY-7` —contratos cripto legados y defaults Apple de los resolvers del catálogo—, que sí
puede cerrar el ítem; después, `FUNDAMENTALS-COVERAGE` pasa a único `NEXT`.

## Afirmaciones externas stale, corregidas en vez de heredadas

Estas cuatro afirmaciones provienen de los documentos externos revisados y **no** describen el estado vivo.
Se registran aquí corregidas para que ninguna decisión se apoye en ellas:

- **«El 13F está bloqueado a `asset_id`». Falso.** `SEC-CORPUS-29` prueba la correspondencia **por fila**
  (`sec-institutional-row-correspondence-v1`) con vigencia cerrada al período reportado, y `SEC-CORPUS-30` y
  `SEC-CORPUS-31` la operan de forma programada y reanudable. Lo que permanece es la limitación real: sin
  correspondencia inequívoca no hay observación, y la ausencia nunca se convierte en cero.
- **«El Issue #178 sigue abierto». Falso.** #178 está `CLOSED` desde el 2026-09-12; el hallazgo de OOM fue
  atendido por `RUNTIME-EFFICIENCY-4`/#183 (`MERGED` el 2026-09-07), que acotó las lecturas del refresh diario
  Coinbase.
- **«El refresh Coinbase materializa sin límite». Falso.** `RUNTIME-EFFICIENCY-4`/#183 acotó esas lecturas; el
  refresh cripto opera por lotes acotados y reanudables bajo el mutex writer.
- **«Hay que reclasificar el score 60/40 y unificar el CLI». No adoptado aquí.** Ambas propuestas exigen su
  propio Work Block con frontera de capacidad declarada; ninguna es privilegio por activo y ninguna cabe en la
  retirada de contratos legados.

## Registro consolidado de las dieciocho recomendaciones pendientes

Cada fila clasifica una recomendación de la revisión estratégica externa como `DONE`, `PARTIAL`, `BUILD`,
`INTEGRATE`, `DEFER`, `BLOCKED` o `DROP`, con su razón y la evidencia viva que la sostiene. Esta tabla no
autoriza BUILD: toda ejecución exige su propio Work Block.

| # | Recomendación externa | Clasificación | Razón y evidencia viva |
| --- | --- | --- | --- |
| 1 | Personal frente a Historical Research Universe | `PARTIAL` | El producto es personal y local-first sobre un catálogo acotado de 37 identidades; un universo de investigación histórico survivorship-aware no existe y es condición de `PREDICTIVE-RESEARCH` (`DEFERRED`). |
| 2 | Política histórica por familia de datos | `PARTIAL` | Mercado diario, fundamentales SEC, macro y actividad declarada tienen retención y fuente declaradas y PIT verificado; cripto on-chain, BVL y noticias carecen todavía de política de retención y licencia. |
| 3 | Discovery de alto recall | `DEFER` | No hay corpus de descubrimiento ni etiqueta que lo evalúe; introducirlo sin PIT, licencia y medición contradice la separación de capas. |
| 4 | ML como ranker complementario y no gate absoluto | `DEFER` | Sin label versionado, baselines ni validación temporal fuera de muestra; pertenece a `PREDICTIVE-RESEARCH`. |
| 5 | Bypass por eventos materiales, watchlist manual y muestra exploratoria | `PARTIAL` | La watchlist persistente y las reglas descriptivas de candidatos ya existen; un bypass por eventos materiales exige contrato propio y no está autorizado. |
| 6 | Recall, precision, recall@K, miss rate, lead time y estabilidad | `DEFER` | Requiere un objetivo con etiqueta y una muestra fuera de muestra; sin eso cualquier métrica sería narrativa. |
| 7 | Missed-case analysis | `DEFER` | Depende de 6; hoy sólo existen diagnósticos y candidatos descriptivos auditables. |
| 8 | Labels, leakage audit, walk-forward, holdout, shadow y rollback | `DEFER` | Es la puerta declarada de `PREDICTIVE-RESEARCH`; no hay ningún label implementado. |
| 9 | Attention ranking frente a forecasting | `DEFER` | Ninguna de las dos capacidades existe; la elección exige evidencia comparada que hoy no existe. |
| 10 | LLM como proveedor intercambiable de interpretación | `PARTIAL` | El núcleo funciona sin LLM y la interpretación opcional está prevista como módulo posterior; no hay integración ni proveedor configurado. |
| 11 | Tesis, antítesis, risk review, escenarios y evidence audit | `DEFER` | Exige interpretación LLM con citas sobre evidencia ya calculada; pertenece a `Fase 12` y no es dependencia del núcleo. |
| 12 | Benchmark PIT de LLMs antes de integrar uno | `DEFER` | Sin benchmark point-in-time previo no se autoriza integrar un LLM; es requisito de la frontera de IA cualitativa. |
| 13 | Política de admisión de fuentes por pregunta, PIT, licencia, retención, mantenimiento y utilidad medible | `PARTIAL` | Cada fuente integrada declara proveedor, `source_id`, feed, ajuste y limitaciones (incluida la cobertura IEX de un solo mercado); el registro formal por pregunta de investigación no existe todavía. |
| 14 | Prioridad de historia diaria sobre intraday masivo | `DONE` | El producto es diario por defecto; el intraday se limita a una ventana acotada de BTC-USD bajo demanda y nunca reemplaza la historia diaria. |
| 15 | Backfills acotados y reanudables | `DONE` | Mercado, SEC documental, actividad declarada y ciclo 13F operan por lotes acotados, reanudables, con backlog declarado y outcomes terminales reutilizados. |
| 16 | Infraestructura ligera y rechazo de complejidad anticipada | `DONE` | DuckDB y Parquet locales, una sola conexión writer, sin servicios ni colas externas; sólo Pydantic y DuckDB como dependencias de producción. |
| 17 | Clasificación `BUILD`/`INTEGRATE`/`DEFER`/`DROP` | `INTEGRATE` | Este propio registro aplica la taxonomía ampliada a las dieciocho recomendaciones pendientes, con razón y evidencia viva por fila. |
| 18 | Medición del progreso por utilidad, cobertura, runtime y resultados en vez de pantallas o documentos | `PARTIAL` | Existen cobertura de universo, conteos creados/reutilizados, presupuestos de lectura y benchmarks de runtime; falta un panel único de utilidad medible y no se promete por número de pantallas. |

### Registro previo de la trayectoria SEC-CORPUS

| Recomendación | Decisión | Evidencia/razón | Ruta futura |
| --- | --- | --- | --- |
| Corpus reciente y auditable antes de IA | INTEGRATE | Submissions fresco y revisiones v2 verificadas | SEC-CORPUS |
| GET dedicado de Submissions | BUILD | Evita mezclar Company Facts en el refresh documental | SEC-CORPUS-25 |
| Adquisición automática de actividad declarada | BUILD | Importadores, normalizador y métricas existían pero sólo se invocaban a mano | SEC-CORPUS-26 |
| Universo oficial y acotado de gestores 13F | BUILD | Form 13F Data Sets oficiales SEC limitados a CUSIP de catálogo | SEC-CORPUS-27 |
| Adquisición 13F por gestor y correspondencia | `DONE` | `SEC-CORPUS-28` y `SEC-CORPUS-29` ya adquirieron por gestor con acceptance-time PIT y probaron la correspondencia por fila | Integrado |
| Adquisición 13F dirigida y reanudable desde el universo | BUILD | El universo ya persistido permite paginar gestores con un Submissions por gestor y reanudación exacta | SEC-CORPUS-28 |
| Correspondencia CUSIP↔activo y observaciones 13F | BUILD | La coincidencia exacta de CUSIP por fila permite una prueba aislada sin inventar vigencia corporativa | SEC-CORPUS-29 |
| Operación programada del ciclo institucional | BUILD | Opera en background bajo el scheduler existente con una sola conexión writer y progreso atómico | SEC-CORPUS-30 |
| Ventana de dos cierres adyacentes y cadena derivada | BUILD | La operación manual del cierre más reciente impedía materializar métricas, eventos y outbox comparables | SEC-CORPUS-31 |
| Expansión de activos y cripto on-chain | DEFER | Faltan fuente, licencia/retención, PIT y coste por familia medidos | Plan futuro de datos |
| UI nueva | DEFER | La capacidad opera por scheduler y la UI permanece congelada | Work Block futuro P0/P1 |
| Backfill suplementario e histórico | DEFER | Faltan presupuesto, cobertura y licencia medidos | Plan futuro de corpus |
| Extracción de texto, embeddings o LLM | DROP | No son necesarios para adquisición ni evidencia primaria | Requiere frontera nueva |
| Score o recomendación | DROP | Contradice separación de evidencia y análisis | No autorizado |
