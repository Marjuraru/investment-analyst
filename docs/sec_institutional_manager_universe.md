# Universo oficial y acotado de gestores SEC Form 13F

Este documento define la especificación, el pipeline de adquisición y la semántica point-in-time
del artefacto `sec-13f-manager-universe-v1` y la política de selección
`sec-13f-manager-universe-selection-v1`, implementados en `SEC-CORPUS-27`.

El objetivo exclusivo de este artefacto es proveer una cola de adquisición determinista, auditable y
acotada de gestores institucionales Form 13F para los activos que ya cuentan con un binding CUSIP en
el catálogo (`equity:us:aapl` → `037833100`). No genera observaciones de posiciones, no infiere
correspondencias de clase y no emite señales ni recomendaciones.

## Origen de datos oficial

- **Fuente:** SEC Form 13F Data Sets trimestrales publicados oficialmente por la SEC en
  `https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets`.
- **Acceso:** Cliente HTTPS exclusivo con cabecera `SEC_USER_AGENT` obligatoria.
- **Catálogo:** La URL oficial se inspecciona buscando enlaces relativos o absolutos bajo
  `/files/structureddata/data/form-13f-data-sets/` que cumplan el patrón
  `DDmonYYYY-DDmonYYYY_form13f.zip`. Se selecciona determinísticamente el período más reciente
  (`period_end` descendente, `period_start` descendente).
- **Límites de transporte:** Catálogo limitado a 2 MiB; archivo ZIP limitado a 160 MiB comprimidos.
  Cualquier redirección fuera de `sec.gov`, contenido no ZIP o respuesta no 200 falla cerrado.

## Integridad del archivo y parser seguro

El archivo ZIP se procesa en streaming y en memoria sin dependencias externas:

1. **Límites de archivo:**
   - Máximo 16 miembros en el archivo ZIP.
   - Máximo 1 GiB descomprimido acumulado.
   - Prohibición estricta de cifrado, extensiones ZIP64 no estándar y secuencias de path traversal (`..`, `/`).
2. **Tablas requeridas:**
   - Exige la presencia obligatoria de `SUBMISSION.tsv`, `COVERPAGE.tsv` e `INFOTABLE.tsv`.
   - Se validan encabezados conocidos y se falla cerrado ante columnas faltantes, tipos inválidos o delimitadores ambiguos.
3. **Filtrado por formulario y CUSIP:**
   - Solo se admiten formularios `13F-HR` y `13F-HR/A`. Se descartan expresamente `13F-NT` y `13F-NT/A`.
   - La tabla `INFOTABLE.tsv` se lee en streaming contra el conjunto de CUSIP activos declarados en el catálogo.
   - Se descartan filas cuyo CUSIP no esté en el catálogo. No se realizan uniones por ticker, emisor o similitud de texto.

## Almacenamiento y linaje

- **Document Store:** El ZIP completo e inalterado se almacena en `documents/sha256/<hash>.zip` con
  fsync atómico y verificación inmediata de integridad SHA-256.
- **Revisión RawRecord:** Se persiste bajo `source_id="sec-edgar:form-13f-data-set:manager-universe"` con:
  - `dataset_id`: derivado deterministicamente de URL, período y SHA-256.
  - `period_start` y `period_end`: fechas UTC del período trimestral del dataset.
  - `available_at`: exactamente `retrieved_at` (marca de tiempo UTC de la descarga local).
  - `event_time`: `period_end` a las 00:00:00 UTC.
- **Idempotencia:** Si se reejecuta la adquisición y los bytes del ZIP son idénticos, se reutiliza
  el blob y la revisión sin crear registros duplicados. Si el ZIP fue corregido en origen por la SEC,
  se genera una nueva revisión preservando la histórica.

## Política de selección (`sec-13f-manager-universe-selection-v1`)

Para cada activo con binding CUSIP:

1. Se determina el `PERIODOFREPORT` más reciente presente en el dataset para dicho activo.
2. Se agrupan las posiciones exactas de dicho período por `(asset_id, CIK, accession)`.
3. Se suma el valor reportado (`VALUE`) utilizando aritmética `Decimal` exacta en unidades
   declaradas (`usd_thousands_as_filed`).
4. Si un gestor presenta múltiples accessions para el mismo período (por ejemplo, enmiendas), se
   selecciona el accession de mayor valor reportado, desempatando por accession más reciente.
5. Los gestores candidatos se ordenan de forma determinista:
   - Valor reportado descendente.
   - Fecha de filing descendente.
   - CIK normalizado ascendente.
   - Accession number ascendente.
6. Se trunca el universo a un máximo de 25 gestores (`max_managers_per_asset = 25`).
7. El snapshot resultante (`sec-13f-manager-universe-snapshot-v1`) indica si hubo truncación, el total
   de gestores disponibles y la lista ordenada de gestores seleccionados.

## Semántica Point-in-Time (PIT)

- Los Form 13F Data Sets de la SEC contienen `FILING_DATE`, pero no contienen `acceptanceDateTime`.
- Por consiguiente, para garantizar la integridad PIT sin sesgo de anticipación, `available_at` es
  estrictamente igual a `retrieved_at`. `FILING_DATE` se registra como metadato as-filed, pero nunca
  sustituye el momento en que la información estuvo disponible localmente.
- Las consultas PIT aplican `available_at <= known_at`.
- Cada consulta verifica la existencia del blob en el document store y la correspondencia exacta de su SHA-256.

## Herramientas CLI

- `scripts/refresh_sec_institutional_manager_universe.py`: Descarga y procesa el dataset oficial más
  reciente o uno local proporcionado por ruta/URL, persistiendo la revisión y el snapshot.
- `scripts/query_sec_institutional_manager_universe.py`: Consulta de solo lectura del universo
  disponible a un corte `known_at` específico, con verificación de integridad de blob y formato JSON/humano.
- `scripts/smoke_sec_institutional_manager_universe.py`: Verificación integral de extremo a extremo
  en un workspace temporal con métricas de red, memoria RSS e idempotencia.
