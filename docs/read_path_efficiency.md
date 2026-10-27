# Eficiencia de lectura de evidencia

> Estado vigente 2026-10-26: `DATA-CHASSIS` está `DONE` y `FUNDAMENTALS-COVERAGE` es el único
> `NEXT`. La matriz V1–V6 vive en [`v2_operational_readiness.md`](v2_operational_readiness.md).

## Contrato vigente (`read-dependency-signature-v1`)

Cada lectura cacheable se identifica por UUID real del workspace, versión de código/contrato/catálogo,
endpoint, argumentos completos, `known_at` explícito y selección semántica. Las dependencias se leen
como proyecciones tipadas de índice (observaciones, raw, métricas, secuencias y parámetros), nunca
hidratando modelos completos ni infiriendo igualdad desde count/MAX, tamaño DB/WAL, mtime o TTL. Si
una lectura no puede demostrar que firma y resultado corresponden al mismo snapshot, se ejecuta sin
caché persistente y se registra `uncacheable`.

La caché es globalmente acotada (128 entradas/32 MiB, LRU) y coalesce sólo requests idénticas bajo
identidad/snapshot compatibles. Los cambios de activo/corte/UUID/release invalidan por identidad, no
por heurística temporal. La telemetría `critical-read-performance-v1` mantiene contadores por familia
y las últimas 64 duraciones; `/api/v1/read-performance` no consulta la base ni se mide a sí mismo.
`benchmark_runtime_reads.py --include-critical` publica la ventana y el overhead pareado de 20
repeticiones; no declara cumplidas metas de ruta por un fixture.

Matriz cubierta: Mesa/cobertura, gráfico diario y comparación AMD+AAPL, fundamentales/valoración AMD,
documentos SEC y posiciones de un gestor 13F. AAPL es control equivalente, no predeterminado. Las
consultas globales de cobertura leen su universo legítimo y no se confunden con una consulta de un
activo. Las superficies no cubiertas permanecen pendientes justificadas en este documento, no se
declaran optimizadas.

El contrato del arranque exige cero hidratación ajena, paginación completa y medición de filas/bytes/query plan. Las referencias inferiores a document_json y call sites preservados describen su bloque histórico; no obligan a mantener una lectura ineficiente reproducida en el scope autorizado de #345.

## Proyección v2 y reapertura (`FUNDAMENTALS-COVERAGE-READINESS`)

En un workspace formato 2, la cobertura de mercado y el precio de valoración usan
`HistoricalMarketDataV2Service` sobre el índice tipado de `raw_v2`:

- la selección PIT conserva el mismo orden, revisión única y error de ambigüedad que
  `HistoricalMarketDataService`; los conteos `candidate_versions` y `discarded_revisions` se suman por
  página;
- no se hidrata un `RawRecord` por barra histórica: la cobertura materializa sólo la última barra
  seleccionada y la valoración sólo el precio seleccionado, verificando checksum, observaciones y
  alcance;
- `get_many` calcula una sola vez por lote la existencia de la tabla de proyecciones JSON, en lugar
  de consultar `information_schema` por registro;
- `summarize_market_history_v2` selecciona todas las revisiones PIT y sus conteos con candidatos
  ligeros, pero construye y verifica sólo la proyección más reciente; la ambigüedad del timestamp
  seleccionado sigue fallando cerrado;
- la valoración busca el precio en ventanas recientes (30/90/365/3650 días) y sólo expande hasta el
  rango completo si no encuentra una barra, conservando la última barra PIT y su verificación;
- la cobertura v2 reparte los activos en hasta cuatro conexiones read-only aisladas y conserva el
  orden contractual del resultado; el formato 1 sigue secuencial;
- la tabla de observaciones se verifica una vez por conexión y los instantes ISO repetidos usan una
  caché acotada;
- la firma de cobertura acota observaciones y raw al rango `min(market_start, fundamental_start)` →
  `known_at`, de modo que el coste depende del universo/ventana consultados y no de toda la historia;
- Mesa prioriza la matriz de 37 activos y difiere los loaders secundarios hasta que termina; un fallo
  libera el tablero para reintento;
- el formato 1 conserva el camino anterior sin cambios.

La matriz cubierta se amplía a la proyección v2 sin cambiar fórmulas, `Decimal`, IDs, versiones,
resultados ni límites PIT. El corpus ajeno 1x/10x se prueba en scratch y no aumenta la hidratación de
la selección individual. La medición in-process del 2026-10-26 sobre el workspace activo dio ~9,9 s
en frío y ~6,6 s en caliente para los 37 activos; el smoke de navegador y sus cinco muestras se
registran en [`v2_operational_readiness.md`](v2_operational_readiness.md).

En el cliente, un fallo de cualquier loader diferido libera el tablero de `loadedBoardIds` tras
`Promise.allSettled`, de modo que una nueva activación reintenta. La cobertura de Mesa vuelve a lanzar
el error después de renderizar el estado de ausencia, sin dejar el tablero bloqueado ni presentar la
respuesta fallida como datos vigentes.

### Precio seleccionado compartido (`FUNDAMENTALS-COVERAGE-CYCLE`)

En una consulta de cobertura de un equity, la lectura de mercado ya selecciona y verifica la última
barra PIT del corte. La valoración corporativa de la misma consulta reutiliza esa barra verificada
en lugar de repetir `summarize_market_history_v2` y `materialize` sobre el mismo activo/fuente/corte.
`CorporateValuationService.query` acepta un `selected_price` keyword-only interno y lo valida contra
activo, fuente, frecuencia, `available_at <= known_at` y fecha de valoración; si no coincide, falla
cerrado. La búsqueda por ventanas 30/90/365/3650/todo se conserva para los consumidores que no
disponen de una barra previa, como la lectura directa de valoración de un activo.

El resultado es exactamente el mismo: la última barra <= `valuation_date` dentro de la ventana de
cobertura es también la última barra elegible de la historia. La prueba estructural
`test_v2_market_projection_matches_v1_and_hydrates_only_the_latest_bar` baja de dos a una lectura de
blob por activo y conserva conteos, PIT, `Decimal`, orden y payload. `test_selected_price_avoids_a_second_market_read`
y `test_selected_price_must_match_the_requested_cut` pinnean la equivalencia y el rechazo.


Las lecturas de `RawRecord` verifican siempre el SHA-256 del archivo antes de
deserializarlo. La copia canónica `document_json` del índice se conserva por
compatibilidad y auditoría, pero su comparación textual completa se realiza
sólo mediante `verify_index_integrity(record_ids)`: una operación explícita,
acotada a 1.000 identificadores y fail-closed.

Los artefactos semánticos 13F visibles se resuelven por `record_id` mediante
un lector LRU efímero de 256 entradas. El caché pertenece a una instancia de
repositorio de registros crudos: no se persiste, no se comparte entre
workspaces/sesiones y no altera el filtro ni el orden point-in-time.

Para reproducir la medición local sin proveedores ni workspace permanente:

```bash
PYTHONPATH=src .venv/bin/python scripts/benchmark_evidence_read_path.py
```

El script crea un corpus sintético en un directorio temporal externo y emite
conteos de lecturas SHA, parseos, invalidaciones y una aproximación de tiempo
y memoria para la ruta anterior y la memoizada.

El camino programado de alertas usa una medición equivalente para `metric_results`:

```bash
PYTHONPATH=src .venv/bin/python scripts/benchmark_analytical_metric_query.py
```

El benchmark crea métricas sintéticas en un directorio temporal externo y compara la consulta por
activo sin proyección con la consulta acotada a las claves de una regla. Informa conteos de filas
materializadas y documentos deserializados, además de una aproximación efímera de tiempo y memoria;
los conteos son reproducibles y no se persisten. La reducción se aplica sólo a
`AnalyticalScreeningMonitor` y `AnalyticalBacktestService`. Los ocho call sites de lectura bajo
petición documentados en `docs/storage.md` conservan el patrón anterior de forma intencional:
este bloque no amplía la superficie ni cambia su semántica point-in-time.
