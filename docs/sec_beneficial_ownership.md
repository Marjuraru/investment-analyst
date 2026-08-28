# SEC Schedule 13D/13G: propiedad beneficiaria

Esta vertical conserva evidencia descriptiva y point-in-time de los Schedules `SC 13D`, `SC 13D/A`,
`SC 13G` y `SC 13G/A`. Su `source_id` es
`sec-edgar:beneficial-ownership-13d-13g`; no comparte contratos, identidades ni consultas con la
vertical de Sección 16.

El importador sólo examina `filings.recent` del snapshot de Submissions ya persistido. Para cada
accession conserva el locator declarado, consulta el manifest oficial y acepta exclusivamente un
único XML de nivel superior cuya raíz sea `edgarSubmission`. HTML, texto, XML con DTD/ENTITY, una
raíz inesperada o cero/múltiples XML generan un outcome rechazado con hash, tamaño, locator, URL y
motivo tipado. No se aplica parsing heurístico, saneamiento ni reparación.

Los outcomes y statements son `RawRecord` append-only. `available_at` es exactamente
`SecFiling.accepted_at`; `retrieved_at` y `parsed_at` siguen registrando la materialización local.
La reimportación de los mismos bytes conserva identidades UUID5 y no añade filas. Las consultas por
`known_at` sólo devuelven statements disponibles al corte, ordenados del más reciente al más antiguo,
con total y truncamiento explícitos.

El statement almacena únicamente valores declarados que pueden extraerse del XML estructurado:
compañía sujeto, declarante, fecha del evento, cantidad y porcentaje. No normaliza CUSIP,
instrumentos, clases ni posiciones de 13F; tampoco produce score, señal, candidato o recomendación.

## CLI local

```bash
SEC_USER_AGENT='Nombre contacto@example.com' .venv/bin/python scripts/import_sec_beneficial_ownership.py \
  --workspace /ruta/workspace --asset-id equity:us:aapl --form 'SC 13G'

.venv/bin/python scripts/query_sec_beneficial_ownership.py \
  --workspace /ruta/workspace --asset-id equity:us:aapl --known-at 2026-08-28T00:00:00Z
```

El importador requiere una identidad SEC conforme. Un `reportDate` vacío o ausente se conserva como
`report_date: null`; no se sustituye por `filingDate`, `accepted_at`, cero ni una fecha sintética.
