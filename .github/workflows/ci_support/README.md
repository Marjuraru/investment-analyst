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

## Pesos y recuperación

Los pesos iniciales provienen del run oficial `39880858153` / job `120177616448`
(`097e4e356d23f98e3e5eb01e33732ef9a69d6752`), calculados por delta de timestamps por test y sumados
por módulo (2260.204 s observados). Un módulo nuevo entra con el peso por defecto positivo. Si un
shard queda desbalanceado, se actualizan únicamente los pesos justificados, se versiona el archivo
y se repite la comparación con el mismo SHA o con un candidato nuevo. No se omiten tests, no se
rebaja el mínimo de cobertura y no se mueven pruebas a un job nocturno para alcanzar el objetivo.

Ante un fallo, el agregado deja los artefactos del run/attempt exacto y explica la causa. La
recuperación consiste en corregir el candidato o el reparto y publicar un SHA nuevo; nunca en
reutilizar artefactos de otro run, ignorar un outcome o publicar un gate verde sin sus cuatro
recibos.
