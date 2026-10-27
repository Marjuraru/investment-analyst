# V2 operational readiness

Documento vigente de readiness operativa para retomar `FUNDAMENTALS-COVERAGE`. No declara cobertura
fundamental nueva, no cierra esa ruta y no sustituye la autoridad de
[`product_roadmap.md`](product_roadmap.md) y [`basic_functional_release_plan.md`](basic_functional_release_plan.md).

## Autoridad y corte

| Campo | Valor |
| --- | --- |
| Work Block | `FUNDAMENTALS-COVERAGE-CYCLE` (#363) |
| Base declarada | `origin/main@a9dd555b9368168d145e357f380e9d0c47acfd41` |
| Runtime desplegado | `PENDIENTE_DE_MEDICION` |
| Workspace activo | `workspaces/active-v2-20261021` |
| UUID v2 | `70c64668-1738-4dfe-ba1f-7e72026bb7fe` |
| Rollback de referencia | `d2d2c36149db717bb94a810b15503ceb17786061` |
| Medición de adquisición | `PENDIENTE_DE_MEDICION` |
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
| `C3` | Coste estructural A3 | Precio PIT compartido entre cobertura de mercado y valoración dentro de la misma consulta de cobertura: una sola lectura de blob y una sola materialización, con resultado idéntico. Evidencia: `test_read_path_efficiency_integration.py`, `test_service.py` y benchmark/probe read-only. |
| `C4` | UI ágil real | Shell mediana ≤2 s y máximo ≤5 s; matriz de 37 activos fría y caliente mediana ≤10 s y máximo ≤20 s; reapertura visible mediana ≤2 s y máximo ≤5 s, con revalidación medida aparte; cero timeout/5xx. Cinco muestras por escenario, navegador real y HTTP local. Evidencia: `scripts/smoke_v2_readiness.py`, `browser-smoke.json` y `/api/v1/read-performance`. |
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

## Resultados medidos

`PENDIENTE_DE_MEDICION`. Esta sección registra, para el árbol candidato exacto: fecha UTC, `known_at`,
ventanas, SHA/tree, cinco muestras por escenario (shell, matriz 37 fría/caliente, reapertura,
gráfico, fundamentales, valoración y comparación), p50/máx, bytes, errores/5xx, cache hit/miss,
tiempos de revalidación y el resultado de la matriz de causas. El detalle vive en
`ops/fundamentals-coverage-cycle/smoke.json` y `browser-smoke.json`, no se copia aquí.

## Presupuesto físico y retención

| Campo | Declaración | Real |
| --- | --- | --- |
| `rutas_proposito` | Rama aislada para código/tests/docs; scratch sintético temporal; `ops/fundamentals-coverage-cycle` para recibos; release nuevo por gestor existente; workspace activo sólo ingesta reciente acotada. | `PENDIENTE_DE_MEDICION` |
| `bytes_nuevos` | Diff/docs/recibos ≤10 MiB; release fuente ≤32 MiB; ingesta reciente UNKNOWN hasta dry-plan, techo 512 MiB autorizado para el smoke. | `PENDIENTE_DE_MEDICION` |
| `bytes_modificados` | Código acotado por diff; DB/WAL medido antes/después; GET dominio 0. | `PENDIENTE_DE_MEDICION` |
| `temporales` | Scratch sintético ≤128 MiB y perfiles/logs ≤16 MiB; retirar sólo temporales propios. | `PENDIENTE_DE_MEDICION` |
| `pico_adicional` | Techo 768 MiB incluyendo release, scratch, ingesta y WAL. | `PENDIENTE_DE_MEDICION` |
| `reutilizados` | Entorno Python/browser, workspace v2, rollback vigente, cadena Drive y recibos de 48/361. | Sin copias nuevas. |
| `exclusivos_retirables` | Sólo scratch propio del bloque, medido. | `PENDIENTE_DE_MEDICION` |
| `libres_host_wsl` | Host ≥5.000.000.000 B y WSL ≥5.000.000.000 B antes de escritura material. | `PENDIENTE_DE_MEDICION` |
| `reserva` | ≥5.000.000.000 B libres en cada FS más pico restante admitido. | `PENDIENTE_DE_MEDICION` |
| `retencion` | Código/docs en Git; recibos compactos hasta cierre/auditoría; release actual y rollback según gestor. | Datos nuevos append-only; sin backups nuevos. |

No se agregó ningún backup, copia de DB ni restore. La ingesta real se limita al último intervalo
pendiente de hasta siete días completados; si el dry-plan demuestra que la evidencia local ya
permite el smoke, no se adquiere nada. Cualquier operación prevista ≥100 MB se anuncia antes.

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
