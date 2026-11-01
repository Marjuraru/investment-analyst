# Fundación predictiva — experimento diario cripto y dataset PIT

> Autoridad técnica y operativa única del artefacto abierto por `PREDICTIVE-FOUNDATION`/#391
> (`PREDICTIVE-RESEARCH`, `ADVANCES`, sin cierre). Estrategia y ruta viven en
> [`product_roadmap.md`](product_roadmap.md), [`basic_functional_release_plan.md`](basic_functional_release_plan.md)
> y [`research_information_strategy.md`](research_information_strategy.md). Este documento no
> autoriza adquisición, entrenamiento, recomendación ni ejecución.

## Estado y alcance

`PREDICTIVE-FOUNDATION` fija una primera pregunta predictiva, construye un dataset de investigación
reproducible con separación temporal entre features y label y publica su elegibilidad real. No
entrena un modelo, no produce una predicción, no genera una señal ni una recomendación y no toca el
workspace operativo en escritura.

Perfil soportado: cripto spot USD, barras diarias UTC y fuente inequívoca del catálogo. La primera
fuente es Coinbase diario para `crypto:btc-usd`; `crypto:sol-usd` es control de reutilización. La
lógica no tiene ramas por BTC o SOL. Equity/ETF, intradía, derivados, cross-sectional y mezcla de
fuentes o perfiles quedan fuera con error explícito. El workspace se abre siempre en
`READ_ONLY`; el export escribe únicamente en una raíz de investigación explícita, fuera del
workspace.

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

## Evaluación futura (no ejecutada aquí)

La métrica primaria predeclarada para un futuro modelo es Brier fuera de muestra frente a `p=0.5` y
al prior de clase estimado sólo en `train` de cada fold; log-loss, calibración y detalle por
fold/fecha son secundarios. La regla inicial de avance futura es una mejora relativa de Brier
`>= 5 %` frente a ambos baselines en validación temporal agregada, superioridad con intervalo
pareado que respete la dependencia de labels y confirmación en holdout intacto. El protocolo
numérico de incertidumbre y el embargo de folds se definen antes de entrenar en el siguiente bloque.
No se optimiza el criterio sobre holdout ni se promete esa mejora. Shadow, drift, costes reales y
rollback son puertas separadas. Un LLM posterior recibe un paquete ya calculado y nunca participa en
label ni feature. Ningún paquete ML se instala en este bloque.

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

## Presupuesto y retención

La raíz real `~/.local/share/investment-analyst/research/predictive-foundation` tiene cap de 16 MiB;
cada artefacto, de 8 MiB, más un manifest compacto. El export real quedó en 360 843 B, sin copias de
DB/venv, sin entrenamiento y sin ETL daemon. El artefacto se conserva hasta un consumidor o una
revisión explícita futura; no hay borrado automático ni un artefacto por ensayo. Los temporales
propios incompletos se retiran tras su último consumidor. No se toca ni se copia el workspace
operativo.

## Limitaciones y siguiente decisión

- Sólo existe 1 muestra con features PIT disponibles; 0 muestras elegibles. No se puede entrenar,
  calibrar ni prometer una predicción.
- El label es un retorno bruto observado, no una rentabilidad ejecutable ni una señal de compra.
- La partición 60/20/20 es la base del holdout; no sustituye purged walk-forward dentro de
  train/validation del futuro P2–P3.
- Un software completo no implica datos suficientes, modelo evaluado ni predictor promovido. V1-U4
  permanece `UNKNOWN`: un label no es un modelo.
- `ML-BASELINE-VALIDATION` es el siguiente esperado sólo si existe muestra elegible suficiente. Si
  no, el siguiente PLAN resuelve la carencia temporal demostrada usando primero evidencia histórica
  recuperable o captura prospectiva existente, sin retrofechar ni entrenar con datos filtrados desde
  el futuro.

## Contrato del consumidor futuro

Un consumidor externo debe verificar el manifest y el hash semántico antes de leer; respetar el
orden de las tres features, `sample_key` y `decision_at`; mantener las features separadas de
labels/metadatos futuros; comprobar unidades y ausencias; y no usar muestras fuera de su split ni
del corte declarado. La conversión de Decimal-string al dtype del modelo ocurre sólo en ese futuro
adaptador explícito, nunca en el núcleo. No se crea ahora una clase abstracta de entrenador, un fake
predictor, un `ModelExplanationPacket` vacío ni infraestructura de plugins.
