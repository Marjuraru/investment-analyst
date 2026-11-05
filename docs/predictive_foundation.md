# Fundación predictiva — experimento diario cripto, dataset PIT y reconstrucción histórica

> Autoridad técnica y operativa única de los artefactos abiertos por `PREDICTIVE-FOUNDATION`/#391,
> `PREDICTIVE-HISTORICAL-DATA`/#393, `ML-BASELINE-VALIDATION`/#395 y
> `ML-RESEARCH-RESULTS-DELIVERY`/#397 (`PREDICTIVE-RESEARCH`, `ADVANCES`, sin cierre). Estrategia y
> ruta viven en [`product_roadmap.md`](product_roadmap.md),
> [`basic_functional_release_plan.md`](basic_functional_release_plan.md) y
> [`research_information_strategy.md`](research_information_strategy.md). Este documento no autoriza
> adquisición ilimitada, entrenamiento, recomendación ni ejecución.

## Estado y alcance

`PREDICTIVE-FOUNDATION` fija una primera pregunta predictiva, construye un dataset de investigación
reproducible con separación temporal entre features y label y publica su elegibilidad real. No
entrena un modelo, no produce una predicción, no genera una señal ni una recomendación y no toca el
workspace operativo en escritura.

Existen dos modos explícitos y no intercambiables:

1. **PIT observado** (#391): consume el workspace operativo en `READ_ONLY` y sólo admite evidencia
   cuyo `available_at` ya existía en el corte. Es el único modo que puede satisfacer el gate de
   evidencia PIT observada.
2. **Reconstruido** (#393): adquiere un archivo histórico oficial acotado y reconstruye features
   causales bajo el supuesto explícito de disponibilidad al cierre de barra más una hora. Permite
   investigación retrospectiva condicional, nunca promoción, señal operacional ni sustitución del
   gate PIT observado.

Perfil soportado: cripto spot USD, barras diarias UTC y fuente inequívoca del catálogo. La primera
fuente es Coinbase diario para `crypto:btc-usd`; `crypto:sol-usd` es control de reutilización. La
lógica no tiene ramas por BTC o SOL. Equity/ETF, intradía, derivados, cross-sectional y mezcla de
fuentes o perfiles quedan fuera con error explícito. El workspace se abre siempre en
`READ_ONLY` en el modo PIT; el modo reconstruido no abre el workspace ni una conexión de escritura.

## Experimento preregistrado (P0)

| Campo | Valor fijo |
| --- | --- |
| `experiment_spec_id` | `crypto-daily-positive-return-5d-v1` |
| `target_version` | `crypto-forward-close-return-5d-v1` |
| `feature_set_version` | `crypto-market-baseline-3-v1` |
| Pregunta | Con información disponible a las 12:00 UTC de D, ¿será positivo el cambio de cierre a cierre durante los cinco intervalos diarios siguientes? |
| `decision_at` | `D 12:00:00Z` |
| Horizonte | cinco intervalos entre cierres; entrada en la barra D y salida en la barra D+5 |
| Label | `return_5d = exit_close / entry_close - 1`; `y = 1` sólo si `return_5d > 0`, `y = 0` en otro caso, incluido cero |
| Universo inicial | `crypto:btc-usd`; `crypto:sol-usd` sólo como control READ_ONLY |
| Rango de decisión | fechas inclusivas, máximo 366 fechas |
| Corte de extracción | `dataset_known_at` UTC explícito, sin default |

Una decisión por día D del rango solicitado. No se ajusta la hora ni el universo después de mirar
labels, no se imputa, no se normaliza con estadísticos aprendidos ahora y no se retrofecha un
`available_at`.

### Features

Tres columnas en orden fijo, `Decimal` de precisión 34 con `ROUND_HALF_EVEN`:

1. `close_return_1d = close[D-1] / close[D-2] - 1`, unidad `ratio`.
2. `rsi_14_fraction = RSI[D-1] / 100`, `market-rsi-incremental-v2-decimal34`, ventana 14, unidad
   upstream `index`.
3. `macd_12_26_9_histogram_close_ratio = histograma[D-1] / close[D-1]`,
   `market-macd-incremental-v2-decimal34`, `fast_window=12`, `slow_window=26`,
   `signal_window=9`, unidad upstream `USD`.

Son features independientes, sin score agregado. Los divisores deben ser estrictamente positivos y
finitos. Se reutiliza `AnalyticalMetricSnapshotSelector` y la política vigente sobre el período
exacto D−1 y el corte `decision_at`; no se le pasa todo el histórico para que rellene ausencias. Los
cierres usan selección PIT por `observed_at`, última revisión disponible al corte y rechazo de
empate ambiguo con los helpers públicos de `history_service`. Una revisión o empate ambiguo falla
cerrado. No se tocan selector, reglas ni fórmulas upstream.

Todas las revisiones y métricas deben haber estado disponibles en `decision_at` y también en
`dataset_known_at`. La barra D−1 termina a D 00:00 UTC; la decisión es D 12:00 UTC. No hay warm-up:
se consumen métricas ya persistidas, no se calculan.

### Label

`entry_close` es el cierre de la barra D 00:00Z..(D+1) 00:00Z; `exit_close` es el de la barra D+5
00:00Z..(D+6) 00:00Z. Se exige un calendario continuo de seis barras D..D+5; un hueco no se sustituye
por «el siguiente dato disponible». Sólo se consume `close`, no OHLCV ni raw.

El label usa la revisión elegible en `dataset_known_at` y
`label_available_at = max(available_at de los seis cierres seleccionados, fin de la barra de
salida)`. Si falta madurez o dato, el label queda `not_yet_available` o `missing` con causa; nunca
cero sintético y nunca selección mirando el signo. Un export posterior puede incorporar una revisión
o un label maduro con identidad nueva, conservando el export anterior.

El intervalo del label para purge es `[decision_at, exit_bar_end]`. Los precios, identificadores y
disponibilidad del label no entran en las features ni en su firma de entrada al modelo. La metadata
del label sirve al evaluador, nunca al predictor.

## Lecturas, estados y ventanas

La ventana derivada de cierres es `[start-2 días, end+6 días)` en UTC; la de métricas es
`[start-1 día, end día)` porque sólo se usa D−1. Todas las lecturas filtran
`available_at <= dataset_known_at`, activo, fuente, `field_name=close` o las dos claves métricas,
frecuencia diaria y rango. El índice de métricas se recorre por páginas de hasta 256 entradas y la
hidratación usa `get_many` con lotes de hasta 256; no hay consulta por muestra ni reescaneo del
corpus por día. Se resuelve catálogo/runtime una vez y no se consulta ningún proveedor.

Cada decisión solicitada produce una fila auditable, incluso si no es elegible. Estados separados:

- features: `available`, `missing`, `not_yet_available`, `incompatible`, `ambiguous`;
- label: `available`, `missing`, `not_yet_available`, `incompatible`, `ambiguous`.

Causas finitas: falta de cierre o métrica, disponibilidad posterior al corte, incompatibilidad de
versión/unidad/parámetros/calidad, período incompatible, hueco de calendario, madurez del label o
revisión ambigua. Una contradicción se representa como fallo explícito de la muestra y de los
conteos; nunca se elige un ganador arbitrario. Una muestra es elegible para entrenamiento sólo si
sus tres features y su label están disponibles y la fila no fue purgada por split.

## Splits, purge y readiness

La política es `predictive-temporal-split-v1`. Sobre las fechas solicitadas, ordenadas por
`decision_at`, se bloquean `train 60 % / validation 20 % / holdout 20 %` con floors para los dos
primeros conjuntos y el resto para holdout, **antes** de filtrar elegibilidad. Las fronteras no se
mueven para balancear clases.

El purge elimina de `train` y `validation` las muestras cuyo intervalo de label toque o solape el
inicio del conjunto posterior; además se aplica un embargo de cinco días UTC al inicio de
`validation` y `holdout`. Se reportan IDs, causas y conteos; una fila no se mueve al otro split para
compensar.

`research_ready` exige, antes de excluir por split, al menos 300 muestras maduras elegibles; después
de exclusión, al menos 150 `train`, 40 `validation` y 40 `holdout`; y ambas clases en `train` con al
menos 20 cada una. Son pisos de admisión operativa preregistrados, no prueba de potencia ni
garantía de promoción. Si no se alcanzan, el estado es `insufficient_pit_sample` con los conteos
reales; el bloque puede completar el constructor/export y demostrar la limitación, pero no afirmar
dataset listo ni pasar a entrenar. No se usan scratch ni sintéticos como evidencia de readiness
real.

## Evaluación preregistrada

La métrica primaria es Brier fuera de muestra frente a `p=0.5` y al prior de clase estimado sólo en
`train` de cada fold; log-loss, calibración y detalle por fold son secundarios. La regla de avance es
una mejora relativa de Brier `>= 5 %` frente a ambos baselines en validación temporal agregada,
superioridad con intervalo pareado que respete la dependencia de labels y confirmación en holdout
intacto. `ML-BASELINE-VALIDATION` (#395) fijó y ejecutó el protocolo numérico, el embargo de folds y
la disciplina de holdout antes de entrenar; ver
[Evaluación del baseline ML](#evaluación-del-baseline-ml-395). No se optimiza el criterio sobre
holdout ni se promete esa mejora. Shadow, drift, costes reales y rollback son puertas separadas. Un
LLM posterior recibe un paquete ya calculado y nunca participa en label ni feature. #391 no instaló
paquetes ML; #395 declara las dependencias de entrenamiento sólo en el extra `dev`, fuera del release
core.

## Artefactos, esquema y encoding

El export se sella en `<output-dir>/<dataset_id>/` con:

- `rows.parquet`: todas las filas y estados, orden estable `decision_at, sample_key`.
- `manifest.json`: esquema, orden, encoding, ventanas, identidad, conteos, splits, readiness,
  evidencia por fila y límites.

Columnas, en este orden exacto:

| Columna | Tipo | Nulo | Unidad/contenido |
| --- | --- | --- | --- |
| `sample_key` | text | no | UUID determinista de spec/activo/fuente/decisión. |
| `row_id` | text | no | UUID determinista con features y evidencia del label. |
| `decision_at` | text | no | ISO 8601 UTC, 12:00Z. |
| `close_return_1d` | text | sí | Decimal canónico, `ratio`. |
| `rsi_14_fraction` | text | sí | Decimal canónico, `ratio`. |
| `macd_12_26_9_histogram_close_ratio` | text | sí | Decimal canónico, `ratio`. |
| `feature_state` | text | no | Estado agregado de features. |
| `label_state` | text | no | Estado del label. |
| `label` | integer | sí | 1 sólo si `return_5d > 0`; 0 en otro caso. |
| `return_5d` | text | sí | Decimal canónico, `ratio`. |
| `entry_close` | text | sí | Decimal canónico, `USD`. |
| `exit_close` | text | sí | Decimal canónico, `USD`. |
| `entry_close_observation_id` | text | sí | UUID de la observación de entrada. |
| `exit_close_observation_id` | text | sí | UUID de la observación de salida. |
| `label_available_at` | text | sí | ISO 8601 UTC. |
| `feature_evidence_json` | text | no | JSON canónico con IDs y `available_at` por columna. |
| `label_bars_json` | text | no | JSON canónico con las seis barras. |
| `split` | text | no | `train`, `validation` o `holdout`. |
| `purged` | boolean | no | Purge por solape o embargo. |
| `purge_reason` | text | sí | `label_interval_overlap` o `embargo`. |
| `eligible` | boolean | no | Features y label disponibles, sin purge. |

`feature_encoding` es `canonical-decimal-text`; `timestamp_encoding` es `iso8601-utc`;
`id_encoding` es `uuid-text`; `order_by` es `decision_at,sample_key`. Ningún consumidor debe asumir
`float` ni `DECIMAL(38,18)` que redondee en silencio. El núcleo conserva `Decimal` exacto; la
conversión a dtype del modelo pertenece a un futuro adaptador externo explícito.

El hash semántico del dataset (`dataset_content_sha256`) se calcula sobre el contenido canónico y es
independiente del hash físico del Parquet. Los bytes del Parquet identifican ese artefacto, no
prometen igualdad entre versiones de la librería.

## Identidades y versiones

Namespace: `uuid5(NAMESPACE_URL, "investment-analyst/predictive-foundation-v1")`.

- `sample_key`: `uuid5(namespace, hash canónico({artifact, spec, activo, fuente, decision_at}))`.
- `feature_hash`: sólo inputs y valores disponibles para la decisión; excluye label y reloj de
  ejecución.
- `row_id`: `sample_key`, `feature_hash`, features y evidencia del label.
- `dataset_id`: spec, UUID del workspace, request, `split_policy` y filas ordenadas por `row_id`.

`computed_at` y el reloj de ejecución quedan fuera de las identidades; un recibo operacional puede
tener una hora fuera de la identidad semántica. Misma evidencia y corte producen los mismos IDs y
hash. Una revisión o un label nuevo cambia `row_id`/`dataset_id`, sin sobrescribir historia.

Schemas propios: `predictive-experiment-v1` (spec), `predictive-dataset-v1` (dataset/manifest
determinista) y `predictive-dataset-manifest-v1` (manifiesto sellado del artefacto).

## Modo reconstruido — archivo histórico y dataset causal (#393)

`PREDICTIVE-HISTORICAL-DATA` añade un artefacto **aislado y opt-in**. No modifica el export PIT de
#391, sus schemas, IDs, validadores ni límites. Un lector PIT previo no acepta este artefacto sin
una decisión explícita de adaptación.

### Archivo fuente

- Fuente: Coinbase Exchange, producto y `source_id` resueltos por el catálogo central
  (`resolve_historical_scope`); no hay ramas por ticker ni `source_id` sintetizado.
- Rango público inclusivo `2019-01-01..2025-12-31` → `[2019-01-01T00:00Z, 2026-01-01T00:00Z)`,
  máximo 2.560 días y un máximo de 12 GET secuenciales. El cliente Coinbase vigente pagina, filtra,
  ordena, valida `Decimal`/`raw_values` y rechaza duplicados contradictorios; no se crea otro parser
  HTTP ni otro paginador.
- `candles.jsonl` canónico conserva OHLCV, `raw_values` y `bar_start`; el manifest declara activo,
  producto, granularidad, rango, URLs, `retrieved_at`, conteos de barras y ausencias, hash físico y
  hash semántico. Un recibo completo acredita peticiones completadas, **no** que todos los días
  tengan vela. Las ausencias se conservan; nunca se rellenan con el día anterior, el siguiente ni
  un precio cero.
- Un destino existente se reutiliza sin red ni reescritura sólo tras verificar identidad,
  parámetros y checksums. Corrupción o contenido distinto falla cerrado. No hay auto-refresh ni
  borrado.
- `bar_content_id = uuid5(namespace, hash canónico(activo, fuente, frecuencia, `bar_start`, OHLCV,
  raw_values))`; excluye el reloj de cómputo. `archive_id` liga la adquisición (URLs, `retrieved_at`,
  rango) y el contenido ordenado; otra recepción produce otra identidad y nunca sobrescribe.

Condiciones de uso: el uso previsto es personal y local. El artefacto publica la URL de términos del
proveedor y no afirma licencia abierta, checksum oficial ni derecho a redistribuir.

### Features causales reconstruidas

`feature_set_version=crypto-market-reconstructed-3-v1`, `target_version` idéntico a #391,
`experiment_spec_id=crypto-daily-positive-return-5d-historical-v1` y
`availability_policy=historical-close-plus-1h-assumed-v1`. Precisión 34 `ROUND_HALF_EVEN`, sin
`float`.

Las features de la decisión D usan sólo cierres hasta D−1:

1. `close_return_1d = close[D-1] / close[D-2] - 1`.
2. `rsi_14_fraction = RSI14[D-1] / 100`.
3. `macd_12_26_9_histogram_close_ratio = histograma[D-1] / close[D-1]`.

Se reutilizan `decimal34_mean`, `ema_step` y `wilder_average_step`. RSI siembra con la media de las
primeras 14 ganancias/pérdidas sobre 15 cierres; MACD siembra EMA12/EMA26 con media simple y señal 9
con la media de las primeras 9 líneas (34 cierres mínimos). Un recorrido cronológico mantiene estado
y emite por fecha; no se llama `full_*` ni se recalcula el prefijo completo por muestra. Los tests
comparan exactamente contra `full_rsi`/`full_macd` vigentes.

Segmentos diarios contiguos: un hueco invalida las fechas afectadas y reinicia el calentamiento en el
siguiente segmento. `segment_seed_start` y `warmup_bars` quedan por fila. RSI/MACD permanecen
ausentes hasta calentamiento completo. El prefijo inicial empieza en 2019-01-01 y no se afirma
igualdad numérica con métricas operativas sembradas en otra fecha.

`assumed_available_at = fin de barra + 1 h` es una hipótesis explícita
(`availability_basis=historical_reconstruction_assumed`), no primera publicación observada ni SLA.
`strict_pit_verified=false` siempre. El feature hash liga sólo spec, política, segmento/seed y un
digest incremental del prefijo causal de `bar_content_id`/cierres hasta D−1; nunca `archive_id`,
barras futuras, label, split ni reloj del export.

### Label, split y readiness

El label conserva exactamente #391: seis barras continuas D..D+5, `entry_close` de D, `exit_close` de
D+5, fin de salida D+6, `return_5d=exit/entry−1` y `y=1` sólo si es estrictamente positivo (cero ⇒ 0).
Ausencia, fin no maduro o divisor inválido nunca se convierten en `y=0`. La evidencia del label
registra la recepción real y el fin temporal:
`evidence_available_at = max(exit_bar_end, archive.retrieved_at)`; no se retrofecha una
disponibilidad observada.

La política es `historical-temporal-split-v1`: 60/20/20 sobre la grilla solicitada antes de filtrar,
floors para train/validation, purge por solape del intervalo del label y embargo de cinco días UTC al
inicio de validation/holdout. Una fila por decisión, sin reequilibrar mirando `y`.

`reconstruction_ready` exige ≥300 maduras/elegibles antes del split, ≥150 train / ≥40 validation /
≥40 holdout después de purge/embargo y ≥20 de cada clase en train; en otro caso
`insufficient_reconstructed_sample` con los conteos reales. Son pisos de admisión para investigación
ML posterior; no son significancia, robustez, precisión ni equivalencia al `research_ready` PIT.

### Export y consumidor offline

`<output-dir>/<dataset_id>/rows.parquet` + `manifest.json` sellados de forma atómica. Los números
financieros son texto decimal canónico; `y` es entero o nulo; los timestamps son UTC. Las columnas
separan identificadores, las tres features, el target, la metadata futura del label, el split y la
elegibilidad; el manifest declara orden, unidades, versiones, schema, fronteras de split, supuestos,
referencia content-addressed al archivo único y los hashes físico/semántico. El archivo fuente no se
copia dentro del dataset.

El consumidor offline `read_historical_dataset_view` verifica el manifest y lee proyecciones
explícitas: sólo las tres features, sólo el target o sólo la metadata. La conversión a `float` o a
un dtype de modelo pertenece a un adaptador externo futuro; no se crea aquí.

## Comandos exactos

Desde la raíz del repositorio, con intérprete explícito y `PYTHONPATH=src`:

```bash
# Resumen READ_ONLY: no escribe nada.
PYTHONPATH=src .venv/bin/python scripts/build_predictive_dataset.py \
  --workspace /home/marjuraru/.local/share/investment-analyst/workspaces/active-v2-20261021 \
  --asset-id crypto:btc-usd \
  --start-date 2026-08-03 --end-date 2026-10-31 \
  --known-at 2026-10-31T23:21:30.756664Z \
  --json

# Export explícito, fuera del workspace. Repetir el mismo comando reutiliza el artefacto.
PYTHONPATH=src .venv/bin/python scripts/build_predictive_dataset.py \
  --workspace /home/marjuraru/.local/share/investment-analyst/workspaces/active-v2-20261021 \
  --asset-id crypto:btc-usd \
  --start-date 2026-08-03 --end-date 2026-10-31 \
  --known-at 2026-10-31T23:21:30.756664Z \
  --output-dir /home/marjuraru/.local/share/investment-analyst/research/predictive-foundation \
  --json
```

`--output-dir` debe ser absoluto, resolverse fuera del workspace, no ser la raíz del sistema y no
estar ocupado por contenido distinto. El destino existente válido del mismo dataset se verifica y
reutiliza sin rewrite; contenido distinto o corrupto se rechaza, no se sobrescribe ni se repara. El
sellado es atómico: el directorio final sólo aparece con Parquet y manifest completos; una
interrupción deja sólo temporales propios, que se retiran.

Readback seguro, sólo conteos y metadatos (no imprime valores de holdout):

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path
import duckdb
from investment_analyst.research.predictive_export import read_predictive_export

root = Path("/home/marjuraru/.local/share/investment-analyst/research/predictive-foundation")
dataset_dir = next(path for path in root.iterdir() if path.is_dir())
manifest = read_predictive_export(dataset_dir)
connection = duckdb.connect(":memory:")
rows = connection.execute(
    "SELECT count(*), min(decision_at), max(decision_at) FROM read_parquet(?)",
    [str(dataset_dir / "rows.parquet")],
).fetchone()
connection.close()
print(manifest.dataset.dataset_id, manifest.dataset.counts, manifest.dataset.readiness.status)
print(rows)
PY
```

### Modo reconstruido (#393)

```bash
# Adquisición finita: 9 GET como máximo, sella el archivo y reintenta sin red si ya existe.
PYTHONPATH=src .venv/bin/python scripts/build_historical_research_dataset.py acquire \
  --asset-id crypto:btc-usd \
  --start-date 2019-01-01 --end-date 2025-12-31 \
  --output-dir /home/marjuraru/.local/share/investment-analyst/research/predictive-historical-data \
  --json

# Resumen sin escribir: no requiere output-dir ni abre el workspace.
PYTHONPATH=src .venv/bin/python scripts/build_historical_research_dataset.py build \
  --archive-dir /home/marjuraru/.local/share/investment-analyst/research/predictive-historical-data/<archive_id> \
  --start-date 2020-01-01 --end-date 2025-12-25 \
  --known-at 2026-11-01T02:37:32.364526Z \
  --json

# Export aislado, fuera del workspace, runtime, release y repositorio.
PYTHONPATH=src .venv/bin/python scripts/build_historical_research_dataset.py build \
  --archive-dir /home/marjuraru/.local/share/investment-analyst/research/predictive-historical-data/<archive_id> \
  --start-date 2020-01-01 --end-date 2025-12-25 \
  --known-at 2026-11-01T02:37:32.364526Z \
  --output-dir /home/marjuraru/.local/share/investment-analyst/research/predictive-historical-data/datasets \
  --json
```

`build` no acepta `--workspace`, no abre el runtime ni una conexión de escritura y ejecuta desde
cualquier cwd porque todos los paths se resuelven absolutos. Rechaza destinos dentro del
repositorio, workspace, runtime, release u `ops`, y cualquier componente simbólico. Verificación
offline:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path
from investment_analyst.research.historical_export import read_historical_dataset_view

root = Path("/home/marjuraru/.local/share/investment-analyst/research/predictive-historical-data/datasets")
dataset_dir = next(path for path in root.iterdir() if path.is_dir())
view = read_historical_dataset_view(dataset_dir)
print(view.manifest.dataset.readiness.status, len(view.features), len(view.targets))
print(view.features[0])
PY
```

## Evidencia real (2026-10-31)

Workspace `70c64668-1738-4dfe-ba1f-7e72026bb7fe` (`active-v2-20261021`), BTC
`2026-08-03..2026-10-31`, corte `2026-10-31T23:21:30.756664Z`, `READ_ONLY`:

- 90 decisiones; 1 feature `available`, 89 `not_yet_available`; 0 `missing`, 0 `incompatible`, 0
  `ambiguous`.
- 84 labels maduros, 6 `not_yet_available`; 0 `missing`, 0 `incompatible`, 0 `ambiguous`.
- 0 elegibles antes de split; 22 filas purgadas por calendario (11 train, 11 validation).
- Splits propuestos: train 54, validation 18, holdout 18; elegibles tras purge: 0/0/0.
- Readiness: `insufficient_pit_sample` (0 < 300; train 0 < 150; validation 0 < 40; holdout 0 <
  40; clases de train 0/0 < 20).
- Los `available_at` de los cierres van de `2026-10-21T20:44:03.560911Z` a
  `2026-10-31T15:47:54.039626Z`; la única feature disponible tiene `available_at`
  `2026-10-27T01:54:50.001801Z`. La historia de precios existe, pero la historia entrenable PIT no.
- SOL como control READ_ONLY reprodujo la misma carencia: 90 decisiones, 1 feature disponible, 89
  no disponibles, 84 labels maduros, 6 pendientes, 0 elegibles.

Export BTC: `dataset_id=5417a46d-61f1-56ed-b008-8f9fadf5d5a4`, 90 filas, Parquet 54 526 B,
manifest 306 317 B, total 360 843 B. Una segunda ejecución idéntica reutilizó el mismo `dataset_id`,
`rows_sha256`, `manifest_sha256` y no cambió el fingerprint del workspace (DB, manifest y conteo de
archivos/bytes). El manifest sellado se releyó con hash semántico coincidente. No se imprimieron
valores ni targets de holdout.

## Evidencia real reconstruida (2026-11-01)

Adquisición única por Coinbase público, sin credenciales:

- `archive_id=67ec4e68-33f9-53ef-82bb-032258b57786`,
  `semantic_sha256=a5e3b4dc24e572b14b86d110aecb40aec935be2947a31300e5d739bd30efac18`,
  `candles_sha256=6a6ecb1250347b6d235facd94dd3eb24471674ae1aca8ff9ef0cd0dbed48e999`.
- 2.557 días solicitados, 2.557 barras, 0 ausencias, 9 GET secuenciales; primer cierre
  `2019-01-01 3826.1 USD`, último `2025-12-31 87497.94 USD`; `retrieved_at`
  `2026-11-01T02:37:32.364526Z`; `candles.jsonl` 712.767 B y manifest 3.237 B.
- Adquisición medida: 6,61 s de pared y pico RSS 109.388 KiB.

Dataset reconstruido, `known_at=2026-11-01T02:37:32.364526Z`, decisiones
`2020-01-01..2025-12-25`:

- `dataset_id=d927c7f1-794a-571c-b729-6bb68d311115`,
  `dataset_content_sha256=16b895451e45d9b286c58779adc501b5707a05c4503aacab99ca88694fcab4f0`.
- 2.186 decisiones; 2.186 features `available` y 2.186 labels maduros; 0 `missing`, 0
  `not_yet_available`, 0 `incompatible`; 2.186 elegibles antes del split y 22 purgadas
  (11 train, 11 validation por calendario).
- Splits propuestos 1.311 / 437 / 438; elegibles tras purge 1.300 / 426 / 438; clases de train
  699 positivas / 601 negativas.
- Readiness `reconstruction_ready` con `strict_pit_verified=false` y
  `availability_basis=historical_reconstruction_assumed`. No es una validación estadística ni una
  señal.
- `rows.parquet` 1.303.425 B, `manifest.json` 6.369.517 B; el dataset completo 7.672.942 B y la raíz
  total 8.388.946 B, bajo el cap de 32 MiB. Reconstrucción y export medidos: 7,32 s de pared y pico
  RSS 236.616 KiB.
- Una repetición idéntica sin red devolvió `reused`, el mismo `rows_sha256`
  `eaf3d5f85ceedcc6df24b59ffe7e68d5b20beaefb2f8ad478510a89bc1463565`, el mismo `manifest_sha256`
  `fd56e0917e1ef3133ad7543d3f77541fdb41d82df8e701252a84c88982961444` y no cambió los `mtime`.
- El consumidor offline releyó 2.186 features, 2.186 targets y 2.186 metadatos con `Decimal`
  exacto y sin cruzar features con el label.

El workspace operativo no se abrió. Su metadata antes/después es idéntica: `index.duckdb`
693.383.168 B, mismo `mtime` e inodo; `manifest.json` y `state/**` sin cambios; 4.098.119.661 B y
870.825 archivos en ambas lecturas; no apareció WAL. El delta atribuible al bloque es 0; la ventana
no registró actividad concurrente del scheduler.

## Evaluación del baseline ML (#395)

`ML-BASELINE-VALIDATION` añade un estudio aislado y opt-in sobre el dataset reconstruido sellado. No
abre el workspace, no adquiere datos, no modifica el dataset, sus features, labels, splits ni el gate
PIT, y no produce señal, diagnóstico, recomendación ni predicción operativa.

### Protocolo, folds y decisión

`ml-baseline-protocol-v1` con `protocol_id=crypto-historical-5d-baselines-v1`; `protocol.json` se
sella antes del primer fit. Entrada exclusiva: las tres columnas de
`crypto-market-reconstructed-3-v1`; target `return_5d > 0`; `dtype=float64`; un único hilo
BLAS/OpenMP; máximo 6 fits de desarrollo y 1 refit de holdout.

Tres folds walk-forward expansivos con índices sobre todas las fechas solicitadas antes de filtrar:
`a=⌊0.5N⌋`, `b=train_end` original, `c=b+⌊(holdout_start−b)/2⌋`, `d=holdout_start` original;
validaciones `[a,b)`, `[b,c)`, `[c,d)`. Cada etiqueta de entrenamiento exige además
`exit_bar_end < medianoche_UTC(inicio_validación) − 5d`; se conservan el purge y la elegibilidad
originales. Dos candidatos fijos —regresión logística con `StandardScaler` ajustado sólo en train e
histogram gradient boosting— y dos referencias: `p=0.5` y el prior de clase del train de cada fold.

`Brier` decide; log-loss con clip `1e-15` y calibración de 10 bins son secundarios. La incertidumbre
usa un bootstrap pareado de bloques móviles de 20 decisiones consecutivas, 2.000 réplicas y
`PCG64(1729)`, dentro de cada segmento diario contiguo de cada fold. En desarrollo se exige mejora
relativa `>=5 %` frente a **ambas** referencias y ambos límites inferiores unilaterales (quantile
`0.0125`, Bonferroni `0.05/4`) `> 0`; el holdout usa quantile `0.025`. Un candidato admisible se
sella en `selection.json` antes de abrir el holdout.

### Artefacto y comandos

`<output-dir>/<run_id>/` contiene `protocol.json`, `selection.json` cuando procede,
`predictions.parquet` (sólo OOS; holdout sólo si se abrió), `report.json` y `manifest.json` con
hashes. El `run_id` liga dataset, protocolo, código material ML y versiones efectivas; no depende de
paths ni del reloj de ejecución. Un artefacto sellado se reutiliza sin fitting ni rewrite; protocolo,
selección o predicciones divergentes fallan cerrado.

```bash
PYTHONPATH=src .venv/bin/python scripts/evaluate_ml_baselines.py \
  --dataset-dir /home/marjuraru/.local/share/investment-analyst/research/predictive-historical-data/datasets/d927c7f1-794a-571c-b729-6bb68d311115 \
  --output-dir /home/marjuraru/.local/share/investment-analyst/research/ml-baseline-validation \
  --json
```

La lectura del artefacto sellado no requiere dependencias ML:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path
from investment_analyst.research.ml_export import read_ml_baseline_artifact
manifest, report = read_ml_baseline_artifact(Path("<artifact_dir>"))
print(manifest.run_id, report.status, report.development.decision)
PY
```

### Resultado real (2026-11-01)

`run_id=fc4ea570-4ffb-55fc-ac49-17d74c328d24`, `protocol_sha256=e7256118…`, 2.186 decisiones,
`status=completed`, `decision=no_demonstrated_edge`, `holdout_status=not_evaluated`, 6 fits de
desarrollo, 0 de holdout y 0 accesos al holdout. 3,68 s de pared, pico RSS 256.180.224 B, artefacto
45.176 B. El holdout nunca se consultó.

| Fold | train | purge | embargo | eval | logística | HGB | `p=0.5` | prior |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1.082 | 0 | 11 | 207 | 0,251195 | 0,242597 | 0,250000 | 0,250418 |
| 2 | 1.300 | 11 | 0 | 218 | 0,240791 | 0,260389 | 0,250000 | 0,243467 |
| 3 | 1.507 | 11 | 11 | 208 | 0,256396 | 0,251660 | 0,250000 | 0,251947 |
| Pooled | — | — | — | 633 | 0,249321 | 0,251702 | 0,250000 | 0,248527 |

Pooled OOS: 343 positivas / 290 negativas. Mejora relativa de la logística `+0,27 %` frente a
`p=0.5` y `−0,32 %` frente al prior, con límites inferiores `−0,006855` y `−0,003212`; HGB `−0,68 %`
y `−1,28 %`, con `−0,016330` y `−0,016935`. Ningún candidato alcanza el 5 % frente a ambas
referencias y el prior de clase es la mejor referencia. No se ajustó ningún parámetro, no se probó
otra longitud de bloque ni otro umbral, y no se abrió el holdout para buscar un ganador.

Reutilización real: una segunda ejecución devolvió `status=reused` en 2,01 s con los mismos hashes de
`protocol.json`, `predictions.parquet`, `report.json` y `manifest.json`; no hubo fitting, red ni
rewrite. El dataset `d927c7f1…` conservó exactamente sus hashes antes y después (`manifest.json` y
`rows.parquet` verificados). El lector core releyó el artefacto sin `sklearn`, `numpy`, `scipy` ni
`threadpoolctl`.

Un resultado negativo es una entrega válida del bloque: no demuestra una ventaja del baseline
técnico y no promueve ningún predictor. La decisión siguiente pertenece a PLAN: o bien una hipótesis
de datos/features distinta sustentada por investigación, o el cierre de este experimento; no se
encadena tuning, más indicadores ni nuevos activos para buscar un ganador.

## Consulta local del resultado ML (#397)

`ML-RESEARCH-RESULTS-DELIVERY` integra la consulta verificable de un run sellado en la API y la
interfaz técnica local. La vista es un consumidor read-only de `ml-baseline-manifest-v1`,
`ml-baseline-protocol-v1` y `ml-baseline-evaluation-v1`: verifica el artefacto, comprueba que el
`asset_id` solicitado coincide con el protocolo y proyecta el resultado con el contrato aislado
`ml-research-report-view-v1`. No reejecuta la investigación, no entrena, no adquiere datos, no abre
el dataset, no consulta el holdout y no promueve un predictor.

### Configuración y comandos

La raíz se configura una sola vez del lado servidor. El default es
`~/.local/share/investment-analyst/research/ml-baseline-validation`; debe ser absoluta y la raíz no
se crea en startup, GET ni consulta.

```bash
PYTHONPATH=src .venv/bin/python scripts/serve_investment_analyst.py \
  --ml-results-root /home/marjuraru/.local/share/investment-analyst/research/ml-baseline-validation
```

La consulta HTTP exacta, con un único `asset_id` y un único `run_id` canónico, es:

```text
GET http://127.0.0.1:8765/api/v1/research/ml-report?asset_id=crypto:btc-usd&run_id=fc4ea570-4ffb-55fc-ac49-17d74c328d24
```

El identificador se toma del recibo de evaluación. En la interfaz técnica, el disclosure
«Evaluación ML de investigación» usa el activo seleccionado y sólo carga al pulsar «Consultar
evaluación»; `?ml_run_id=<uuid>` puede prellenar el campo, sin ejecutar la consulta ni cambiar de
activo. Enlace real para el run sellado y BTC:

```text
http://127.0.0.1:8765/?ml_run_id=fc4ea570-4ffb-55fc-ac49-17d74c328d24
```

### Contrato, errores y límites

- `400` para parámetros ausentes, duplicados, desconocidos, UUID no canónico o activo fuera del
  catálogo; `404` para raíz o run inexistente; `409` para artefacto incompatible/corrupto, `asset_id`
  distinto o servicio no configurado. No hay fallback a otro run.
- La resolución es exactamente `<root>/<run_id>`, sin búsqueda recursiva ni selección por `mtime`.
  Se rechazan symlinks, componentes que escapan de la raíz, entradas inesperadas, descriptores
  sustituidos por directorios y crecimiento durante la lectura.
- Caps antes de leer: manifest y protocolo ≤64 KiB, reporte y selección ≤1 MiB, predicciones ≤8 MiB
  y conjunto ≤8 MiB incluyendo el manifest. El lector público `read_ml_baseline_artifact` sigue
  siendo la verificación completa; el servicio añade la comprobación de protocolo, asset y run.
- La caché conserva sólo la proyección y las firmas SHA-256 de los archivos declarados, con máximo
  8 entradas y 2 MiB. Un cambio de contenido invalida aunque se restaure el `mtime`; una reapertura
  válida no repite DuckDB, fitting ni un nuevo readback Parquet. Las peticiones idénticas
  concurrentes se coalescen.
- La proyección no incluye predicciones por fila, retornos, labels, matrices ni rutas absolutas. El
  protocolo sellado trae `asset_id`, `dataset_id` y hashes, pero no `source_id`, venue ni fecha de
  publicación: la vista declara esa ausencia y no la infiere del ticker.
- `strict_pit_verified=false` y `availability_basis=historical_reconstruction_assumed` se muestran
  de forma visible. Brier, comparaciones, intervalos, folds, calibración, limitaciones y coste
  observado son los del artefacto; un holdout `not_evaluated` no produce métricas, filas ni clases.
- Un `confirmed_research_edge` de fixture se presenta como mejora retrospectiva condicional, nunca
  como autorización de uso. La interfaz no incluye botón de evaluación, ranking, semáforo ni
  recomendación.

### Resultado real (2026-11-02)

Smoke premerge READ_ONLY sobre `run_id=fc4ea570-4ffb-55fc-ac49-17d74c328d24`, activo
`crypto:btc-usd`, servidor aislado en puerto libre y sin segundo writer del workspace:

- Artefacto de 45.176 B y 4 archivos; reporte `b0be6b59…`; `status=completed`,
  `decision=no_demonstrated_edge`, `holdout=not_evaluated`, 0 accesos al holdout, 2.532 filas OOS
  selladas y predictor no promovido.
- Primera apertura 15,32 ms; reapertura 0,36 ms; primera respuesta HTTP 19,05 ms. Una única
  verificación completa, 7 escaneos de firma, 316.232 B de firma (7 × 45.176 B, sólo el run
  solicitado), caché de 1 entrada / 17.932 B y pico RSS 132.808.704 B.
- El asset negativo `equity:us:aapl` devolvió `409 ml_report_conflict` sin filtrar rutas. El
  artefacto sellado y el dataset `d927c7f1…` conservaron tamaño, `mtime` y SHA-256 antes/después.
- El lector funcionó en un intérprete con `sklearn`, `numpy`, `scipy` y `threadpoolctl` bloqueados
  por `sys.meta_path`; ninguno quedó importado. No hubo entrenamiento, run nuevo, escrituras,
  proveedores ni lectura de otro run.

## Evidencia histórica de funding (#399)

`PREDICTIVE-FUNDING-DATA` prepara una evidencia diferencial para una hipótesis nueva: la presión de
financiación del perpetuo puede aportar información sobre el mismo retorno spot futuro de cinco
intervalos. No se afirma causalidad económica, ventaja predictiva ni rentabilidad negociable. La
construcción es una entrega de evidencia y no entrena, no evalúa el holdout y no promueve un
predictor; tampoco reabre el experimento de tres features técnicas cerrado como negativo.

### Frontera y contratos

El bloque reutiliza `DeribitClient.fetch_funding_history`, `resolve_deribit_configuration`, el
catálogo central y las operaciones Decimal34 vigentes. No crea un provider, catálogo o indicador
nuevo, no escribe `RawRecord`, `NormalizedObservation`, `MetricResult` ni `DiagnosticResult`, y no
abre `ApplicationRuntime` ni la DB del workspace. El spot Coinbase y el funding Deribit no se
fusionan como una serie de precios: son evidencia de dominios distintos referida al mismo
subyacente por el catálogo.

Los contratos aislados son `research-funding-archive-v1` (archivo de páginas),
`research-funding-features-v1` (manifiesto semántico del complemento) y
`research-funding-bundle-v1` (manifiesto físico exportado). `feature_set_version` es
`deribit-funding-sums-24h-168h-reconstructed-v1`; `availability_policy` es
`historical-funding-end-plus-1h-assumed-v1`; `strict_pit_verified=false` en todos los artefactos.
Una revisión obtenida hoy puede diferir de lo publicado entonces: el archivo conserva
`retrieved_at`, no una primera publicación demostrada.

### Archivo de páginas

El rango se deriva de la grilla del dataset reconstruido, nunca de labels. Para decisión D a las
12:00 UTC el ancla es D 00:00 UTC; la ventana de 24 h contiene D00−23 h..D00 y la de 168 h contiene
D00−167 h..D00. Para `2020-01-01..2025-12-25` el intervalo es
`[2019-12-25T01:00:00Z, 2025-12-25T01:00:00Z)`, con 2.186 decisiones y 52.608 puntos horarios
máximos. La adquisición usa páginas lógicas de hasta 743 horas (31 días menos una hora), una por
request, secuenciales, con el pacing y el transporte existentes, y un máximo de 72 páginas. El
modelo de archivo conserva la admisión histórica de 31 días para leer recepciones ya selladas. Un
fallo de página no sella un archivo parcial.

`pages.jsonl` conserva por fila `page_index`, `point_id`, `timestamp` y el `raw_payload` con los
cinco campos exactos como texto. El manifiesto guarda el contexto común: identidad de catálogo,
método, rango, URL y tiempos reales de cada página, hash y bytes por página, hash y bytes totales,
conteos de puntos observados/ausentes y referencias a las condiciones del proveedor. Un duplicado
conflictivo, un timestamp desalineado o una página incompleta fallan explícitamente; un hueco es una
ausencia, nunca un funding cero. El archivo completo se sella atómicamente y se reutiliza sin red
cuando la solicitud coincide.

### Features y complemento

Las columnas son `funding_sum_1h_24h` y `funding_sum_1h_168h`, unidad `ratio`. Cada valor es la suma
cronológica exacta de `interest_1h` en la ventana fija, con Decimal34 y `ROUND_HALF_EVEN`; no se
suman `interest_8h` solapados, no se anualiza, no se compone retorno, no se cambia el signo y no se
estandariza. Un cero válido es un valor disponible; un hueco, un nulo o una desalineación es
`missing` con motivo. La ventana de 168 h nunca se acorta ni se rellena con la de 24 h. La
elegibilidad por feature y la elegibilidad conjunta se publican separadas.

El `funding_feature_hash` cubre `sample_key`, `decision_at`, la ventana causal, `interest_1h`, la
fuente y la política. No incluye labels, precios ni el hash del dataset base. Actualizar el label del
dataset reconstruido crea un bundle nuevo, pero no cambia el hash de funding; cambiar un input de la
ventana sí lo cambia. El bundle referencia el dataset `d927c7f1…` por identidad y hash y no copia sus
features, labels, precios ni filas completas. La vista offline devuelve features base y funding por
`sample_key`, mantiene targets y metadata separados y sólo materializa TRAIN/VALIDATION; el holdout
se verifica y se cuenta, nunca se entrega a una matriz de entrenamiento.

### Comandos exactos

```bash
PYTHONPATH=src .venv/bin/python scripts/build_funding_research_dataset.py acquire \
  --dataset-dir /home/marjuraru/.local/share/investment-analyst/research/predictive-historical-data/datasets/d927c7f1-794a-571c-b729-6bb68d311115 \
  --asset-id crypto:btc-usd \
  --output-dir /home/marjuraru/.local/share/investment-analyst/research/predictive-funding-data/archive \
  --json

PYTHONPATH=src .venv/bin/python scripts/build_funding_research_dataset.py build \
  --dataset-dir /home/marjuraru/.local/share/investment-analyst/research/predictive-historical-data/datasets/d927c7f1-794a-571c-b729-6bb68d311115 \
  --archive-dir /home/marjuraru/.local/share/investment-analyst/research/predictive-funding-data/archive/95c542f0-07f9-57b7-9882-dc72f3d99d77 \
  --known-at 2026-11-02T17:30:00Z \
  --output-dir /home/marjuraru/.local/share/investment-analyst/research/predictive-funding-data/bundle \
  --json
```

Repetir el mismo `acquire` reutiliza el archivo sin GET; repetir el mismo `build` con `--output-dir`
reutiliza el bundle sin reescritura. Sin `--output-dir`, `build` devuelve el resumen sin escribir.
El destino debe ser absoluto, fuera del repositorio, workspace, runtime y ops, sin symlinks ni
carpetas ajenas ocupadas.

### Resultado real (2026-11-02)

El archivo real `95c542f0-07f9-57b7-9882-dc72f3d99d77` (semántico `e7cbd0de…`, páginas
`e499745c…`) tiene 71 páginas, 52.536 puntos observados y 72 ausencias sobre 52.608 esperados;
`pages.jsonl` ocupa 14.629.547 B y el manifiesto 41.171 B, dentro del cap de 32 MiB. La recepción
terminó en `2026-11-02T17:21:37.414270Z`. La adquisición tardó 127,52 s de pared (suma de tiempos de
página 116,012 s; mínimo 1,003 s, máximo 4,558 s, media 1,634 s) y el pico RSS fue 487.060 KB. Una
repetición con transporte prohibido reutilizó el archivo en 5,07 s sin ninguna llamada de red.

El bundle real `06f96d38-4c77-5257-ba9f-faf8badd603e` (contenido `a861289b…`) contiene 2.186 filas;
el Parquet ocupa 782.876 B y el manifiesto 2.586.791 B, dentro del cap de 8 MiB. La raíz completa
ocupa 18.040.385 B, bajo el cap de 64 MiB. La construcción y export tardaron 11,66 s con pico RSS
372.636 KB; una segunda ejecución reutilizó el mismo bundle en 8,94 s sin reescribir archivos. La
relectura offline tardó 0,770 s y la vista TRAIN/VALIDATION 1,779 s con RSS 204.541.952 B y sin
importar `sklearn`, `numpy`, `scipy` ni `threadpoolctl`. El dataset base y su archivo conservaron sus
hashes.

Cobertura observada: 24 h disponibles 2.116 y missing 70; 168 h disponibles 1.700 y missing 486;
conjunta 1.700; `base_eligible` 2.164 y `base_eligible_joint_available` 1.681. Por split
(requested/base_eligible/joint): train 1.311/1.300/1.006, validation 437/426/328 y holdout
438/438/347. La admisión es `insufficient_funding_coverage`: 1.334/1.726 = 0,772885… frente al piso
0,95. Los pisos de train ≥150, validation ≥40 y holdout ≥40 se cumplen; sólo falla la cobertura
conjunta. No se rebaja el umbral, no se cambia el período ni el proveedor y no se rellena ningún
hueco. La construcción correcta es una entrega válida aunque la admisión sea insuficiente.

La vista TRAIN/VALIDATION devuelve 1.748 filas con features y targets separados; 438 filas de holdout
quedan fuera y su disponibilidad sólo se cuenta. No se entrena, no se calcula correlación,
performance, selección de clases ni target de holdout, y no se declara una señal o recomendación.

### Corrección de paginación y nueva recepción (2026-11-02)

La recepción anterior `95c542f0…` se conserva como evidencia histórica de #399. La comparación por
timestamp y payload Decimal mostró que 70 de sus 72 ausencias coincidían con inicios de página de 31
días y sólo `2020-08-27T06:00Z` y `07:00Z` eran huecos internos.

La nueva recepción `9d6c0972-984d-5dac-a3ad-5f7ef3d05973` (semántico `bf5dbd75…`, páginas
`3281f58c…`) usa páginas de 743 h y un request por página. Tiene 71 páginas, 52.606/52.608 puntos
observados, `pages.jsonl` de 14.649.075 B y manifiesto de 41.170 B; terminó en
`2026-11-02T22:36:01.797130Z` tras 71 requests, 107,72 s y un pico RSS de 488.376 KB. Recupera las 70
ausencias de borde, conserva las dos internas, no añade ausencias en inicios de página y no retira ni
revisa ningún punto.

El bundle `fca740b8-8f38-5703-8006-e6a467bfb3ba` (contenido `2c626662…`) se construyó con
`known_at=2026-11-02T22:36:16Z`: 2.186 filas, Parquet de 786.546 B y manifiesto de 2.491.635 B, en
11,16 s con pico RSS de 371.188 KB. La raíz completa ocupa 17.968.426 B. La admisión pasa a
`funding_data_ready` con 1.719/1.726 = 0,995944… frente al piso 0,95; train 1.293, validation 426 y
holdout 438. Permanecen 1 fila sin `funding_sum_1h_24h` y 7 sin `funding_sum_1h_168h` por los dos
huecos internos; no se imputa ninguna. La vista TRAIN/VALIDATION conserva 1.748 filas y el holdout
sigue fuera. Este resultado habilita un experimento de funding como siguiente decisión de PLAN; no
se entrena, no se evalúa el holdout y no se promueve un predictor.

### Limitaciones y siguiente decisión

- El funding se reconstruye con la recepción actual; no demuestra la primera publicación histórica
  ni satisface el gate de PIT observado.
- La disponibilidad es un supuesto de fin de ventana más una hora, no un SLA ni una observación de
  recepción por punto.
- En la recepción histórica de #399 se conservaban 72 huecos; la recepción corregida conserva sólo
  los dos internos. Una ventana de 168 h que contenga uno de ellos queda `missing`, aunque la de 24 h
  sí esté disponible. No se acorta ni se interpola.
- El uso es personal y local; no se afirma licencia abierta ni derecho de redistribución.
- La admisión de #399 era insuficiente; la recepción corregida alcanza `funding_data_ready`, que
  tampoco es una prueba estadística ni autoriza por sí solo un predictor. El siguiente PLAN decide
  el experimento de funding; si lo autoriza, deberá congelar una comparación nueva que compare el
  aporte incremental del funding frente a referencias y control técnico sobre exactamente las mismas
  fechas, sin tuning ni promoción automática.

## Validación incremental de funding (#405)

`ML-FUNDING-VALIDATION` ejecuta el experimento que #403 habilitó, sin reabrir #395 y sin abrir el
holdout. La hipótesis única es que las sumas causales `funding_sum_1h_24h` y `funding_sum_1h_168h`
aportan información incremental para `P(return_5d > 0)` de BTC spot frente al mismo control técnico
de tres features, sobre exactamente las mismas decisiones. Es investigación retrospectiva
reconstruida (`strict_pit_verified=false`); no es señal, recomendación, rentabilidad ni promoción.

### Frontera y contratos

Los contratos aislados son `ml-funding-protocol-v1`, `ml-funding-evaluation-v1` y
`ml-funding-manifest-v1`, con `protocol_id=crypto-historical-5d-funding-increment-v1`. Se reutilizan
el lector de funding, `development_fold_boundaries` y `MlLogisticParameters`; el adaptador ML declara
un ancho `Literal[3,5]` con default 3 aplicado sólo a logística, y el histograma conserva y exige 3.
No se adapta el report/service/UI v1 ni se afirma que entienden este artefacto. No hay dependencias
nuevas ni un framework genérico de evaluaciones.

`run_id = uuid5(namespace ml-funding-validation-v1, protocol_sha256, dataset_id,
dataset_content_sha256, bundle_id, bundle_content_sha256, code_material_sha256, environment)`. El
hash de código cubre, en orden literal, `ml_funding_models.py`, `ml_funding_evaluation.py`,
`ml_funding_export.py`, `ml_adapter.py`, `ml_models.py`, `funding_export.py`, `funding_models.py`,
`historical_export.py` y `historical_models.py`; no incluye paths absolutos ni reloj.

### Protocolo congelado

La entrada exclusiva es el dataset `d927c7f1…` (contenido `16b89545…`) y el bundle `fca740b8…`
(contenido `2c626662…`). Una sola máscara `eligibility_joint` se aplica a ambos brazos, en train y en
evaluación; no hay imputación, cambio de período, selección de régimen ni reetiquetado.
`technical_logistic` usa las tres features técnicas; `funding_logistic` usa las mismas tres en el
mismo orden más las dos sumas de funding. Un único algoritmo: `StandardScaler` ajustado sólo en train
y `LogisticRegression` con los parámetros vigentes (C=1, l1_ratio=0, lbfgs, intercepto, max_iter=1000,
tol=1e-8, sin class_weight, semilla 1729, penalty omitido). Las referencias simples son
`half_probability` y el prior positivo del mismo train elegible de cada fold.

Tres folds expansivos se calculan con `development_fold_boundaries(2186)` antes de filtrar:
`[1093,1311)`, `[1311,1529)` y `[1529,1748)`; se preservan el purge original y el embargo
`exit_bar_end < inicio_validación - 5d`. Los pisos son train ≥300, cada clase ≥20, evaluación ≥100 y
cada segmento diario contiguo ≥20. El protocolo sella los conteos esperados antes del primer fit; un
piso, convergencia o contrato fallido se registra como no evaluable/error, nunca como evidencia
negativa estadística ni éxito. El máximo es 6 fits de desarrollo (2 brazos × 3 folds), con cero
refits y cero fits de holdout, y BLAS/OpenMP fijados a 1 hilo.

El Brier medio decide; log-loss con clip 1e-15 y calibración de 10 bins son descriptivos y no cambian
el ganador. `delta = loss_referencia - loss_funding`; positivo significa mejora. La incertidumbre es un
bootstrap pareado de bloques móviles de 20 decisiones, 2000 réplicas, PCG64(1729), dentro de los
segmentos diarios contiguos de cada fold, sin cruzar huecos ni folds, con los mismos draws para todos
los scorers y truncamiento del último bloque. Los límites inferiores unilaterales usan quantile 0,0125
en las tres comparaciones.

`incremental_evidence_observed` exige mejora relativa de Brier ≥5 % frente a AMBAS referencias
simples, delta de Brier frente al control técnico > 0 y los TRES límites inferiores > 0. En cualquier
otro caso la decisión es `no_demonstrated_increment`. Es evidencia de desarrollo del experimento
declarado, no significancia global ni licencia para abrir el holdout, que permanece cerrado con
`not_evaluated`, 0 accesos y 0 fits.

### Comandos exactos

```bash
PYTHONPATH=src .venv/bin/python scripts/evaluate_ml_funding.py evaluate \
  --dataset-dir /home/marjuraru/.local/share/investment-analyst/research/predictive-historical-data/datasets/d927c7f1-794a-571c-b729-6bb68d311115 \
  --funding-bundle-dir /home/marjuraru/.local/share/investment-analyst/research/funding-boundary-coverage/bundle/fca740b8-8f38-5703-8006-e6a467bfb3ba \
  --output-dir /home/marjuraru/.local/share/investment-analyst/research/ml-funding-validation \
  --json

PYTHONPATH=src .venv/bin/python scripts/evaluate_ml_funding.py read \
  --artifact-dir /home/marjuraru/.local/share/investment-analyst/research/ml-funding-validation/8dbc6330-1ff2-51bc-b701-a65611afa495 \
  --json
```

No existen flags de parámetros, modelo ni holdout. `read` y `--help` funcionan sin `sklearn`,
`numpy`, `scipy` ni `threadpoolctl`. El destino debe ser absoluto, fuera del repositorio, workspace,
runtime y ops, sin symlinks ni destinos ajenos ocupados.

### Resultado real (2026-11-03)

El run `8dbc6330-1ff2-51bc-b701-a65611afa495` (protocolo `653d026d…`, reporte `8154507e…`,
manifiesto `7540484f…`) terminó `completed` con decisión `no_demonstrated_increment`. Se ejecutaron
6 fits de desarrollo y se sellaron 2.532 predicciones OOS (633 decisiones × 4 scorers) en 56.159 B
(protocolo 3.917 B, predicciones 25.242 B, reporte 25.134 B, manifiesto 1.866 B). La ejecución tardó
4,01 s de pared (lectura 1,865 s, fits 0,151 s, evaluación 0,118 s) con RSS pico 228.745.216 B. No se
abrió el workspace ni se contactó ningún proveedor.

| Scorer | Brier pooled | Log-loss pooled |
| --- | --- | --- |
| `technical_logistic` | 0,24940344102885015 | 0,6919493484712885 |
| `funding_logistic` | 0,25094407714874845 | 0,6949937569660629 |
| `half_probability` | 0,25 | 0,6931471805599453 |
| `training_class_prior` | 0,24853491584603996 | 0,6902173542074427 |

Las tres comparaciones son negativas y ningún criterio se satisface: frente al control técnico el
delta de Brier es `-0,0015406361198983` (relativo `-0,006177284938583043`, límite inferior
`-0,004162300338326084`); frente a `p=0.5`, `-0,00094407714874845` (relativo
`-0,0037763085949938`, límite inferior `-0,011044765473245052`); y frente al prior,
`-0,00240916130270849` (relativo `-0,009693452103127814`, límite inferior
`-0,0070160962765873835`). El brazo de funding tampoco gana en ningún fold individual: sus Brier son
0,2541754343637745, 0,24079893111137493 y 0,25836114874796506 frente a 0,25166587200749874,
0,2403083784710963 y 0,25668421230293714 del control técnico.

La contabilidad de folds reproduce el protocolo: 1.075/1.293/1.500 filas de train y 207/218/208
decisiones de evaluación, un segmento diario contiguo por fold. Se excluyeron 7 filas de train por
funding incompleto en cada fold, 11 por embargo en los folds 1 y 3, y 11 por purge previo en los
folds 2 y 3. Una segunda ejecución idéntica, con `fit_and_predict` prohibido, reutilizó el mismo run
sin fits, sin red y sin reescribir los cuatro archivos; el dataset y el bundle conservaron sus
hashes (`eaf3d5f8…` y `c8369212…`). El lector funcionó con las dependencias ML bloqueadas por
`sys.meta_path`.

### Limitaciones y siguiente decisión

- La disponibilidad reconstruida es un supuesto; el resultado no satisface el gate de PIT observado.
- Ambos brazos usan el train reducido por la misma máscara conjunta; las métricas de #395 no son un
  control válido para esta comparación. El delta correcto es el reportado aquí.
- Los labels se solapan cinco intervalos y el bootstrap de bloques sólo modela dependencia local.
- No se calculan PnL, costes ni rentabilidad porque no se define una estrategia ni ejecución.
- El holdout no se abrió y este bloque no lo autoriza. El siguiente PLAN valora el resultado: si es
  negativo, detiene esta hipótesis sin tuning y decide si existe otra justificación investigativa; no
  hay entrenamiento automático posterior ni promoción de predictor. Un resultado longitudinal no
  demuestra por sí mismo una ventaja operativa ni cambia V1-U4.

## Descubrimiento, detalle y contraste de estudios (#407)

`RESEARCH-EXPERIMENTS-DELIVERY` completa el recorrido local de investigación: descubrir los estudios
sellados de un activo, abrir su detalle verificado, contrastar condiciones lado a lado y descargar la
proyección consultada. Reutiliza el lector baseline y `read_funding_ml_artifact`; crea sólo dos
contratos de presentación aislados, `research-experiment-list-v1` y
`research-experiment-report-view-v1`, con una unión discriminada `family=baseline|funding`. No
recomputa evaluaciones, no entrena, no abre el workspace, no contacta proveedores y no convierte un
resultado retrospectivo en señal, recomendación o ranking.

### Contratos y composición

- `GET /api/v1/research/experiments?asset_id=<catalog-id>&family=all|baseline|funding&limit=<1..50>&cursor=<token>`
  enumera sólo hijos directos de las dos raíces, con tope de 256 entradas por raíz. `family` default
  `all`, `limit` default 20. La página lee únicamente `manifest.json` y `protocol.json` de sus
  candidatos (≤64 KiB cada uno), verifica codificación canónica, binding protocolo↔manifiesto y
  `asset_id` antes de hidratar nada; no abre `report.json`, `predictions.parquet`, datasets ni
  lineage y no publica métricas ni veredicto.
- `GET /api/v1/research/experiment-report?asset_id=<catalog-id>&family=baseline|funding&run_id=<uuid>`
  devuelve la vista discriminada. Baseline delega exactamente en `MlResearchReportService` y su
  proyección existente; funding verifica el run completo y copia protocolo, resultados y proveniencia
  sin recomputar la evaluación. Los `Decimal` viajan como texto y no se convierten a float.
- `--ml-results-root` conserva su contrato. Se añade `--ml-funding-results-root` (absoluto, default
  `~/.local/share/investment-analyst/research/ml-funding-validation`). Las raíces son del servidor y
  nunca llegan por HTTP; no se crean directorios al iniciar ni al consultar. El servicio nuevo se
  inyecta junto a la misma instancia baseline: no hay segunda caché ni segunda construcción de
  storage. `GET /api/v1/research/ml-report` y `?ml_run_id=<uuid>` siguen funcionando como baseline;
  `ml_family` sólo identifica la familia y nunca una ruta.

### Límites, errores y coste

- Raíz ausente es estado vacío explícito (`missing`), nunca cero. Raíz symlink o ilegible falla
  cerrada; no se sigue ningún symlink, no se acepta traversal y un nombre de directorio no canónico
  nunca se convierte en ruta. Un candidato incompleto o corrupto se declara como advertencia compacta
  y no se presenta como estudio verificado.
- El cursor es opaco y versionado: valida el último `(family, run_id)`, el contexto activo / familia /
  límite y la huella del inventario ordenado. Un inventario cambiado devuelve error explícito y exige
  reiniciar el listado, sin saltos ni duplicados silenciosos. Una página puede tener cero
  coincidencias y ofrecer cursor: no se anuncia fin mientras queden candidatos.
- Caps de funding: protocolo y manifiesto ≤64 KiB, reporte ≤1 MiB, predicciones ≤8 MiB, conjunto
  ≤16 MiB. La caché nueva conserva ≤4 entradas / 2 MiB de proyecciones —nunca datasets, modelos ni
  Parquet— y la baseline conserva su 8 / 2 MiB: presupuesto combinado ≤12 entradas / 4 MiB. Un cambio
  de contenido invalida aunque se preserven tamaño y `mtime`; las peticiones idénticas concurrentes se
  coalescen y una respuesta tardía se descarta.
- `400` para parámetros ausentes, duplicados, desconocidos, UUID no canónico, límite fuera de rango,
  familia inválida o activo fuera del catálogo; `404` para raíz o run inexistente; `409` para artefacto
  incompatible/corrupto, `asset_id` distinto, cursor stale, capacidad excedida o servicio no
  configurado. No hay fallback a otro run.

### Interfaz

El panel de investigación existente se amplía; no se crea un dashboard paralelo. La primera apertura
ocurre por acción explícita, con selector de familia, páginas navegables, selección de run y consulta
manual por UUID como alternativa. Al cambiar activo, familia, página o run se cancelan las peticiones
obsoletas, se limpia el detalle y el contraste incompatibles y se descartan respuestas tardías; la app
no consulta estudios al iniciar ni en polling.

El detalle muestra el resultado principal neutral según la decisión literal, distingue siempre la
evaluación retrospectiva de una señal utilizable y expone objetivo, ventana, tamaño OOS con sus cuatro
scorers, criterio, protocolo, folds, calibración, proveniencia y JSON en disclosure progresivo. El
contraste admite hasta dos estudios del mismo activo y es sólo descriptivo: no calcula delta,
significancia cruzada, ranking ni «mejor estudio». Declara explícitamente que baseline y funding no son
una comparación pareada porque el entrenamiento difiere por la máscara de funding, y que las métricas
de #395 no son un control válido para #405. «Descargar reporte JSON» exporta exactamente la proyección
verificada como `family-run_id.json` desde el navegador, con límite de 1 MiB y sin filesystem del
servidor, red adicional ni recomputación.

### Comandos y verificación

```bash
PYTHONPATH=src .venv/bin/python scripts/serve_investment_analyst.py \
  --ml-results-root /home/marjuraru/.local/share/investment-analyst/research/ml-baseline-validation \
  --ml-funding-results-root /home/marjuraru/.local/share/investment-analyst/research/ml-funding-validation
```

```text
GET http://127.0.0.1:8765/api/v1/research/experiments?asset_id=crypto:btc-usd&family=all&limit=20
GET http://127.0.0.1:8765/api/v1/research/experiment-report?asset_id=crypto:btc-usd&family=funding&run_id=8dbc6330-1ff2-51bc-b701-a65611afa495
```

La prueba focalizada es
`tests/unit/research/test_experiment_report_service.py`,
`tests/integration/frontend/test_research_experiments.py`,
`tests/unit/frontend/test_ml_research_report_behavior.py`,
`tests/integration/frontend/test_ml_research_report.py`,
`tests/unit/research/test_ml_report_service.py`,
`tests/unit/frontend/test_design_system.py`,
`tests/integration/application/test_serve_storage_observability_composition.py` y
`tests/unit/test_delivery_route_contract.py`. El smoke premerge usa el servicio local con scheduler
deshabilitado, raíces reales en sólo lectura y Chromium con Node/CDP.

### Resultado real (premerge, 2026-11-03)

Smoke desde el candidato exact-SHA sobre las raíces reales, con workspace, estado, perfil y puerto
scratch, scheduler deshabilitado y sin proveedores:

- Servicio: el listado `family=all` con `limit=2` recorrió los 5 candidatos en 3 páginas (2+2+1), sin
  duplicados ni saltos. Los 3 runs verificables son `276e7e72…` y `fc4ea570…` (baseline) y
  `8dbc6330…` (funding). Los candidatos de funding `151ce74c…` y `411a4c42…` quedaron como
  `incompatible_artifact`: se sellaron con un campo `criteria.tie_tolerance` que el lector vigente
  rechaza —`read_funding_ml_artifact` también los rechaza—, así que no se presentan como estudios y no
  se reescriben.
- Igualdad con los lectores originales: el detalle baseline coincide exactamente con la proyección
  JSON de `MlResearchReportService` (`manifest ecacc2c8…`, `no_demonstrated_edge`, holdout
  `not_evaluated`, 2.532 filas OOS); el detalle funding coincide con `read_funding_ml_artifact`
  (`manifest 7540484f…`, reporte `8154507e…`, `no_demonstrated_increment`, Brier 0,249403… /
  0,250944… / 0,25 / 0,248535…) y el holdout sigue en 0 accesos y 0 fits. Ningún payload incluye
  rutas absolutas.
- Navegador real (`Chromium 153.0.8010.12`, Node 24.20.0 y CDP, perfil y descargas scratch):
  descubrió el listado, abrió ambos runs de referencia, construyó el contraste de 19 filas con la nota
  de no-comparación pareada y descargó `funding-8dbc6330-1ff2-51bc-b701-a65611afa495.json` (28.890 B)
  cuyo JSON es idéntico a la proyección recibida. Con una petición de listado retenida y un cambio de
  activo a `equity:us:aapl`, la app canceló la petición y no renderizó la respuesta tardía: lista
  vacía, detalle y contraste ocultos.
- El control «Siguiente página» se ejerció contra una raíz scratch de 21 entradas: la página 1 examinó
  20 candidatos, la página 2 examinó 3 y detuvo el cursor.
- Coste medido: listado en frío 2,1 ms y en caliente 1,4 ms con 10 lecturas de cabecera y 27.111 B
  (sólo manifiesto y protocolo de los candidatos examinados); detalle funding en frío 16,4 ms y en
  caliente 0,24 ms, con una única verificación completa y caché de 1 entrada / 28.890 B; el detalle no
  vuelve a enumerar raíces. RSS pico del proceso de medición 98,0 MB. Un corpus ajeno de 21 y 210
  entradas mantuvo el examen en 20 candidatos, 9.666 B y tiempos planos: sólo crece la enumeración
  acotada de nombres.

## Presupuesto y retención

La raíz real `~/.local/share/investment-analyst/research/predictive-foundation` tiene cap de 16 MiB;
cada artefacto, de 8 MiB, más un manifest compacto. El export real quedó en 360 843 B, sin copias de
DB/venv, sin entrenamiento y sin ETL daemon. El artefacto se conserva hasta un consumidor o una
revisión explícita futura; no hay borrado automático ni un artefacto por ensayo. Los temporales
propios incompletos se retiran tras su último consumidor. No se toca ni se copia el workspace
operativo.

La raíz reconstruida `~/.local/share/investment-analyst/research/predictive-historical-data` tiene
cap total de 32 MiB: archivo fuente ≤4 MiB, dataset+manifest ≤16 MiB, recibo ≤1 MiB. Se conserva un
único archivo y un único dataset sellados hasta una decisión explícita posterior a su consumidor ML;
no se crea un artefacto por ensayo ni se borra automáticamente. Los temporales propios incompletos se
retiran. La receta de reconciliación sigue siendo la única de `docs/development_protocol.md`.

La raíz `~/.local/share/investment-analyst/research/ml-funding-validation` conserva el run final de
56.159 B: protocolo, predicciones, reporte y manifiesto, con cap de 16 MiB por raíz y 8 MiB por
archivo. Los runs de candidatos anteriores permanecen sin reescribir y la raíz completa mide
168.622 B. No hay pesos, pickles ni copias de inputs. El artefacto se conserva hasta una decisión
explícita de descarte y ausencia de consumidor; los resultados negativos no se borran.

## Preparación PIT explicable (2026-11-04)

`PIT-READINESS-DELIVERY`/#418 (`ADVANCES`) publica `research-pit-readiness-view-v1`, una proyección
tipada, inmutable y `extra=forbid` sobre el mismo `PredictiveDatasetSummary` que ya emite el
constructor preregistrado. La vista añade sólo agregados de admisión: por cada columna de feature,
una partición exacta de las decisiones solicitadas entre `available`, `missing`,
`not_yet_available`, `incompatible` y `ambiguous`, más la incidencia de cada código de razón.
`reason_scope=column_reason_occurrences` declara explícitamente que una decisión puede emitir varias
razones para la misma columna y que, por tanto, las incidencias pueden superar el número de
decisiones y no son una partición. La vista no publica filas, precios, valores de feature por fila,
retornos ni clases de validation/holdout; los conteos `positive`/`negative` fuera de train siguen
siendo `null` porque el summary los limita a train.

La consulta es explícita (`asset_id`, `start_date`, `end_date`, `known_at`) y se sirve por
`GET /api/v1/research/readiness` y por el panel de preparación del tablero `tecnico`. El endpoint
valida duplicados, parámetros desconocidos, fechas y el límite de 366 decisiones, y traduce el corte a
`dataset_known_at`; un activo fuera del perfil cripto spot USD responde 422 `asset_incompatible` con
su motivo, nunca una respuesta vacía o un 500. La lectura abre un único `ApplicationRuntime` en modo
READ_ONLY y reutiliza la firma compuesta y la caché acotada existentes con las consultas
`MARKET_BARS` (`close`) y `METRICS` (RSI y MACD) sobre los rangos exactos que lee el constructor,
incluidos D-2..D+6 y los períodos de métricas, filtrados por `available_at <= known_at`. La firma
incluye endpoint, parámetros, versiones del experimento, workspace/catálogo y las dependencias de
observaciones y métricas.

Evidencia viva contra el workspace `active-v2-20261021`, corte `2026-11-04T19:51:37Z` y rango
inclusivo 2026-08-03..2026-11-03: BTC produce 93 decisiones, 1 feature disponible y 92
`not_yet_available`, 88 labels disponibles y 5 pendientes, 22 purgadas, 1 elegible antes del split
(train 0, validation 0, holdout 1), `insufficient_pit_sample` y dataset `dbb83880…`; SOL reproduce
las mismas cifras y dataset `b05e9853…`. El summary coincide exactamente con el `--json` del CLI
vigente, la respuesta mide 4.361 B (≤256 KiB) y el workspace conserva tamaño, `mtime` y ausencia de
WAL de `index.duckdb` y el SHA-256 de `manifest.json` antes y después. No se adquiere ningún
proveedor, no se abre ningún artefacto ML y no se ejecuta entrenamiento.

El contrato admite hasta 366 fechas por consulta y el PIT observado crece al ritmo del calendario:
esperar no garantiza alcanzar los pisos de 300 elegibles, 150 de train, 40 de validation y 40 de
holdout. `PREDICTIVE-RESEARCH` permanece `NEXT` y V1-U4 sigue `UNKNOWN`; la siguiente decisión debe
justificar con evidencia si otra hipótesis preregistrada o datos externos merecen un experimento,
conservando los costes y el holdout cerrado. Este bloque no abre un estudio nuevo ni promueve un
predictor.

## Investigación de variación futura de BTC (2026-11-05)

`VOLATILITY-BASELINE-RESEARCH`/#420 (`ADVANCES`) abre una hipótesis distinta de dirección: un proxy
de variación futura de BTC. No continúa el tuning direccional detenido por #395/#405, no reabre sus
datasets de clasificación ni usa el holdout. Corsi (2009,
https://doi.org/10.1093/jjfinec/nbp001) motiva contrastar persistencia en escalas distintas; no se
atribuye al artículo un edge BTC ni se denomina HAR-RV canónico. El resultado es retrospectivo y
condicionado.

### Entrada, contrato y protocolo

- Archivo exclusivo y read-only: `67ec4e68-33f9-53ef-82bb-032258b57786`, manifest 3.237 B
  `e1669871…`, `candles.jsonl` 712.767 B `6a6ecb12…`, semántico `a5e3b4dc…`, 2.557 barras diarias
  2019-01-01..2025-12-31, `retrieved_at=2026-11-01T02:37:32.364526Z`. El comando verifica el archivo
  completo por integridad; los bytes del período reservado no se usan para features ni targets.
- Contrato aislado `crypto-historical-log-variation-5d-v1`; esquemas `research-volatility-protocol-v1`,
  `research-volatility-report-v1` y `research-volatility-manifest-v1`. No se extienden los esquemas de
  clasificación, funding, baseline o readiness.
- Decisiones planificadas 2020-01-01..2025-12-25 (2.186). Se construyen sólo las 1.748 anteriores a
  `d=2024-10-14`; las 438 restantes no se construyen. Folds `[1093,1311)`, `[1311,1529)` y
  `[1529,1748)`; train expansivo `[0,inicio)`; embargo de labels `< fin_ventana − 5 d`; pisos 500
  train, 100 validation y segmentos contiguos ≥20.
- Features `X_k=ln(1e-12 + (1/k)Σ r_t²)` para `k∈{1,7,30}`, con `r_t=ln(C_t/C_(t−1))`; referencia
  de persistencia `X_5` y `X_30`. Target `Y_D=ln(1e-12 + (1/5)Σ_(t=D+1..D+5) r_t²)`, con cierres
  D..D+5. Decimal34 `ROUND_HALF_EVEN`, sin floats, sin forward-fill, sin interpolación y sin saltar
  días. Decisión 12:00 UTC; disponibilidad asumida del feature `max(bar_end+1h) ≤ D 01:00`; madurez
  del label `D+6 01:00`; `evidence_available_at=max(madurez,retrieved_at)`; corte del estudio
  `retrieved_at`.
- Único candidato: `StandardScaler` ajustado sólo en train + `Ridge(alpha=1.0, fit_intercept=True,
  solver='svd')`, float64 en el adaptador, un hilo BLAS/OpenMP, exactamente 3 fits (uno por fold). Sin
  CV interna, tuning, calibración, winsorización, selección de features, semillas ni otros modelos.
  Referencias: `persistence_5d`, `persistence_30d` y media de `Ytrain`. La incertidumbre es un
  bootstrap pareado de bloques móviles de 20, 2.000 réplicas, PCG64(1729), los mismos draws para
  candidato y referencias, quantile unilateral 0,05/3 y límites inferiores.
- Criterio: `development_evidence` sólo si la mejora pooled de MSE es ≥5 % frente a las tres
  referencias, los tres límites inferiores son >0 y la mejora puntual es positiva frente a cada
  referencia en cada fold. En otro caso, `no_demonstrated_increment`. Un fallo de pisos o de contrato
  es `insufficient_sample`/error, nunca un negativo estadístico. El holdout queda `not_evaluated`, 0
  fits y 0 evaluaciones de targets reservados; no se promueve nada.

### Resultado real (2026-11-05)

Run `44b61556-c34e-5778-9f7f-af081a6cc195`; protocolo `6c4ca709…`, reporte `7f2d30b5…`,
predicciones `39091b1f…` y manifiesto `29630228…`. El artefacto ocupa 933.594 B (protocolo 3.243,
predicciones 919.486, reporte 9.276 y manifiesto 1.589) y la raíz retiene un único run. La ejecución
tardó 3,50 s con RSS pico 214.962.176 B y un solo hilo; el archivo fuente conservó sus hashes. Se
construyeron 1.748 decisiones, 1.748 con features y 1.737 con target; 622 filas OOS, 2.488
predicciones y 3 fits. No se abrió el workspace, no hubo red y no se escribió fuera de la raíz.

| Modelo pooled | MSE | MAE |
| --- | --- | --- |
| `ridge` | 1,3076747311778731 | 0,9124603170735622 |
| `persistence_5d` | 2,0585856516150556 | 1,150328648997028 |
| `persistence_30d` | 1,429063481864044 | 0,9598422263402294 |
| `training_mean` | 1,6739505264739558 | 1,004404422200344 |

Frente a `persistence_5d`, la mejora relativa pooled es 36,48 % (delta 0,7509109204371825; límite
inferior 0,4189972919873506). Frente a `persistence_30d`, 8,49 % (delta 0,1213887506861709; límite
inferior 0,029798190116840068). Frente a `training_mean`, 21,88 % (delta 0,3662757952960827; límite
inferior 0,09439052714273738). Los tres límites son positivos y las tres mejoras pooled superan el
5 %. Sin embargo, el fold 2 no mejora a `persistence_30d`: MSE 1,6488321182713472 frente a
1,6297878184531283 (delta −0,0190442998182189; relativo −1,17 %). El criterio preregistrado exige
mejora positiva en cada fold, de modo que la decisión final es `no_demonstrated_increment`. No se
ajusta el criterio, no se elimina el fold, no se prueban otras ventanas, otro activo ni otra semilla.

### Lectura, límites y siguiente decisión

- El pooled sí muestra una mejora amplia y con límites positivos, pero la estabilidad por fold frente
  a la persistencia de 30 días no se cumple. La decisión conservadora es negativa; no se presenta como
  un predictor útil ni como una señal.
- Sólo hay cierres diarios: el target es un log de media de retornos logarítmicos al cuadrado, no una
  varianza realizada intradía ni una volatilidad anualizada. No se invierte `exp(Y)` ni se interpreta
  como esperanza condicional de varianza o PnL.
- La disponibilidad sigue siendo `historical_reconstruction_assumed`, `strict_pit_verified=false`; no
  satisface el gate PIT observado. El período reservado no se evaluó y no se autoriza abrirlo.
- La familia no se integra en el listado UI de baseline/funding ni en readiness; una superficie de
  lectura exige un contrato explícito posterior. El siguiente PLAN decide si esta hipótesis se detiene
  o si una validación confirmatoria separada merece un bloque propio; no se entrena por inercia.

### Comandos exactos

```bash
PYTHONPATH=src .venv/bin/python scripts/evaluate_volatility_baseline.py \
  --archive-dir /home/marjuraru/.local/share/investment-analyst/research/predictive-historical-data/67ec4e68-33f9-53ef-82bb-032258b57786 \
  --output-dir /home/marjuraru/.local/share/investment-analyst/research/volatility-baseline-research \
  --json
```

No existen flags de parámetros, modelo, semilla ni holdout. Una segunda ejecución idéntica reutiliza
el run sin fits ni reescritura. `--help` y el lector del artefacto no requieren `sklearn`, `numpy`,
`scipy` ni `threadpoolctl`; el destino debe ser absoluto, fuera del repositorio, workspace, runtime y
ops, sin symlinks ni solapamiento con la entrada u otro experimento.

## Limitaciones y siguiente decisión

- Sólo existe 1 muestra con features PIT disponibles; 0 muestras elegibles. No se puede entrenar,
  calibrar ni prometer una predicción con evidencia PIT observada.
- El dataset reconstruido amplía la muestra disponible, pero su `assumed_available_at` es una
  hipótesis: un precio histórico revisado hoy pudo tener otra primera publicación entonces. Es
  investigación retrospectiva condicional y nunca satisface por sustitución el gate de PIT observado.
- El label es un retorno bruto observado, no una rentabilidad ejecutable ni una señal de compra.
- La partición 60/20/20 es la base del holdout; no sustituye purged walk-forward dentro de
  train/validation del futuro P2–P3.
- Un software completo no implica datos suficientes, modelo evaluado ni predictor promovido. V1-U4
  permanece `UNKNOWN`: un label no es un modelo.
- `ML-BASELINE-VALIDATION` ejecutó la evaluación sobre la muestra reconstruida con muestra suficiente
  y protocolo preregistrado: no demostró ventaja y no abrió el holdout. El resultado no satisface el
  gate PIT observado ni autoriza promoción; el siguiente PLAN decide una hipótesis distinta o el
  cierre del experimento, sin tuning ni nuevos activos.

## Contrato del consumidor futuro

Un consumidor externo debe verificar el manifest y el hash semántico antes de leer; respetar el
orden de las tres features, `sample_key` y `decision_at`; mantener las features separadas de
labels/metadatos futuros; comprobar unidades y ausencias; y no usar muestras fuera de su split ni
del corte declarado.

Un adaptador ML debe declarar además qué modo consume. Para el modo reconstruido debe admitir
explícitamente `research-historical-dataset-v1` y
`availability_basis=historical_reconstruction_assumed`, rechazar `strict_pit_verified=true` y no
presentar la muestra como PIT observada. La conversión de Decimal-string al dtype del modelo ocurre
sólo en ese futuro adaptador explícito, nunca en el núcleo. No se crea ahora una clase abstracta de
entrenador, un fake predictor, un `ModelExplanationPacket` vacío ni infraestructura de plugins.
