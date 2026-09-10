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

| Recomendación | Decisión | Evidencia/razón | Ruta futura |
| --- | --- | --- | --- |
| Corpus reciente y auditable antes de IA | INTEGRATE | Submissions fresco y revisiones v2 verificadas | SEC-CORPUS |
| GET dedicado de Submissions | BUILD | Evita mezclar Company Facts en el refresh documental | SEC-CORPUS-25 |
| Adquisición automática de actividad declarada | BUILD | Importadores, normalizador y métricas existían pero sólo se invocaban a mano | SEC-CORPUS-26 |
| 13F por gestor | DEFER | Requiere universo explícito de managers y correspondencia de instrumento | SEC-CORPUS-27 |
| Expansión de activos y cripto on-chain | DEFER | Faltan fuente, licencia/retención, PIT y coste por familia medidos | Plan futuro de datos |
| UI nueva | DEFER | La capacidad opera por scheduler y la UI permanece congelada | Work Block futuro P0/P1 |
| Backfill suplementario e histórico | DEFER | Faltan presupuesto, cobertura y licencia medidos | Plan futuro de corpus |
| Extracción de texto, embeddings o LLM | DROP | No son necesarios para adquisición ni evidencia primaria | Requiere frontera nueva |
| Score o recomendación | DROP | Contradice separación de evidencia y análisis | No autorizado |
