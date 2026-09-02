# Semántica institucional 13F as-filed

`sec-institutional-holdings-semantics-v2` es una evidencia local, append-only y sin
normalización financiera derivada de un reporte 13F v1 ya persistido. No sustituye el contrato v1,
no asigna `asset_id` y no produce observaciones, métricas, señales, candidatos ni recomendaciones.

Cada bundle conserva la cabecera de la presentación y todas las filas de `informationTable` en su
ordinal original. CUSIP, clase, `putCall`, cantidad/tipo, discreción, `otherManager`, votos y FIGI
si aparece son dimensiones declaradas: dos filas iguales por CUSIP y clase permanecen distintas. Las
enmiendas se consultan tal como fueron presentadas; no se componen como una cartera efectiva.

El importe `value_as_reported` es `Decimal` exacto con `value_unit=sec_13f_as_reported` y
`monetary_scale_status=unresolved`. Por tanto no afirma USD, miles, comparabilidad entre filings ni
conversión de principal a acciones. Ausencia, código no soportado y referencia de gestor no resuelta
se exponen como limitaciones separadas, sin defaults silenciosos.

## Linaje y PIT

La identidad UUID5 de un artefacto usa el `report_id` v1, ambas revisiones documentales, parser y
schema; la identidad de fila usa el artefacto y el ordinal. `available_at` hereda la aceptación SEC;
`parsed_at` queda fuera de la identidad para que una reejecución equivalente reutilice el bundle.
El enriquecedor no consulta red: relee y verifica los dos XML oficiales por hash. Bytes ausentes o
alterados, revisiones incompatibles, XML inseguro, tamaño mayor de 50 MiB o más de 100000 filas
fallan antes de persistir el bundle.

## Uso explícito

Primero importa el 13F v1 al workspace elegido. Luego indica sólo reportes conocidos y un corte UTC:

```bash
python scripts/enrich_sec_institutional_holdings.py --workspace /tmp/sec-13f \
  --filer-cik 1067983 --report-id <REPORT-UUID> --known-at 2026-08-31T00:00:00Z
python scripts/query_sec_institutional_semantics.py --workspace /tmp/sec-13f \
  --filer-cik 1067983 --report-id <REPORT-UUID> --known-at 2026-08-31T00:00:00Z \
  --cusip 037833100 --offset 0 --limit 100
```

La query abre el workspace en solo lectura y distingue `missing` de `not_enriched`. Por reporte
entrega `total_rows`, `matching_rows`, `truncated` y sólo una página: una página nunca implica una
cartera completa. El verificador de backup vuelve a derivar cada bundle desde los XML persistidos y
rechaza cualquier divergencia.
## Proyección de composición posterior

Los bundles semánticos as-filed conservan literalmente la metadata necesaria para una política de
selección posterior (`is_amendment`, `amendment_number`, `amendment_type`, totales declarados,
filas y `available_at`). La política `sec-13f-composition-policy-v1` los consume en modo
read-only; no modifica esta evidencia ni convierte metadata ausente en cero. Véase
`cazatiburones_institutional_composition.md` para sus estados y límites.
