# Eficiencia de lectura de evidencia

> Decisión PLAN 2026-10-21: [arranque v2 nuevo](data_chassis_v2_fresh_start_plan.md). Activación verificada por #346 MERGED (`daad58d`), workspace UUID `70c64668-1738-4dfe-ba1f-7e72026bb7fe`; historia v1/F5 preservada. La formalización, neutralidad y retención continúan en [DATA-CHASSIS-46](data_chassis_continuity_plan.md) (#349). Las recetas de migración anteriores son históricas y no se ejecutan para el arranque nuevo.

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
