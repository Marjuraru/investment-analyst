# Fundación predictiva — experimento diario cripto, dataset PIT y reconstrucción histórica

> Autoridad técnica y operativa única de los artefactos abiertos por `PREDICTIVE-FOUNDATION`/#391 y
> `PREDICTIVE-HISTORICAL-DATA`/#393 (`PREDICTIVE-RESEARCH`, `ADVANCES`, sin cierre). Estrategia y
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
