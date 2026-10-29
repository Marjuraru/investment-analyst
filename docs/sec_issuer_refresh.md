# Refresh fundamental SEC por emisor

Este flujo actualiza una empresa cotizada y configurada en SEC sin depender de Alpaca, de su
cobertura de mercado ni del bootstrap completo de Apple. Usa únicamente SEC EDGAR, no exige una API
key y mantiene el diagnóstico fundamental separado del diagnóstico de mercado.

## Etapas

Cada ejecución abre una sola conexión writer y ejecuta exactamente las seis etapas del contrato
`sec-issuer-fundamental-refresh-v1`, en este orden:

1. `sec_fetch`: descarga de Submissions y Company Facts y persistencia append-only de los dos
   snapshots oficiales;
2. `sec_normalization`: normalización de hechos XBRL compatibles, incluida la alternancia anual DEI de
   acciones comunes;
3. `known_at_resolution`: resolución del corte point-in-time (`requested_known_at` o reloj UTC);
4. `fundamental_metrics`: persistencia idempotente de las cinco familias de métricas base;
5. `fundamental_diagnostic`: persistencia de un diagnóstico fundamental independiente;
6. `corporate_valuation`: cálculo y persistencia de la valoración `latest_annual` con
   `valuation_date = effective_known_at.date()`.

SEC se consulta en cada ejecución. Un snapshot idéntico reutiliza su identidad; una revisión crea
otra versión sin borrar la anterior. Si una etapa falla, el error identifica la etapa y el progreso
de las etapas anteriores se conserva. No hay rollback global.

## Emisores activos

Apple conserva sus IDs y versiones históricas. Los emisores genéricos habilitados son:

- AMD: activo `equity:us:amd`, ticker `AMD` y CIK `0000002488`;
- Intel: activo `equity:us:intc`, ticker `INTC` y CIK `0000050863`;
- Strategy: activo `equity:us:mstr`, ticker `MSTR` y CIK `0001050446`;
- Micron: activo `equity:us:mu`, ticker `MU` y CIK `0000723125`;
- Palantir: activo `equity:us:pltr`, ticker `PLTR` y CIK `0001321655`;
- Coeur Mining: activo `equity:us:cde`, ticker `CDE` y CIK `0000215466`;
- Hycroft Mining: activo `equity:us:hymc`, ticker `HYMC` y CIK `0001718405`;
- McEwen: activo `equity:us:mux`, ticker `MUX` y CIK `0000314203`;
- Newmont: activo `equity:us:nem`, ticker `NEM` y CIK `0001164727`;
- Southern Copper: activo `equity:us:scco`, ticker `SCCO` y CIK `0001001838`.
- Barrick: activo `equity:us:b`, ticker `B`, CIK `0000756894`, IFRS anual y 40-F;
- Buenaventura ADR: activo `equity:us:bvn`, ticker `BVN`, CIK `0001013131`, IFRS anual y 20-F;
- TSM: activo `equity:us:tsm`, ticker `TSM`, CIK `0001046179`, IFRS anual y 20-F.

Las identidades fueron contrastadas con el índice oficial de compañías de SEC. La de AMD también se
contrastó con su
[Form 10-K oficial de SEC](https://www.sec.gov/Archives/edgar/data/2488/000000248826000018/0000002488-26-000018-index.htm).
Su incorporación no autoriza aplicar el conector SEC a ETF, criptomonedas ni empresas BVL/SMV.
El catálogo declara además la taxonomía: US-GAAP admite anual y trimestral mediante 10-K/10-Q; el
perfil IFRS admite solo anual mediante 20-F/40-F. Una frecuencia incompatible falla antes de
consultar SEC o abrir una operación de escritura. La cobertura de universo aplica la misma
declaración: una consulta `quarterly` sobre un perfil IFRS devuelve `capability=not_implemented` y
`evidence=not_queried`, no `missing`, y añade la limitación de perfil al activo.

## Acciones comunes DEI

El normalizador consulta además `dei:EntityCommonStockSharesOutstanding` como fuente complementaria
de `fundamental.shares_outstanding`. Sólo admite el hecho instantáneo anual del mismo emisor,
filing, accession, forma, año y período fiscal, cierre `reportDate` y aceptación SEC que los hechos
del filing; una fecha de portada distinta no se transporta al cierre y se omite con causa explícita.
La unidad debe ser `shares`, el valor debe ser `Decimal` finito y positivo, y no se aceptan acciones
autorizadas, emitidas, promedio, diluidas ni de otra clase.

La selección nativa del perfil US-GAAP o IFRS conserva prioridad en su mismo contexto. Si DEI
coincide exactamente no se emite una segunda observación; si discrepa se rechaza la observación DEI,
se conserva la nativa y el conflicto queda contabilizado explícitamente sin tolerancia, redondeo ni
prioridad que lo esconda; el resto del filing sigue normalizando. Sólo cuando falta la nativa
elegible se emite una observación DEI con `taxonomy=dei`, su tag real y la versión
`sec-companyfacts-dei-shares-v1`; los conceptos existentes conservan sus versiones, bytes e IDs. El
`record_key` mantiene taxonomy, tag, unidad, período y linaje, y la reconstrucción/replay reutiliza
la misma identidad sin duplicar filas. Las ausencias y los rechazos quedan contabilizados en
`skipped_counts`; no se completa un ejercicio con el valor de otro.

## Ejecución

SEC exige un `User-Agent` identificable con nombre de aplicación y contacto:

```bash
export SEC_USER_AGENT="Investment Analyst contacto@example.com"
```

Actualizar AMD anualmente en el workspace predeterminado:

```bash
cd ~/projects/investment-analyst
.venv/bin/python scripts/refresh_sec_fundamentals.py \
  --workspace ~/.local/share/investment-analyst/workspaces/default \
  --asset-id equity:us:amd \
  --frequency annual
```

Para una reconstrucción histórica, `--known-at` acepta un timestamp ISO 8601 con zona:

```bash
.venv/bin/python scripts/refresh_sec_fundamentals.py \
  --workspace ~/.local/share/investment-analyst/workspaces/default \
  --asset-id equity:us:amd \
  --frequency annual \
  --known-at 2026-12-31T23:59:59Z
```

El corte explícito se conserva exactamente. Si precede toda la evidencia normalizada de esa
frecuencia, el flujo se detiene antes de métricas y diagnóstico, pero conserva los snapshots y las
observaciones recién validados.

El mismo refresh se ejecuta sobre el workspace activo por binding desde la interfaz local:

```bash
curl -s -X POST http://127.0.0.1:8765/api/fundamental-refresh \
  -H 'Content-Type: application/json' \
  -d '{"asset_id":"equity:us:b","frequency":"annual","requested_known_at":null}'
```

El endpoint resuelve el emisor contra el catálogo, usa el mutex y la cola manual existentes,
invalida las cachés de lectura afectadas y no adquiere mercado. `requested_known_at: null` resuelve
el corte con el reloj UTC; un timestamp explícito se conserva exactamente. El workspace es el
declarado por el binding de la release activa, no una ruta adivinada por el cliente.

## Resultado y límites

`sec-issuer-fundamental-refresh-v1` informa documentos, hechos examinados y seleccionados,
observaciones por campo y frecuencia, omisiones, métricas creadas o reutilizadas, cobertura,
requisitos faltantes y diagnóstico. No incluye documentos SEC, credenciales ni un `User-Agent`.

Si Company Facts asigna el mismo año y período fiscal a dos cierres distintos, el motor no escoge
uno por orden de almacenamiento. Marca ambos contextos fiscales como inconsistentes, conserva los
ratios que dependen solo del mismo cierre y omite únicamente los comparativos interanuales
afectados. La omisión queda contabilizada como `inconsistent_fiscal_metadata`.

El motor ampliado puede consultar después la tendencia de cinco hechos, las 40 métricas, su
historial y las ocho secciones analíticas usando el mismo `asset_id`. El smoke real de AMD del
28 de julio de 2026 confirmó idempotencia exacta y 25 métricas disponibles en el período más
reciente. El smoke real anual de Intel del mismo día procesó dos documentos, 687 observaciones y 19
métricas base; la repetición reutilizó exactamente los dos raw, las 687 observaciones, las 19
métricas y el diagnóstico. MSTR, MU y PLTR produjeron respectivamente 477, 873 y 559 observaciones,
27, 39 y 28 métricas base, cobertura diagnóstica completa y una repetición sin nuevos registros.
CDE, HYMC, MUX, NEM y SCCO produjeron respectivamente 873, 633, 845, 569 y 1224 observaciones y 20,
27, 31, 33 y 9 métricas base. Su cobertura varía y permanece explícita; las repeticiones no crearon
registros. Todos conservaron trazabilidad completa, por lo que sus descriptores web están
habilitados. Los smokes IFRS reales del 29 de julio de 2026 produjeron 166 observaciones y 43
métricas base para Barrick, 215 y 38 para BVN, y 149 y 38 para TSM; sus repeticiones reutilizaron
exactamente los dos raw, todas las observaciones, todas las métricas y el diagnóstico. La interfaz
usa un endpoint writer independiente, frecuencia anual y cachés de lectura por emisor.
La etapa `corporate_valuation` calcula y persiste hasta once métricas `latest_annual` por emisor, con
estado y motivo explícitos; el resumen informa creadas, reutilizadas y no evaluables. No combina
scores, no recomienda operaciones y no ejecuta órdenes. La adopción operacional del 2026-10-29 se
resume en [`corporate_valuation_point_in_time.md`](corporate_valuation_point_in_time.md) y
[`v2_operational_readiness.md`](v2_operational_readiness.md).

El refresh verifica cada observación contra la adquisición que la produjo. Cuando dos adquisiciones
válidas comparten la identidad de un raw SEC, la comprobación reconstruye primero el contexto
actual y sólo ante una diferencia de procedencia usa la evidencia local
`sec-acquisition-provenance-v1` del workspace v2. El runtime no necesita ni abre el workspace v1;
la receta y el estado vigente están en `docs/sec_provenance_repair_runbook.md`.
