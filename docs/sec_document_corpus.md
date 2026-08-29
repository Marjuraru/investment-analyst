# Corpus documental SEC primario

El corpus conserva evidencia documental oficial, no analytics, señales, recomendaciones, decisiones
ni ejecución. Cada filing, documento lógico y revisión tiene una identidad UUID5 distinta. La revisión
referencia bytes completos por SHA-256 en `storage/data/documents/sha256/`; su RawRecord sólo contiene
metadata, URL oficial y lineage al snapshot Submissions que demostró CIK, accession, form y path.

En `sec-document-revision-v1`, `available_at` conserva la semántica histórica de primera
recuperación oficial demostrada y coincide con `retrieved_at`. En
`sec-document-revision-v2`, `available_at` es la disponibilidad pública: deriva exactamente de
`filing.accepted_at`; `retrieved_at` conserva por separado la recepción local. Los replays filtran
`available_at <= known_at` en el índice RawRecord antes de materializar metadata y sólo seleccionan
evidencia v2; el contador `legacy_records_excluded` hace explícita la historia v1 excluida. Una
ausencia devuelve `missing`, nunca cero ni contenido inventado.

Los documentos presentados por un declarante que no es un activo del catálogo usan la revisión
hermana `sec-filer-document-revision-v1`. Conservan el mismo `SecFiling` y
`SecLogicalDocument`, pero sustituyen el vínculo `asset_id` por el `filer_cik` ya declarado en el
filing. Sus namespaces UUID5 y su codec son disjuntos: una revisión de declarante no se convierte en
`sec-document-revision-v1/v2`, y esas revisiones históricas permanecen sin cambios. La familia de
declarante nace directamente con `available_at == filing.accepted_at` y no tiene una variante de
disponibilidad basada en recuperación.

La familia financiera v1 se limita a `10-K`, `10-K/A`, `10-Q`, `10-Q/A`, `20-F`, `20-F/A`, `40-F`
y `40-F/A`. El corpus compartido también reconoce las familias documentales de Sección 16,
13D/13G y `13F-HR`/`13F-HR/A`; cada vertical mantiene contratos derivados y source IDs separados.
El único provider es SEC EDGAR oficial: Submissions ya persistido descubre filing/path y
`www.sec.gov/Archives` entrega el documento primario con HTTPS, host, redirect, tamaño y hash
verificados. No hay fallback ni extracción de hechos, fragments, XBRL, métricas o diagnósticos.

## Operación temporal

Configura una identidad SEC no secreta sólo en el proceso y usa un workspace temporal:

```bash
export SEC_USER_AGENT="Investment Analyst contact@example.com"
python scripts/import_sec_document_corpus.py --workspace /tmp/sec-corpus --asset-id equity:us:aapl --form 10-K
python scripts/query_sec_document_corpus.py --workspace /tmp/sec-corpus --asset-id equity:us:aapl --known-at 2026-01-01T00:00:00Z --form 10-K
```

La consulta abre almacenamiento de solo lectura, no crea directorios, no abre writer y no llama a
SEC. `--read-content` verifica y lee explícitamente los bytes, pero nunca los imprime. Backup/restore
conserva IDs, blobs y replay sin una segunda pasada completa: la validación documental se integra en
el escaneo RawRecord paginado existente.
