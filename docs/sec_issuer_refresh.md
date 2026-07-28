# Refresh fundamental SEC por emisor

Este flujo actualiza una empresa estadounidense configurada en el catálogo sin depender de Alpaca,
de su cobertura de mercado ni del bootstrap completo de Apple. Usa únicamente SEC EDGAR, no exige
una API key y mantiene el diagnóstico fundamental separado del diagnóstico de mercado.

## Etapas

Cada ejecución abre una sola conexión writer y ejecuta exactamente:

1. descarga de Submissions y Company Facts;
2. persistencia append-only de los dos snapshots oficiales;
3. normalización de hechos XBRL compatibles;
4. resolución del corte point-in-time;
5. persistencia idempotente de cinco métricas base;
6. persistencia de un diagnóstico fundamental independiente.

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

Las identidades fueron contrastadas con el índice oficial de compañías de SEC. La de AMD también se
contrastó con su
[Form 10-K oficial de SEC](https://www.sec.gov/Archives/edgar/data/2488/000000248826000018/0000002488-26-000018-index.htm).
Su incorporación no autoriza aplicar el conector SEC a ETF, criptomonedas ni empresas BVL/SMV.

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

## Resultado y límites

`sec-issuer-fundamental-refresh-v1` informa documentos, hechos examinados y seleccionados,
observaciones por campo y frecuencia, omisiones, métricas creadas o reutilizadas, cobertura,
requisitos faltantes y diagnóstico. No incluye documentos SEC, credenciales ni un `User-Agent`.

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
habilitados. Barrick queda excluido porque su evidencia reciente es 40-F/IFRS, fuera del contrato
US-GAAP 10-K/10-Q. La interfaz usa un endpoint writer independiente y cachés de lectura por emisor.
El flujo no calcula valoración, no combina scores, no recomienda operaciones y no ejecuta órdenes.
