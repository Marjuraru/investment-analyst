# Decisiones de revisión estratégica externa — septiembre de 2026

`SEC-CORPUS-25` mantiene el corpus SEC como evidencia documental independiente: la adquisición
incremental de documentos primarios no se convierte en análisis fundamental, extracción de texto,
embeddings, ranking, señal ni recomendación. La política cubre sólo el último filing elegible por
form declarado, conserva Submissions como lineage y falla cerrado ante ambigüedad o corrupción.

La operación diaria prioriza trazabilidad y costo acotado: una rerun verifica almacenamiento antes
de Archives y sólo una accession nueva habilita un GET. La automatización queda local, con fuente
oficial SEC, un único writer y sin superficie HTTP adicional.

| Recomendación | Decisión | Evidencia/razón | Ruta futura |
| --- | --- | --- | --- |
| Corpus reciente y auditable antes de IA | INTEGRATE | Submissions fresco y revisiones v2 verificadas | SEC-CORPUS |
| GET dedicado de Submissions | BUILD | Evita mezclar Company Facts en el refresh documental | SEC-CORPUS-25 |
| UI nueva | DEFER | La capacidad opera por scheduler y la UI permanece congelada | Work Block futuro P0/P1 |
| Backfill suplementario e histórico | DEFER | Faltan presupuesto, cobertura y licencia medidos | Plan futuro de corpus |
| Extracción de texto, embeddings o LLM | DROP | No son necesarios para adquisición ni evidencia primaria | Requiere frontera nueva |
| Score o recomendación | DROP | Contradice separación de evidencia y análisis | No autorizado |
