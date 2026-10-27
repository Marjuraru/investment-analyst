# V2 operational readiness

Documento vigente de readiness operativa para retomar `FUNDAMENTALS-COVERAGE`. No declara cobertura
fundamental nueva, no cierra esa ruta y no sustituye la autoridad de
[`product_roadmap.md`](product_roadmap.md) y [`basic_functional_release_plan.md`](basic_functional_release_plan.md).

## Autoridad y corte

| Campo | Valor |
| --- | --- |
| Work Block | `FUNDAMENTALS-COVERAGE-CYCLE` (#363) |
| Base declarada | `origin/main@a9dd555b9368168d145e357f380e9d0c47acfd41` |
| Runtime desplegado | HEAD de `codex/fundamentals-coverage-cycle`; código de lectura medido `351181bf37c150f656c80f1f76a9fa82d8116b86` (los commits posteriores sólo cambian documentación) |
| Workspace activo | `workspaces/active-v2-20261021` |
| UUID v2 | `70c64668-1738-4dfe-ba1f-7e72026bb7fe` |
| Rollback de referencia | `351181bf37c150f656c80f1f76a9fa82d8116b86` (inmediato); `d2d2c36149db717bb94a810b15503ceb17786061` (retenido) |
| Medición de adquisición | `351181b` (árbol de runtime equivalente); mercado 20–26 oct, SEC no repetido por limitación previa |
| `route_effect` | `ADVANCES` sobre `FUNDAMENTALS-COVERAGE`, que permanece el único `NEXT` |

Los valores de runtime, workspace y release se leen de `runtime/deployment_state.json` y
`runtime/deployment_workspace_binding.json`. Un SHA nuevo invalida las mediciones de este documento
hasta repetir la matriz correspondiente. La medición de adquisición se etiqueta con el árbol exacto
que la produjo; si el candidato final difiere sólo en documentación, se indica literalmente.

## Objetivos finitos C1–C5

Denominador estable: cinco objetivos `C1`–`C5`. Un baseline no medido es `UNKNOWN`, nunca cero. Las
metas de latencia son criterios de aceptación, no afirmaciones actuales. No se hereda `MET` del
bloque anterior.

| ID | Criterio final | Medición, límite y evidencia |
| --- | --- | --- |
| `C1` | Causas A1 trazadas sin bug de lectura/presentación | Matriz Activo→fundamentales/valoración para AAPL, AMD, TSM y BVN NYSE con `known_at` explícito: frecuencia soportada, capacidad, evidencia, periodos, métricas, `skipped_counts`, estado y `reason_codes`. Evidencia: `scripts/smoke_v2_readiness.py --known-at …` y pruebas focalizadas. |
| `C2` | Ciclo semanal finito sin duplicación evitable | Matriz de casos de la sección siguiente sobre workspace scratch, relojes distintos y transportes deterministas. `tests/integration/application/test_operational_weekly_cycle.py` debe quedar verde y cubrir cada fila. |
| `C3` | Coste estructural A3 | Precio PIT compartido entre cobertura de mercado y valoración dentro de la misma consulta de cobertura; cuatro particiones read-only reutilizadas; resumen de mercado por proyección ligera y firma compuesta por ventana. Medición in-process 2026-10-27: firma 208.422→89.980 dependencias, 2,96→1,45 s; loader 9,73→6,5 s; total 15,83→9,4 s; payload idéntico `47d65eb2…`. Evidencia: `test_read_path_efficiency_integration.py`, `test_read_dependency_signature.py`, `test_service.py` y probe read-only. |
| `C4` | UI ágil real | Shell mediana ≤2 s y máximo ≤5 s: 173/204 ms. Matriz 37 aplicación/caché fría (5 muestras in-process, sin arranque de proceso): 9.473/9.809 s → `MET`. Matriz 37 servidor/HTTP fría (5 muestras, incluye arranque de proceso): 10.386/11.644 s; navegador: 10.873/12.274 s → mediana 0.386–0.873 s sobre la meta de aplicación, máximo ≤20 s. Matriz caliente: HTTP 1.899/2.154 s; navegador 2.513/3.791 s → `MET`. Reapertura visible 112/119 ms; revalidación aparte 2.836/3.422 s. Cero timeout/5xx. Evidencia: `smoke-summary.json`, `browser-smoke.json` y `/api/v1/read-performance`. |
| `C5` | Presupuesto y entrega | Presupuesto físico declarado vs real, retención, CI `Python 3.12 quality` exact-SHA, AUDIT fresco y FINALIZE AUTO. Evidencia: recibos de `ops/fundamentals-coverage-cycle/`, `runtime/deployment_state.json` y GitHub vivo. |

## Ciclo semanal: casos, evidencia y límites

La receta operativa es una pasada diaria del scheduler local a las 07:00 `America/Lima`. Cada job
calcula su `market_end` como el día local anterior y conserva el rango público inclusivo convertido
a un intervalo UTC semiabierto. El scheduler ejecuta como máximo un writer por tick y un éxito diario
no se relanza.

| Caso | Qué debe ocurrir | Evidencia determinista | Límite explícito |
| --- | --- | --- | --- |
| Sesión con barras nuevas | Se descarga sólo el borde pendiente; se persisten barras, métricas y diagnóstico. | Ciclo viernes→martes con barras 13–16 y 19 oct. | No se descarga historia ya cubierta. |
| Sábado/domingo | Respuesta oficial vacía completa; se persiste receipt; no se inventan barras, `market_as_of` ni métricas. | Dos receipts vacíos consecutivos 17 y 18 oct. | El vacío no acredita cobertura hasta completar todas las páginas. |
| Feriado | Igual que un día sin sesión, aunque sea día de semana. | Receipt de 25 dic con una barra previa y ninguna observación ese día. | No se infiere calendario de bolsa. |
| Repetición del mismo intervalo vacío | `already_current`; cero descargas y cero receipts nuevos. | Repetición exacta del intervalo vacío inicial. | Un receipt no es una barra. |
| Siguiente sesión | Se descarga el nuevo borde y se actualiza el corte de mercado. | Barra del 28 dic tras el feriado. | Un hueco interno 26–27 no se rellena automáticamente. |
| BTC con novedades en domingo | El mercado cripto no se pausa por fin de semana. | Vela Coinbase del domingo 18 oct. | Coinbase es spot de una venue, no SIP consolidado. |
| SEC con filing/revisión nueva aunque mercado `already_current` | SEC se consulta igualmente y crea observaciones/métricas nuevas. | Revisión 10-K/A con `available_at` posterior y mercado sin llamadas nuevas. | Mercado y fundamentales son pipelines independientes. |
| Paginación fallida | No se crea receipt del intervalo; el progreso anterior permanece. | Primera página con token y segunda página 500. | Un fallo de página no se presenta como cobertura completa. |
| 429/timeout | Reintento acotado con backoff; estado honesto y presupuesto diario. | Categorías `RATE_LIMIT`/`TRANSPORT`, backoff 60 s y `max_attempts_per_day`. | Nunca reintentos infinitos. |
| Restart tras éxito | No se relanza el éxito del mismo día. | Segundo scheduler sobre el mismo estado. | El estado persistido es la autoridad. |
| Restart tras interrupción | El intento `RUNNING` se recupera como `INTERRUPTED` y se reintenta tras backoff. | Intento persistido antes del tick siguiente. | La recuperación no duplica trabajo completado. |
| Revisión tardía con mismo conteo | Nueva identidad de raw/observación; la revisión anterior permanece consultable al corte previo. | Mismo número de barras con cierre 10,40→10,55. | Nunca se sobrescribe historia. |
| Fechas inclusivas/UTC/zona | El rango público se convierte a `[inicio 00:00Z, fin+1 00:00Z)`; el scheduler usa `America/Lima`. | Query real `start=2026-10-01T00:00:00Z`, `end=2026-10-16T00:00:00Z`. | No se usa la hora local como corte financiero. |

## Ausencias de fundamentales y valoración

La lectura corrige dos confusiones demostradas y deja el resto como límites explícitos:

- Una frecuencia no declarada por el perfil SEC del emisor (`IFRS`: sólo `annual`) se publica como
  `capability=not_implemented`, `evidence=not_queried` y una limitación de perfil. No se presenta
  como `missing`, no se convierte silenciosamente a anual y no se etiqueta como trimestral.
- `share_basis_unavailable` deja de ocultar el panel de valoración: el emisor con fundamentales
  conserva la sección visible, la API devuelve el motivo exacto y la Mesa añade el motivo al nombre
  accesible de la celda sin crear un nuevo estado visual. `missing_inputs` e
  `invalid_denominator` siguen siendo motivos descriptivos por métrica, no ceros ni cobertura
  completa.
- AAPL no recibe reglas especiales. La selección de activos del scheduler no se amplía: TSM y BVN
  permanecen fuera del scheduler y la interfaz lo declara.

## Resultados medidos (2026-10-27)

Candidato `351181bf37c150f656c80f1f76a9fa82d8116b86`, workspace UUID
`70c64668-1738-4dfe-ba1f-7e72026bb7fe`, `known_at=2026-10-27T00:00:00Z`, ventanas mercado
`2025-10-26..2026-10-26` y fundamentales `2016-10-18..2026-10-26`. El detalle vive en
`ops/fundamentals-coverage-cycle/smoke-summary.json`, `browser-smoke.json`,
`in-process-cold.json`, `acquisition-summary.json` y `sec-limitation.json`.

### C1 — causas de ausencia

| Activo | Frecuencia | Cobertura fundamentales | Periodos/métricas | Valoración | Causa registrada |
| --- | --- | --- | --- | --- | --- |
| AAPL | quarterly | `supported/present` | 8/224 | `evaluated` (11/11) | inputs oficiales no representados en algunas familias |
| AMD | quarterly | `supported/present` | 8/160 | `partial` (6/11) | `missing_input`, `ebitda_unavailable` |
| TSM | annual | `supported/present` | 5/105 | `not_evaluable` (0/11) | `share_basis_unavailable`; quarterly `not_implemented/not_queried`; perfil IFRS sólo anual |
| BVN NYSE | annual | `supported/present` | 5/105 | `not_evaluable` (0/11) | `share_basis_unavailable`; quarterly `not_implemented/not_queried`; perfil IFRS sólo anual |

TSM y BVN permanecen fuera del scheduler; la interfaz lo declara. AAPL no recibe reglas especiales.
La matriz no convierte una ausencia en cero ni en cobertura completa.

### C2 — ciclo semanal

`tests/integration/application/test_operational_weekly_cycle.py` ejecuta diez pruebas: fin de
semana, feriado, repetición vacía, siguiente sesión, BTC dominical, SEC con revisión nueva,
paginación fallida, 429/timeout con backoff y presupuesto, restart tras éxito/interrupción, revisión
tardía con mismo conteo, fechas inclusivas/UTC/zona y un único writer por tick. Las diez pasan.

### C3 — coste estructural

Medición in-process de la matriz 37, mismos inputs: firma 208.422→89.980 dependencias y
2,96→1,45 s; loader 9,73→6,5 s; firma + loader + revalidación 15,83→9,4 s; payload idéntico
SHA-256 `47d65eb2…`. El precio PIT se comparte entre cobertura y valoración, la fan-out usa cuatro
particiones read-only y el resumen de mercado materializa sólo el grupo más reciente.

### C4 — latencia

Series de cinco muestras del candidato exacto:

| Escenario | P50 | Máximo | Límite |
| --- | --- | --- | --- |
| Shell navegador (servidor frío) | 173 ms | 204 ms | ≤2 s / ≤5 s |
| Matriz 37 aplicación/caché fría | 9.473 s | 9.809 s | ≤10 s / ≤20 s |
| Matriz 37 HTTP fría (incluye arranque) | 10.386 s | 11.644 s | mediana 0.386 s sobre meta; máx ≤20 s |
| Matriz 37 navegador fría (incluye arranque) | 10.873 s | 12.274 s | mediana 0.873 s sobre meta; máx ≤20 s |
| Matriz 37 HTTP caliente | 1.899 s | 2.154 s | ≤10 s / ≤20 s |
| Matriz 37 navegador caliente | 2.513 s | 3.791 s | ≤10 s / ≤20 s |
| Reapertura visible | 112 ms | 119 ms | ≤2 s / ≤5 s |
| Revalidación aparte | 2.836 s | 3.422 s | medida separada |
| Errores/5xx | 0 | 0 | cero |

La serie fría de aplicación/caché es la que define C4: se ejecuta en un proceso read-only sin
scheduler, con la caché de lectura vacía y sin el arranque del proceso de servicio, tal como declara
A4. La serie HTTP/navegador fría incluye el arranque y las importaciones del proceso; su mediana
queda 0.386–0.873 s por encima de la meta de aplicación, aunque su máximo permanece por debajo de
20 s. Se registra como coste de transporte/arranque, no como una mejora del camino analítico. Si el
criterio se interpreta estrictamente sobre el servidor HTTP, C4 no alcanza la mediana de 10 s y el
siguiente paso sería una proyección de dependencias más precisa o reducir el arranque del proceso.

### C5 — entrega y límites

CI `Python 3.12 quality` verde para `351181b`; el candidato está desplegado en el UUID actual y el
rollback inmediato `351181b` se conserva. La adquisición real de mercado fue acotada: AAPL
`already_current` (la barra 26-oct ya se había adquirido), AMD 1 barra/26 métricas, TSM y BVN 4
barras/95 métricas cada uno, BTC 1 vela/20 métricas; el replay de los mismos cortes devolvió
`already_current` en los cinco activos y reutilizó 111/106/100/100/140 métricas. DB +34.078.720 B y
blobs raw +~11 KB; sin backup ni restore.

**Limitación material SEC:** la comprobación de reconstrucción de una observación AAPL falla porque
el raw companyfacts `ddf69926…` fue reescrito con `retrieved_at=2026-10-21T20:49:52Z` mientras la
observación `aef8e8ee…` conserva `2026-08-01T04:06:18Z`. El job programado de AAPL ya falló el
2026-10-25 y 2026-10-26 sobre la base `d2d2c36`, antes de este candidato. No se repitió la descarga
SEC ni se reparó el workspace en este bloque R2; las lecturas SEC existentes siguen respondiendo.
La reparación requiere un Work Block separado.

## Presupuesto físico y retención

| Campo | Declaración | Real |
| --- | --- | --- |
| `rutas_proposito` | Rama aislada para código/tests/docs; scratch sintético temporal; `ops/fundamentals-coverage-cycle` para recibos; release nuevo por gestor existente; workspace activo sólo ingesta reciente acotada. | Rama `codex/fundamentals-coverage-cycle`; recibos 464 KB; release `351181b`; sólo borde 20–26 oct en el workspace. |
| `bytes_nuevos` | Diff/docs/recibos ≤10 MiB; release fuente ≤32 MiB; ingesta reciente UNKNOWN hasta dry-plan, techo 512 MiB autorizado para el smoke. | Release fuente 19.853.097 B (≈18,9 MiB); recibos 464 KB; diff/docs <10 MiB. Release total 108.773.779 B incluye `.venv` de 81 MiB (entorno), dentro del pico. |
| `bytes_modificados` | Código acotado por diff; DB/WAL medido antes/después; GET dominio 0. | DB +34.078.720 B; blobs raw +≈11 KB; sin WAL; GET dominio 0. |
| `temporales` | Scratch sintético ≤128 MiB y perfiles/logs ≤16 MiB; retirar sólo temporales propios. | Scratch <1 MiB en `/tmp`; retirado el recibo detallado de 380 MB; sin copias persistentes. |
| `pico_adicional` | Techo 768 MiB incluyendo release, scratch, ingesta y WAL. | ≈144 MB (release 108,8 + DB 34,1 + recibos/scratch <1) <768 MiB. |
| `reutilizados` | Entorno Python/browser, workspace v2, rollback vigente, cadena Drive y recibos de 48/361. | Sin copias nuevas; Playwright/Chromium y workspace existentes. |
| `exclusivos_retirables` | Sólo scratch propio del bloque, medido. | Sólo scratch propio; nada del workspace, releases ni backups. |
| `libres_host_wsl` | Host ≥5.000.000.000 B y WSL ≥5.000.000.000 B antes de escritura material. | 942.293.852.160 B libres al cierre. |
| `reserva` | ≥5.000.000.000 B libres en cada FS más pico restante admitido. | Preservada. |
| `retencion` | Código/docs en Git; recibos compactos hasta cierre/auditoría; release actual y rollback según gestor. | `351181b` actual, `32898b5` previo y `d2d2c36` de referencia retenidos; datos append-only; sin backups nuevos. |

No se agregó ningún backup, copia de DB ni restore. La ingesta real se limitó al borde pendiente de
hasta siete días completados: 34,1 MB lógicos, por debajo del umbral de aviso de 100 MB y del techo
de 512 MB. El dry-plan previo acotó AAPL/AMD/BTC a 26-oct y TSM/BVN a 21–26 oct. Cualquier operación
prevista ≥100 MB se anuncia antes.

## Limitación SEC y siguiente acción

La adquisición SEC real no se repitió: la comprobación de reconstrucción falla para la observación
`aef8e8ee…` porque el raw companyfacts `ddf69926…` fue reescrito con `retrieved_at=2026-10-21T20:49:52Z`
mientras la observación conserva `2026-08-01T04:06:18Z`. El job programado de AAPL ya falló el
2026-10-25 y 2026-10-26 sobre la base `d2d2c36`; no es un defecto del candidato. Las lecturas SEC y la
matriz de causas responden con la evidencia local existente. La reparación de esa inconsistencia
requiere un Work Block separado; no se reescribió ni se borró ningún dato en este bloque.

## Inventario documental aplicable

| Documento | Clasificación | Acción |
| --- | --- | --- |
| `AGENTS.md`, `docs/development_protocol.md` | `KEEP_CANONICAL` | Sin cambios |
| `docs/product_roadmap.md`, `docs/basic_functional_release_plan.md` | `UPDATE` | Registrar `FUNDAMENTALS-COVERAGE-CYCLE` como `ADVANCES`; la fila sigue `NEXT` |
| `docs/read_path_efficiency.md` | `UPDATE` | Documentar el precio PIT compartido y el coste estructural |
| `docs/local_interface.md` | `UPDATE` | Documentar motivos de ausencia en Mesa, panel de valoración visible y limpieza de ratios |
| `docs/universe_coverage.md` | `UPDATE` | Documentar frecuencia por emisor y `not_implemented`/`not_queried` |
| `docs/operational_runner.md` | `UPDATE` | Documentar el ciclo semanal, fines de semana, feriados y reintentos |
| `docs/sec_issuer_refresh.md` | `UPDATE` | Enlazar la clasificación de frecuencia no implementada |
| `docs/data_chassis*.md`, `docs/historical_recovery_runbook.md` | `HISTORICAL_REFERENCE` | Conservar historia fechada |
| `docs/planning_context/*` | `HISTORICAL_REFERENCE` | Conservar decisiones fechadas |

## Historial 2026-10-26: cierre de `FUNDAMENTALS-COVERAGE-READINESS`

Evidencia fechada del bloque anterior, conservada sin reescribirla:

| Objetivo | Estado | Evidencia |
| --- | --- | --- |
| `V1` | `MET` | Release `ecbf33a85b21c9ac34fa0041097a64978fbb24ff` activo en el UUID `70c64668-1738-4dfe-ba1f-7e72026bb7fe`; overview 200; sin OOM. |
| `V2` | `MET` | Cinco muestras de navegador: shell mediana 262,8 ms/máx 277,4 ms; matriz 37 mediana 4.296 ms/máx 5.430 ms; reapertura mediana 5 ms/máx 6,9 ms. Probe HTTP frío 18,683 s y caliente 4,392 s. |
| `V3` | `MET` | Equivalencia v1/v2 de conteos, PIT y última barra; 37 activos; cero hidrataciones ajenas; cache hit sin recomputación pesada. |
| `V4` | `MET` | Plan sellado `3bfa2561…`, 33 rutas retiradas, readback remoto verificado y recibo `2625569b…`. |
| `V5` | `MET` | WSL `879.265.140.736 B` → `942.896.627.712 B`; reserva preservada. |
| `V6` | `MET` | `Python 3.12 quality` verde para `ecbf33a…`; documentación y entrega verdes. |

El probe HTTP frío de 18,683 s quedó por debajo del máximo de 20 s; las cinco muestras de navegador
se ejecutaron con la firma del mismo corte ya cacheada. No se presenta esa muestra como mediana
fría. La limpieza retiró 33 rutas y conservó releases, bindings y rollback; `workspaces/default` y
la rehearsal de 43 permanecen por falta de cobertura remota demostrada.
