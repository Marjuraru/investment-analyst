# Eficiencia de lectura de evidencia

> Decisión PLAN 2026-10-21: [arranque v2 nuevo](data_chassis_v2_fresh_start_plan.md). Activación verificada por #346 MERGED en `b5e714a`; release observada `daad58d`; workspace UUID `70c64668-1738-4dfe-ba1f-7e72026bb7fe`; historia v1/F5 preservada. La formalización, neutralidad y retención continúan en [DATA-CHASSIS-46](data_chassis_continuity_plan.md) (#349). Las recetas de migración anteriores son históricas y no se ejecutan para el arranque nuevo.

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
