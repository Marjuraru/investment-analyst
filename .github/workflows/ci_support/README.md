# CI partition support

Estos helpers pertenecen exclusivamente al workflow `CI`. No son runtime, no importan código de
producto y no seleccionan tests por narrativa, nombre aproximado o coincidencia de substring.

## Contrato

- `pytest_partition.py` es un plugin de pytest. En modo `collect` escribe el universo completo de
  node ids; en modo `shard` verifica el digest del universo recolectado antes de filtrar y ejecuta
  exactamente los node ids seleccionados; en modo `serial` ejecuta el universo completo como
  referencia opcional. Nunca recibe node ids por shell: lee un JSON estricto.
- `shards.py` construye las particiones, valida manifiestos y recibos, combina cobertura y compara
  la referencia serial. Todos sus JSON rechazan campos duplicados, desconocidos o no finitos.
- `module_weights.json` contiene pesos medidos por módulo, su procedencia y el peso por defecto para
  módulos nuevos. Un peso es una sugerencia de reparto; nunca decide qué tests se ejecutan.

## Niveles de validación

- `FOCUSED`: PLAN declara comandos, suites, costuras afectadas y regresiones cruzadas obligatorias;
  BUILD los ejecuta localmente antes de publicar el candidato. Incluye consumidores de
  almacenamiento, PIT, identidad, interfaces y contratos afectados, no sólo los tests modificados.
  Si el impacto es incierto se amplía la selección o se usa `FULL`. No se infiere seguridad desde
  paths ni se ejecutan comandos recibidos desde el texto del Issue dentro de CI.
- `FULL`: el gate literal `Python 3.12 quality` conserva el nombre, todo el universo recolectado,
  los cuatro shards, Ruff, auditoría y cobertura global `>=82%`. Es obligatorio en cada candidato
  final exact-SHA antes de AUDIT/merge; `FOCUSED` no puede producirlo. El verde se reutiliza sólo
  para el mismo SHA y entorno; se repite ante un SHA nuevo, un cambio relevante de entorno, un fallo
  o un riesgo nuevo, y nunca se traslada cobertura entre SHAs. `serial-reference` sigue opt-in.

La receta local del nivel `FULL` es `bash scripts/check.sh`, ejecutada una sola vez por cambio de CI
o de dependencias/lock/toolchain; no se duplica su pytest ni se copia una base real para validarlo.
`FOCUSED` usa los comandos declarados por el Work Block y no se convierte en un gate parcial que
autorice merge. Los comandos de colecta, selección y ejecución de un shard están en
[Ejecución local](#ejecución-local).

## Manifiesto `ci-partition-manifest-v1`

El job de planificación escribe `ci-partition-manifest.json` y cuatro selecciones
`selected-shard-N.json`. El manifiesto conserva:

- `source_sha`, `run_id`, `run_attempt` y `python_version` del checkout real;
- `lock_sha256` y `weights_sha256` de los bytes usados;
- `universe_count` y `universe_sha256` del universo completo recolectado;
- `parallel_started_at_epoch` para medir el camino paralelo;
- cuatro `partitions` disjuntas con `nodeids` ordenados, módulos, pesos y `weight_seconds`.

Cada runner descarga el artefacto del mismo `sha`/`run_attempt`, valida la identidad y comprueba que
su colección completa tiene exactamente el `universe_sha256` planificado. Un artefacto de otro SHA,
run o attempt falla cerrado.

## Algoritmo de partición

Los node ids se agrupan por módulo (el archivo antes de `::`) para preservar fixtures de ámbito
módulo. La asignación es greedy y determinista: módulos por peso descendente y ruta lexical como
desempate; cada módulo va al shard con menor carga, con el índice como desempate. Se exigen unión
exacta del universo, disjunción estricta y cuatro shards no vacíos. Un módulo sin peso medido usa
`default_weight_seconds`; no existe una allowlist de módulos.

## Recibos y agregado

Cada shard escribe `receipt-shard-N.json`, JUnit, datos de cobertura y timing. El recibo contiene la
identidad del run, el digest del universo y de la selección, el outcome terminal y la duración de
cada node id (incluidos setup/teardown/skip/xfail/xpass). El gate agregado `Python 3.12 quality`
exige:

1. `plan`, los cuatro shards y `audit` en `success`;
2. cuatro recibos completos, sin duplicados, con unión exacta del universo planificado;
3. todos los outcomes terminales y de éxito (un `skip` individual se reporta; un job skipped falla);
4. cobertura combinada con `branch` y `subprocess`, mínimo global 82 y paths bajo el source root;
5. si `compare_serial=true`, referencia serial del mismo SHA y reducción del camino paralelo de al
   menos 40 % sobre la duración serial comparable.

Un recibo o artefacto ausente, un outcome no terminal, un SHA ajeno o una cobertura perdida producen
exit no cero. El agregado nunca sustituye estas comprobaciones por un `echo PASS`.

## Cobertura

Cada shard ejecuta pytest con la misma configuración financiera y `--cov-fail-under=0` sólo para su
fragmento. Los datos se nombran `coverage-shard-N` (no ocultos) y los sufijos paralelos que produce
la instrumentación de subprocesos se incluyen explícitamente en la combinación. El agregado ejecuta
`coverage combine --keep` sobre todos ellos, exporta JSON y exige
`coverage report --fail-under=82`; los archivos nunca ejecutados permanecen en el denominador. La
normalización exige que cada archivo medido resuelva dentro de `src/investment_analyst`.

## Ejecución local

```bash
PYTHONPATH=src:.github/workflows/ci_support CI_PARTITION_MODE=collect \
  CI_COLLECTION_OUT=/tmp/ci-collection.json \
  .venv/bin/python -m pytest --collect-only -q -p pytest_partition

.venv/bin/python .github/workflows/ci_support/shards.py plan \
  --collection /tmp/ci-collection.json \
  --weights .github/workflows/ci_support/module_weights.json \
  --shards 4 \
  --source-sha "$(git rev-parse HEAD)" \
  --run-id 1 --run-attempt 1 --python-version 3.12.11 \
  --lock-file uv.lock --parallel-started-at "$(date +%s)" \
  --output-dir /tmp/ci-partitions --manifest /tmp/ci-partition-manifest.json
```

Para ejecutar un shard local:

```bash
PYTHONPATH=src:.github/workflows/ci_support CI_PARTITION_MODE=shard CI_SHARD=1 \
  CI_SELECTION_FILE=/tmp/ci-partitions/selected-shard-1.json \
  CI_RECEIPT_OUT=/tmp/receipt-shard-1.json COVERAGE_FILE=/tmp/coverage-shard-1 \
  .venv/bin/python -m pytest --assert=plain --verbose \
  --cov=investment_analyst --cov-report=term-missing --cov-fail-under=0 \
  -p pytest_partition
```

## Comparación serial opcional

```bash
gh workflow run ci.yml --ref <branch> -f compare_serial=true
```

El job `Serial reference` ejecuta la suite completa en un runner separado del mismo SHA y publica
recibo, cobertura y timing. El agregado exige esa referencia, compara outcomes y cobertura, y falla
si la ruta paralela no reduce al menos 40 %. La ruta paralela se mide desde el inicio del job de
planificación hasta el último shard, más la duración propia del agregado; no incluye la espera de la
referencia serial, que es sólo evidencia comparativa. El resumen conserva segundos de cola, ejecución
por shard, runner-minutes de plan+máquinas y el desglose de cobertura. No se activa por defecto ni
duplica la suite en PR normales. Un solo ensayo comparativo es suficiente; se repite sólo si un
fallo, un cambio de candidato o un desequilibrio demostrado lo justifica.

## Reporte de balance

El job agregado publica `ci-balance-report-v1` con:

- ruta crítica (`critical_path_seconds`) y `runner-minutes` del camino paralelo;
- duración de plan, agregado, cola y ejecución de shards;
- por shard: `wall_seconds`, `pytest_seconds`, `overhead_seconds`, peso y conteo;
- ratios `wall_ratio`, `pytest_ratio` y `weight_ratio`, con spreads;
- bytes observados de artifacts descargados y retención de 7 días;
- `source_sha`, `run_id`, `run_attempt`, `universe_sha256`, `weights_sha256` y versión de pesos.

Comando local (mismos archivos que produce el workflow):

```bash
.venv/bin/python .github/workflows/ci_support/shards.py report \
  --manifest /tmp/ci-partition-manifest.json \
  --receipts-dir /tmp/ci-receipts \
  --partition-dir /tmp/ci-partitions \
  --quality-dir /tmp/ci-quality \
  --serial-dir /tmp/ci-serial \
  --retention-days 7 \
  --summary-out /tmp/ci-quality/balance-report.json
```

`--serial-dir` es opcional; si no existe, el reporte omite esos bytes. La comparación de balance
exige la misma suite y colección. Un cambio de tests cambia el universo: se declara como no
comparable y no se presenta una mejora estimada como aceleración observada.

## Pesos y recuperación

Los pesos vigentes son `2026-10-26.1`, derivados del run `40253368902` / job de planificación
`121349862244` (`265cd860157b399f7160bd91951e7d512de1ac1c`), por duración de recibo por node id
sumada por módulo (1350.873 s observados; 3878 node ids). El baseline previo `2026-10-23.1`
(run `39880858153`, 2260.204 s) queda como referencia histórica. Un módulo nuevo entra con el peso
por defecto positivo.

Balance medido del baseline (run `40253368902`, pesos `2026-10-23.1`): shards 424/451/284/325 s,
max/min de pared 1.588, ruta crítica 510 s, 25.42 runner-minutes, cola 10 s, overhead
7.715/10.876/10.103/10.306 s. Con los pesos nuevos el reparto predicho es
337.718/337.718/337.719/337.718 s; el run candidato publica la medición observada en
`balance-report.json` y no se declara aceleración hasta leer ese reporte. El módulo mayor mide
166.918 s frente a un ideal de 337.718 s, así que no se justifica reparto por node id: se conserva
la agrupación por módulo para preservar fixtures de ámbito módulo. Si un módulo futuro domina y se
demuestra independiente, se podrá habilitar reparto determinista por node id sólo para ese módulo,
conservando agrupaciones de fixtures/recursos y sin partición aleatoria ni duplicación.

Las pruebas nuevas de esta enmienda agregan node ids a `test_development_workflow_contract.py`, por
lo que el universo del run candidato difiere del baseline en esos casos. La comparación declara esa
diferencia; no se reutiliza el digest del universo anterior como si fuera el mismo.

Si un shard queda desbalanceado, se actualizan únicamente los pesos justificados, se versiona el
archivo y se repite la comparación con el mismo SHA o con un candidato nuevo. No se omiten tests, no
se rebaja el mínimo de cobertura y no se mueven pruebas a un job nocturno para alcanzar el objetivo.

Ante un fallo, el agregado deja los artefactos del run/attempt exacto y explica la causa. La
recuperación consiste en corregir el candidato o el reparto y publicar un SHA nuevo; nunca en
reutilizar artefactos de otro run, ignorar un outcome o publicar un gate verde sin sus cuatro
recibos.
