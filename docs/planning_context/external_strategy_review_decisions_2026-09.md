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
el job adicional `sec:institutional:13f-history` (desfase 120 min). La retirada definitiva de los residuos de
privilegio por ticker y la observación del runtime antes de ampliar activos o proveedores quedan en
`RUNTIME-EFFICIENCY-5`, el siguiente ítem de `EQUITY-UNIVERSE`; la retirada de los contratos Apple
legados `aapl-market-chart-v5` y `AaplDailyRunRequestSnapshot` queda como Work Block posterior separado.

| Recomendación | Decisión | Evidencia/razón | Ruta futura |
| --- | --- | --- | --- |
| Corpus reciente y auditable antes de IA | INTEGRATE | Submissions fresco y revisiones v2 verificadas | SEC-CORPUS |
| GET dedicado de Submissions | BUILD | Evita mezclar Company Facts en el refresh documental | SEC-CORPUS-25 |
| Adquisición automática de actividad declarada | BUILD | Importadores, normalizador y métricas existían pero sólo se invocaban a mano | SEC-CORPUS-26 |
| Universo oficial y acotado de gestores 13F | BUILD | Form 13F Data Sets oficiales SEC limitados a CUSIP de catálogo | SEC-CORPUS-27 |
| Adquisición 13F por gestor y correspondencia | DEFER | Requiere Submissions por gestor, acceptance PIT, XML y correspondencia | SEC-CORPUS-28 |
| Adquisición 13F dirigida y reanudable desde el universo | BUILD | El universo ya persistido permite paginar gestores con un Submissions por gestor y reanudación exacta | SEC-CORPUS-28 |
| Correspondencia CUSIP↔activo y observaciones 13F | BUILD | La coincidencia exacta de CUSIP por fila permite una prueba aislada sin inventar vigencia corporativa | SEC-CORPUS-29 |
| Operación programada del ciclo institucional | BUILD | Opera en background bajo el scheduler existente con una sola conexión writer y progreso atómico | SEC-CORPUS-30 |
| Ventana de dos cierres adyacentes y cadena derivada | BUILD | La operación manual del cierre más reciente impedía materializar métricas, eventos y outbox comparables | SEC-CORPUS-31 |
| Expansión de activos y cripto on-chain | DEFER | Faltan fuente, licencia/retención, PIT y coste por familia medidos | Plan futuro de datos |
| UI nueva | DEFER | La capacidad opera por scheduler y la UI permanece congelada | Work Block futuro P0/P1 |
| Backfill suplementario e histórico | DEFER | Faltan presupuesto, cobertura y licencia medidos | Plan futuro de corpus |
| Extracción de texto, embeddings o LLM | DROP | No son necesarios para adquisición ni evidencia primaria | Requiere frontera nueva |
| Score o recomendación | DROP | Contradice separación de evidencia y análisis | No autorizado |
