# Revisión crítica del informe técnico externo de 2026-10-21

> Referencia histórica fechada. `DATA-CHASSIS` está `DONE` y `FUNDAMENTALS-COVERAGE` es el único
> `NEXT`; la matriz vigente vive en [`../v2_operational_readiness.md`](../v2_operational_readiness.md).
> Este registro no es un Work Block ni autoriza ejecución.

## Autoridad, alcance y evidencia

Decisión documental solicitada por el usuario: aprovechar el informe para la planificación futura,
sin crear Work Block, ampliar #345, modificar su manifest ni intervenir al builder.
En ese corte DATA-CHASSIS conservaba `NEXT` y FUNDAMENTALS-COVERAGE seguía `PLANNED`. Este registro no cierra Q1–Q7.
Las oportunidades siguientes no eran nuevos gates del bloque activo ni una cola de ejecución autorizada.

Informe original: `~/projects/investment-analyst/docs/external_technical_review_2026-10-21.md`.
SHA-256 del documento leído: b67c1db23124504ebdba5eb1ce098a4cc05f7b60453dc78fc714b8d8aa344dc1. Se conserva sin editar.
Inspección de código del writer `codex/data-chassis-45` en
`54acfd644426e92034f7c42e29e1b3b06e97d8c4`; su árbol tracked estaba limpio.
GitHub: #345 único activo, PR346 abierto, CI39704373129 IN_PROGRESS en esa lectura.
Este documento se prepara en un worktree separado en2e330382562fae7bd7f3cbee2ec678bc8e8f8a17;
no sustituye las actualizaciones más recientes del candidato. Reconciliar aditivamente al integrar.
Los estados vivos caducan: refrescarlos antes de elegir el siguiente bloque.

Se leyó el informe completo, código de almacenamiento/serialización, esquema, frontend, workflow,
baseline y ruta candidata. Se reutilizaron receipts de activación/consumidores/backup y la revisión
anterior. No se ejecutaron benchmarks, escaneos completos de DB, nuevas adquisiciones ni limpieza.
Esta revisión no es AUDIT del PR ni permite afirmar mejoras de rendimiento medidas.

## Qué puede sostenerse y qué debe corregirse

| Afirmación externa | Contraste | Decisión |
| --- | --- | --- |
| Sólo falta reconectar OAuth | Obsoleta: receipts acreditan v2 activo, transferencias y restore. La enmienda posterior trata coverage/read-only, aceptación y scheduler; el candidato ya avanzó. | No repetir reconexión ni activación por esa narrativa; resolver estado vivo. |
| 46 bloques dedicados a rescatar historia | La ruta incluye diseño de backend, identidad, incrementalidad, consultas, observabilidad y backup; no46 intentos equivalentes de migración. | Revisar coste de proceso con tiempos por categoría, no inferirlo de la numeración. |
| Ahorro99,2% de10.320MB a79,9MB | Universos y momentos distintos: fresh start no contiene toda v1. | Cifras de snapshots, no comparador de compresión ni Q1 MET. Medir inventario pareado incluyendo DB/WAL/raw/Parquet/CAS. |
| 1.171.019 duplicados /94,71%; ~65.400 únicos v2 | El baseline agrupa por activo, métrica, as_of y available_at; esa consulta no compara inputs, versión, parámetros o valor.65.447 es un agrupamiento v1, no conteo medido v2. | Mantener hallazgo histórico de repetición, pero verificar equivalencia completa antes de llamarla redundancia eliminable. Nunca borrar por ese GROUP BY. |
| 0MB de JSON en DuckDB | El esquema conserva document_json en catálogos y value_bytes BLOB en workspace_analytical_content_v2. CAS no significa almacenamiento necesariamente externo. | Medir payload por representación, tabla y contenido único; no afirmar cero por eliminar una columna antigua. |
| Funding ~15MB, raw redundante0, métricas duplicadas0 | El informe no presenta comparador reproducible completo con inventario/digests/bindings. | Hipótesis/resultados por demostrar; conservar invariantes de idempotencia sin convertirlas en medición global. |
| ≤30MB/día y backup/restore<3min | Un límite de aceptación no es medición diaria. Backups distintos no comparan rendimiento; packed local no mide upload/download/restore externo completo. Los lotes77.594.624/48.234.496B excedieron el criterio anterior de30MB. | Distinguir adquisición, replay y periodo; medir cada fase de recuperación con mismos datos, red y entorno. No ocultar excesos. |
| Buffer1GiB implica RSS<1.200MB | El límite de DuckDB no cubre toda la memoria del proceso/cgroup. | Conservar picos y eventos cgroup, RSS, spill y concurrencia; no deducir consumo del ajuste. |
| UUID nativo ahorra55% de memoria de joins |36→16 bytes es aritmética de representación, no benchmark de índices, compresión, strings o joins. No todo content_id/sequence_id es necesariamente UUID. | Medir tipos reales y prototipo aislado; migración de schema sólo con contrato, compatibilidad y recuperación explícitos. |
| Polling ciego30/60s | app-shell.js usa setTimeout, document.hidden, backoff y bloqueo de peticiones simultáneas. El reloj de mercado se renderiza localmente. | Corregir diagnóstico; medir tráfico/CPU ociosos antes de justificar SSE. |
| SVG causa<30FPS; canvas garantiza60FPS | No hay perfil de navegador/carga adjunto.3.045 líneas de local_web.py sí se verifican, pero tamaño no demuestra latencia. | Medir nodos, puntos, long tasks, p95 interacción y accesibilidad antes de cambiar renderer/servidor. |
|102 jobs en<2min; CI~4min | No hay desglose reproducible que sustente las proyecciones. CI configura timeout45min, no duración fija45min; el run previo exacto duró38m05s. | Objetivos exploratorios, no compromisos de entrega. |

El lenguaje de “percentil superior”, “salvado del colapso” y “cientos de horas” no aporta una
comparación verificable. La crítica útil es reducir retrabajo y separar criterios de cierre de
mejoras opcionales; no eliminar controles de identidad, evidencia, recuperación o revisión humana.

## Riesgos concretos de las soluciones propuestas

### Escritura compacta e integridad

En `_save_metrics`, `checksum = sha256_hex(canonical_json_bytes(item))` representa el modelo
completo. La equivalencia LIVE existente compara modelos excluyendo `computed_at`; en transición
histórica además exige igualdad exacta y origen HISTORICAL. Reemplazar ambas reglas por igualdad
del checksum actual puede rechazar recomputaciones válidas o perder distinciones de procedencia.

Un futuro ensayo puede usar checksum igual como fast path sólo si conserva todas las validaciones;
checksum distinto no significa automáticamente conflicto semántico. Debe demostrar que evita
hidratación en la carga dominante, no sólo en repeticiones idénticas de reloj. Preferir proyecciones
mínimas o reutilización existente a un nuevo digest persistido; este último exige representación
versionada, migración e invalidación. Probar relojes distintos, diferencias financieras, parámetros,
versiones, inputs, corrupción y transición HISTORICAL/LIVE antes de aceptar ahorro.

La lectura de secuencias valida también integridad almacenada, no sólo colisión criptográfica.
Eliminarla porque SHA-256 es fuerte puede ocultar miembros truncados, orden incorrecto o corrupción.
Evaluar verificación agrupada y cache acotada con invalidación correcta; no suprimir los negativos.

### Concurrencia y persistencia

Separar red de escritura puede ser útil si el perfil demuestra espera de red dominante. Una sola
transacción global para102 jobs contradice el progreso durable por etapa. Mantener un writer y
commits/checkpoints existentes; cola acotada por bytes, backpressure, cancelación, límites de fuente,
orden determinista y preservación del progreso ante error. No prefetchear el universo en RAM ni
reordenar available_at. Medir requests, throttling, CPU/red, cola, picos y rendimiento de extremo a
extremo; comparar mismo inventario/cursores/inputs. `urllib.request` por sí solo no prueba saturación.

### CI y cambios de gobernanza

El workflow ya emite `--durations=30` y JUnit: explotar esa evidencia primero, sin rerun completo
sólo para volver a medir. Evaluar particiones balanceadas antes de añadir dependencia xdist;
si se usa, limitar workers según memoria de tests DuckDB, aislar fixtures/puertos/roots y verificar
colección determinista. Combinar coverage de todos los shards con el mismo umbral y conservar
`Python 3.12 quality` como gate agregado exact-SHA: fallar si falta/cancela/falla cualquier shard.
Lint, lock y auditoría siguen obligatorios. No trasladar regresiones críticas de storage/PIT/recovery
a nightly para anunciar CI rápida; nightly sólo para ensayos adicionales que no sustituyan gates.
Workflow/gates pertenecen a gobernanza: requieren próximo bloque con autoridad apropiada, no cambio
incidental del BUILD_PRODUCT actual. Medir wall time y runner-minutes, flakiness y memoria, no sólo
el shard más rápido. La documentación de xdist advierte que fixtures de sesión pueden repetirse.

### Tiering, frontend y limpieza

Parquet frío puede evaluarse tras inventario e importación selectiva. La edad>90d no prueba que el
dato sea prescindible: warm-up, revisiones PIT y lineage pueden necesitarlo. Requerir manifiestos,
consulta hot+cold sin huecos/duplicados, orden/paginación, exactitud Decimal, backup/restore y
recuperación ante interrupción. No adoptar100MB como tope de DB sin carga justificada ni introducir
otra migración antes de demostrar el cuello de botella.

SSE añade conexiones persistentes, reconexión y lifecycle; no es requisito de eficiencia mientras
el polling acotado cumpla. Probar primero paginación/carga diferida y límites visuales que no pierdan
información. Canvas/biblioteca externa exige evidencia, accesibilidad y contrato de exportación;
modularizar rutas sólo con valor de mantenimiento demostrado y pruebas de equivalencia de HTTP.

Worktrees/backups no son basura por nombre o edad. Inventariar paths/bytes/cambios locales/consumidor,
refs y recuperabilidad antes de proponer retiro. Proteger handoffs, v1, F5 y receipts. No autorizar
Optimize-VHD, borrado de worktrees ni limpieza de historia desde este registro. Espacio lógico libre
y recuperación física del disco anfitrión son métricas y operaciones diferentes.

## Orden propuesto para decisiones posteriores

| Prioridad / candidato informativo | Condición de entrada | Entrega y aceptación para un futuro PLAN |
| --- | --- | --- |
|0. Terminar bloque vigente | #345 tiene acciones autorizadas pendientes | Reutilizar su enmienda y gates; no añadir estas oportunidades como bloqueantes ni prometer su cierre. |
|1. Reducir ciclo CI | Bloque libre; desglose del run definitivo disponible | Un bloque de gobernanza cohesivo si CI es coste dominante: mismo conjunto crítico/cobertura/audit, pruebas de gate incompleto, menor wall time reproducible, coste/memoria declarados. Meta cuantitativa se fija tras baseline. |
|2. Archivo e incorporación selectiva | V2 estable y recuperable; inventario de v1/F5/últimas semanas | Piloto por consumidor y dependencias, sin duplicados equivalentes, PIT/Decimal/lineage íntegros, lectura completa, backup/restauración y crecimiento medidos. Es continuidad de DATA-CHASSIS; no posponer por UI/SSE. |
|3. Hot paths compactos | Perfil del replay/piloto demuestra coste material de hidratación | Optimización estrecha sin cambio de significado; comparar mismos inputs/versión/entorno con relojes distintos, recursos y consultas, negativos intactos. Puede preceder al piloto sólo si bloquea su presupuesto. |
|4. Adquisición acotada | Perfil demuestra red dominante y necesidad del usuario | Productor/red concurrente acotado con writer único/progreso durable, límites oficiales y sin ampliación de catálogo. No meta102jobs/2min sin prueba. |
|5. UX y mantenimiento | Perfil real muestra lentitud o coste de cambios | Elegir la intervención mínima: queries/paginación/polling antes de SSE/renderer; modularización independiente si reduce complejidad verificable. No requisito de cierre DATA-CHASSIS. |
|6. Tiering/UUID | Crecimiento o joins incumplen presupuesto tras selección y optimización | Prototipo de copia aislada con ahorro total medido y contrato de migración/recovery; descartar si no justifica riesgo. No elección previa de tecnología por moda. |

No se fija calendario de semanas sin capacidad ni resultados. Como máximo elegir el siguiente
bloque cohesivo al liberarse el activo; no crear seis Issues especulativos ni seis gates nuevos.
Prioridad1 no posterga automáticamente la continuidad2: si CI deja de ser dominante, se sigue con
el piloto. Tras los criterios globales de DATA-CHASSIS, retomar FUNDAMENTALS-COVERAGE conforme a la
ruta vigente; el informe no autoriza capacidades nuevas de Cazatiburones ni cambia su estado.

Cada futuro ensayo declara SHA/runtime, comando, entorno, inventario, inputs, unidad, comparador,
memoria/espacio, resultado y limitaciones. Un dato ausente es UNKNOWN. Medición pareada antes/después
sin descargar/borrar historia para mejorar el resultado; escenarios y número de muestras explícitos.
Si no aparece mejora material o el riesgo supera el beneficio, cerrar la oportunidad sin implementarla.

## Fuentes comprobadas

- Código candidato indicado arriba: `storage/compact_analytical_v2.py` (`_save_metrics`,
  `_ensure_sequences_batch`), `storage/serialization.py`, `storage/migrations/002_workspace_v2.sql`;
  `frontend/static/app-shell.js`, `frontend/local_web.py`, `.github/workflows/ci.yml`.
- Baseline histórico: `docs/data_chassis_baseline.md`, secciones5–6; no medición v2 actual.
- [Issue345](https://github.com/Marjuraru/investment-analyst/issues/345),
  [PR346](https://github.com/Marjuraru/investment-analyst/pull/346) y
  [CI observado](https://github.com/Marjuraru/investment-analyst/actions/runs/39704373129).
- [DuckDB: OOM y memoria fuera del buffer manager](https://www.duckdb.org/docs/current/guides/performance/oom).
- [xdist: fixtures por worker](https://pytest-xdist.readthedocs.io/en/stable/how-to.html#making-session-scoped-fixtures-execute-only-once)
  y [colección determinista](https://pytest-xdist.readthedocs.io/en/stable/known-limitations.html).
