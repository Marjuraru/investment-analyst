# Cartera institucional pública efectiva

`sec-13f-effective-public-holdings-policy-v1` compone, exclusivamente en memoria y bajo un
`known_at` explícito, el conjunto de filas 13F públicas efectivas de un gestor y período.
No persiste una cartera, no normaliza valores a precios de mercado, no asigna `asset_id`
ni crea métricas, score, señal o recomendación. No debe confundirse con una cartera económica
completa ni con propiedad efectiva total.

## Semántica de composición

La proyección consume la política de composición `sec-13f-composition-policy-v2`:
- El filing original inicia el conjunto base.
- Una enmienda oficial `RESTATEMENT` visible opera como `replacement` y sustituye íntegramente
  el conjunto original y los suplementos anteriores.
- Una enmienda oficial `NEW HOLDINGS` posterior a la base efectiva opera como `supplement` y
  suplementa el conjunto agregando sus filas as-filed.
- Cualquier enmienda anterior al último `RESTATEMENT` queda descartada de la proyección.
- Cada contribuyente efectivo debe ser completo y auditable: sus totales declarados de entradas
  y valor deben coincidir exactamente con la suma y recuento de sus filas observadas. De lo
  contrario, la proyección resuelve `not_evaluable` con razón `contributor_incomplete`.

## Lineage y limitaciones

La consulta se ejecuta sobre `ApplicationRuntime` con almacenamiento estrictamente de sólo lectura
(`WorkspaceAccessMode.READ_ONLY`), dejando el workspace intacto.

Cada fila conserva su trazabilidad original (`source_artifact_id`, `source_accession`, `source_row_id`,
`source_row_number`, `contributor_index`). Las filas as-filed nunca se deduplican ni se agregan por CUSIP.
La escala monetaria se reporta explícitamente como `monetary_scale_status: unresolved` (no se totaliza monetariamente).
Limitaciones regulatorias como `confidential_omitted`, reportes combinados (`report_type`) y otros gestores
incluidos se preservan explícitamente en `disclosure_limitations`.

Referencia oficial: [SEC Form 13F Frequently Asked Questions](https://www.sec.gov/rules-regulations/staff-guidance/division-investment-management-frequently-asked-questions/frequently-asked-questions-about-form-13f).

Ejemplo de uso CLI:

```bash
python scripts/query_sec_institutional_effective_holdings.py \
  --workspace /ruta/workspace \
  --manager-cik 0001067983 \
  --report-period 2024-12-31 \
  --known-at 2025-02-14T00:00:00Z
```
