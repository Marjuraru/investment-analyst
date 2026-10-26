# V2 operational readiness

Documento vigente de readiness operativa para retomar `FUNDAMENTALS-COVERAGE`. No declara cobertura
fundamental nueva, no cierra esa ruta y no sustituye la autoridad de
[`product_roadmap.md`](product_roadmap.md) y [`basic_functional_release_plan.md`](basic_functional_release_plan.md).

## Autoridad y corte

| Campo | Valor |
| --- | --- |
| Work Block | `FUNDAMENTALS-COVERAGE-READINESS` (#361) |
| Base declarada | `origin/main@c3ff4008685e668ca76a4eb02204eeea9fb5d4ad` |
| Runtime desplegado al inicio | `c3ff4008685e668ca76a4eb02204eeea9fb5d4ad` |
| Workspace activo | `workspaces/active-v2-20261021` |
| UUID v2 | `70c64668-1738-4dfe-ba1f-7e72026bb7fe` |
| Rollback de referencia | `30a67111617f7d30f22ad16ef67e29f0d86687ee` |
| `route_effect` | `ADVANCES` sobre `FUNDAMENTALS-COVERAGE`, que permanece el único `NEXT` |

Los valores de runtime y workspace se leyeron de `runtime/deployment_state.json` y
`runtime/deployment_workspace_binding.json`. Un SHA nuevo invalida las mediciones de este documento
hasta repetir la matriz correspondiente.

## Objetivos finitos V2-READINESS

Denominador estable: seis objetivos `V1`–`V6`. Un baseline no medido es `UNKNOWN`, nunca cero. Las
metas de latencia son criterios de aceptación, no afirmaciones actuales.

| ID | Criterio final | Medición, límite y evidencia |
| --- | --- | --- |
| `V1` | Runtime y datos operativos | Candidato desplegado al mismo UUID; salud HTTP 200; checksums/seal de superficies afectadas sin alteración; scheduler y errores explícitos; cero OOM. Evidencia: `runtime/deployment_state.json`, `deployment_workspace_binding.json`, probes de salud y logs del release exacto. |
| `V2` | UI ágil real | Shell utilizable mediana ≤2 s y máximo ≤5 s; primera matriz de 37 activos mediana ≤10 s y máximo ≤20 s; reapertura sin cambios mediana ≤2 s y máximo ≤5 s; cinco muestras por escenario con navegador real, sin timeout ni 5xx. El arranque del proceso se mide aparte. |
| `V3` | Lectura correcta y coste estructural | Equivalencia Decimal, IDs, PIT, orden y conteos; cero modelos de activos ajenos en selección individual; cero recomputación pesada en cache hit válido; corpus ajeno 1x/10x sin aumentar hidratación; requests idénticos coalescen. Evidencia: pruebas focalizadas y matriz read-only. |
| `V4` | Resguardo remoto sin copias locales inútiles | Cadena recuperable vigente; faltantes únicos preservados; lista literal de retirada y retención con motivo; cero backup/restore/staging local grande sin consumidor final justificado. Evidencia: recibos de `manage_storage_retirement.py`, manifests remotos y readback. |
| `V5` | Presupuesto físico | Host y WSL ≥5.000.000.000 B más pico antes de cada operación grande; final ≥reserva; deltas reales y enlaces físicos contabilizados; ninguna cifra de liberación inferida. Evidencia: `df -B1` y recibos con bytes lógicos, asignados y exclusivos. |
| `V6` | Documentación y entrega | Matriz markdown sin instrucciones vigentes contradictorias; enlaces y pruebas válidas; `FUNDAMENTALS-COVERAGE` único `NEXT`; CI exact-SHA, AUDIT y FINALIZE según policy. |

## Resultados medidos (2026-10-26)

| Objetivo | Estado | Evidencia |
| --- | --- | --- |
| `V1` | `MET` | Release `ecbf33a85b21c9ac34fa0041097a64978fbb24ff` activo, mismo UUID `70c64668-1738-4dfe-ba1f-7e72026bb7fe`; `GET /api/v1/overview` 200; `Result=success`, `NRestarts=0`; `MemoryPeak=1.897.328.640 B` < `MemoryMax=3.221.225.472 B`; `oom=0`, `oom_kill=0`; rollback previo `3ece53b…` conservado. |
| `V2` | `MET` | Smoke real de navegador, cinco muestras: shell mediana `262,8 ms`/máx `277,4 ms`; primera matriz mediana `4.296 ms`/máx `5.430 ms`; reapertura mediana `5 ms`/máx `6,9 ms`; cero 5xx. Probe HTTP directo del mismo corte: `18.683 s` en frío y `4.392 s` en caliente. `browser-smoke.json` SHA-256 `ee2abf270c9a22c58f4fb510b79970bd5d226bc2fd5e20aa4b4a07ad312409e2`. |
| `V3` | `MET` | Equivalencia v1/v2 de conteos, PIT y última barra en pruebas focalizadas; cobertura con 37 activos; `0` hidrataciones de activos ajenos en selección individual; cache hit válido sin recomputación pesada; corpus ajeno 1x/10x cubierto por las pruebas de lectura. |
| `V4` | `MET` | Plan sellado `3bfa2561…`, 33 rutas retiradas, readback remoto verificado y recibo `2625569b…`; releases antiguos retirados con recibos; `0e93c6…`, v1 y rehearsal 43 retenidos con consumidor o cobertura no demostrada. |
| `V5` | `MET` | WSL `879.265.140.736 B` → `942.896.627.712 B` libres (`+63.631.486.976 B`); host `10.116.333.568 B` libres al cierre; reserva de `5.000.000.000 B` preservada; bytes lógicos/asignados/exclusivos separados. |
| `V6` | `MET` | `Python 3.12 quality` verde para `ecbf33a…`; pruebas de ruta, documentación y entrega verdes; `FUNDAMENTALS-COVERAGE` sigue siendo el único `NEXT`. |

El probe HTTP frío de `18.683 s` queda por debajo del máximo de `20 s`; las cinco muestras de
navegador se ejecutaron con la firma del mismo corte ya cacheada y registraron `4,2–5,4 s`. La
muestra aislada de arranque en frío del navegador es la que fija el máximo; no se oculta el coste
frío ni se presenta como mediana.

## Matriz de caminos críticos

| Camino | Estado de lectura | Límite declarado |
| --- | --- | --- |
| Arranque y overview | Carga shell y preferencias; overview acotado | No ejecuta queries de tablero ni providers |
| Mesa: cobertura 37 | `universe-coverage-v1` con firma, caché y lectura de mercado v2 por proyección | No hidrata blobs de barras históricas salvo la última barra verificada |
| Activo: chart, fundamentales y valoración | Lecturas por activo con `known_at`; valoración usa proyección v2 y materializa sólo el precio seleccionado | No hay SELECT de historia completa para elegir el último precio |
| Comparación | Contrato existente de comparación diaria | Sin cambio en este bloque |
| Cazatiburones | Índice universe-wide y detalle lazy | Sin nuevas familias ni escritura |
| Reapertura | Firma, caché acotada, previews de sesión y ETag de assets | Un fallo de carga libera el tablero para reintento |

Universo completo: 37 activos. BVL se declara `N/A` cuando no hay binding de mercado y FRED queda
fuera de cobertura. AAPL es un activo más: la comparación usa AAPL, AMD, BTC y otro equity elegible
sin condiciones por símbolo.

## Limpieza y retención

La autoridad de ejecución es `scripts/manage_storage_retirement.py` con el contrato
`operational-retention-plan-v1`. Un plan sellado enumera rutas exactas, clase, decisión, dependencias,
evidencia remota, bytes lógicos/asignados/exclusivos y fingerprint de inventario. La ejecución es
separada, fail-closed e idempotente; no selecciona rutas fuera del plan ni sigue symlinks.

- `KEEP_LOCAL`: activo, rollback mínimo, configuración privada y guía pequeña.
- `REMOTE_ONLY`/`RETIRE_AFTER`: payload cubierto por la cadena cifrada en Drive y sin consumidor vivo.
- `UNKNOWN`: se conserva; no equivale a basura.
- El workspace v1 `workspaces/default` y la rehearsal de 43 sólo se retiran si la cobertura remota y
  la ausencia de evidencia única se demuestran. Si no, se conservan con la lista exacta de faltantes.
- Los releases actual y previo, sus bindings, symlinks y referencias systemd están excluidos.

La cadena remota reutilizada es `ia_drive_crypt` con base y deltas; los manifests, controles e
inventarios se leen con rclone y se verifican por SHA-256 tras el readback. No se borran objetos
remotos ni se usa `sync --delete`. Los secretos y la configuración rclone no se imprimen ni se suben.

### Ejecución real del 2026-10-26

| Evidencia | Resultado medido |
| --- | --- |
| Plan sellado | `plan_sha256=3bfa2561b0c7bd8c70f2a00c620739e6da191ca00e34354a2fbcd68898a8d239`; 40 entradas |
| Publicación remota | `v2-readiness-20261026/plan-…json` y `control-…json`; readback SHA-256 verificado |
| Retirada ejecutada | 33 rutas; `62.169.468.769 B` lógicos, `78.735.626.240 B` asignados, `53.988.843.520 B` exclusivos |
| Espacio WSL | `879.265.140.736 B` → `942.896.627.712 B` libres (delta observado `+63.631.486.976 B`) |
| Espacio host | `10.116.333.568 B` libres al cierre; reserva de `5.000.000.000 B` preservada |
| Recibo | `ops/v2-readiness-20261026/retention-receipt.json`, `completed_at=2026-10-26T20:04:59.106709Z` |
| Releases | 10 SHA antiguos retirados; `0e93c6…` retenido por el `memory-sampler` activo; current/previous/bindings conservados |
| Retenido | `workspaces/default` 16 GB y `workspaces/data-chassis-43-rehearsal` 8,3 GB, sin cobertura remota demostrada |

Las rutas retiradas eran staging, generaciones de backup y releases sin consumidor vivo. Los
controles retenidos (`repair-delta`, selecciones, `continuity-archive-v1` y manifiesto v1) siguen
locales. `UNKNOWN` no se convirtió en borrado: v1 y la rehearsal permanecen con motivo explícito.

## Entrega

`FOCUSED` durante iteración y `FULL` como gate literal `Python 3.12 quality` sobre el SHA final.
El despliegue usa el gestor de releases existente con `candidate-update` al mismo UUID; se conserva
rollback hasta que la salud del release nuevo sea verde. CI verde, smoke real y AUDIT fresco son
condiciones de cierre. Un commit invalida la evidencia anterior.

## Inventario documental aplicable

| Documento | Clasificación | Acción |
| --- | --- | --- |
| `AGENTS.md`, `docs/development_protocol.md` | `KEEP_CANONICAL` | Sin cambios |
| `docs/product_roadmap.md`, `docs/basic_functional_release_plan.md` | `KEEP_CANONICAL` | Actualizar evidencia y retirar instrucciones activas obsoletas |
| `docs/README.md` | `KEEP_CANONICAL` | Añadir este documento y corregir el estado de 46–49 |
| `docs/read_path_efficiency.md`, `docs/storage.md` | `KEEP_CANONICAL` | Documentar proyección v2 y retención operativa |
| `docs/continuous_integration.md` | `UPDATE` | Documentar el gate de cuatro shards y la autoridad exact-SHA |
| `docs/workspace.md`, `docs/workspaces.md`, `docs/application_runtime.md` | `KEEP_CANONICAL` | Reemplazar punteros de bloque cerrado por el estado vigente |
| `docs/data_chassis*.md`, `docs/historical_recovery_runbook.md` | `HISTORICAL_REFERENCE` | Rotular historia fechada; no reescribir evidencia |
| `docs/planning_context/*` | `HISTORICAL_REFERENCE` | Conservar decisiones fechadas y retirar presente activo |
