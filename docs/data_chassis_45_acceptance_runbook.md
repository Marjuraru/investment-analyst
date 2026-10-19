# DATA-CHASSIS-45: operación y aceptación

Registro versionado del incidente y límites del diagnóstico: [INC-DC45-WSL-20261016](incidents/data_chassis_45_wsl_spill_20261016.md). Autoridad operativa: #345 actualizado 2026-10-19T19:53:20Z; manifest `9bcfad4eb3c66824d873f7729126c26aa09e9cf5a7d220fa0c49c658a6720a51`. Secuencia restante exclusiva E4a→E4b→E4c→E5→E6. E0–E3 acreditados: no repetir. R0–R7/P0–P5 y las recetas anteriores se conservan como antecedentes.

## Actualización 2026-10-19T19:53Z — readiness/caché/C5 y segundo intento autorizado

#345 renovó **UNA vez** la autorización operativa `human-dc45-cut-5400-20261019` para un segundo intento con UUID/controles/sesión/freeze/GO nuevos; no reabre la ventana `df649c34…` ni concede terceros intentos. Manifest vigente `9bcfad4eb3c66824d873f7729126c26aa09e9cf5a7d220fa0c49c658a6720a51`. El primer intento sigue terminal `HEALTHY_V1`, sin reserva ni mutación F3, y se conserva íntegro.

- **E4-fix readiness:** `_CutMonitor.start_ready` exige una muestra host/R1 válida antes de crear ventana o detener v1. La última lectura válida y su timestamp se separan del intento/fallo; un fallo transitorio no rejuvenece la edad y se tolera ≤15 s; un valor real bajo el floor invalida de inmediato; un dato caducado o un fallo persistente detiene. `cut-host-probe.json` conserva sólo estado/valor/diagnóstico saneado. Regresiones `cut_monitor`: **5 passed /4,15 s**. El controlador global conserva camino feliz y fallo de host: **3 passed /28,26 s**.
- **Fallo real de caché CI:** `_owned_payload_scope` ya no rechaza cualquier `pytest`/`ruff` del host. Acota el consumidor al root solicitado mediante cwd, descriptores y argumentos resueltos; un launcher `uv`/pytest de otra raíz no bloquea. `test_owned_payload_dispatch_retires_only_bound_regenerable_cache[None]` pasa con `uv run --locked` y con ejecutable directo; lifecycle completo **69 passed /44,10 s**.
- **C5 pendiente completado:** W2 staging `E5`, cache independiente `E5` con binding preservado fuera del root y scratch acotado por recibo. `_owned_payload_proofs` diferencia delta/cache/scratch y valida manifest/files/upload/restore/readback; el inventario exige objetos delta conocidos, rechaza contenido desconocido y nunca toca DB/WAL/raw/Parquet ni controles. Pruebas C5 nuevas: scope W2/cache, retiro real de cache con binding, retiro scratch y negativos de contenido protegido.
- **Optimización acotada de preparación:** `_certified_successor_fixture` usa un template inmutable de raíz fija y un snapshot prístino; cada test recibe un reset aislado, misma ruta de ledger, reaplicando únicamente los dos patches que el fixture dejaba activos. No se eliminan casos, parametrizaciones, asserts ni cobertura. `test_workspace_v2_inventory_unit.py`: **126 passed /153,23 s** (antes ~278 s en CI), sin skips/xfails nuevos. Rastreo: setup inicial 5,47 s y cuerpos globales 3-6 s.
- Estado: v1 activo PID30533 y HTTP200/200; mantenimiento inactivo; segundo corte **no ejecutado** hasta CI completa exact-SHA verde. El candidato y su CI se publicarán con el mismo manifest; el marker BUILD se actualizará sólo a `PENDING` con la evidencia vigente.

## Actualización 2026-10-19T21:11Z — segundo intento abortado en catchup; ledger/C1 bloqueado

- Segundo F `15220aa8-bbf7-445b-a3ba-c1d64dc32206`, ventana `f337085a-3175-4246-8ae4-926cf7516460`, GO `2821c7…`, evidencia `E4f-5400-go-second-20261019.json` SHA `2bb894…`. Tras readiness válida (41,29 GB) y stop controlado, la ventana quedó terminal `HEALTHY_V1`; v1 recuperó PID80515 con overview/candidates HTTP200/200. El DB destino conserva SHA `fd18cb94…` y 2.166.632.448 B; **no hubo activación v2**.
- Causa concreta: el catchup publicó la generación `SUCCESSOR_BOUND` `7cba8e…` conservando `tree_fingerprint=1f8862…` mientras `coverage.verifier_tree_fingerprint=e49091…`; el modelo exige igualdad. `publish` no permitía avanzar `tree_fingerprint`/`environment_fingerprint`/`code` y el productor no los actualizaba. El intento falló en catchup; `_recover_global_cut` → `_invalidate_cut_owner` → `ledger.resolve()` volvió a fallar al validar el head inválido, por lo que **no se escribió evento C1** y el head/reserva quedaron preservados.
- Evidencia preservada sin borrar/editar: reserva `progress-successor-reservation.15220…`, comparación/work/new-index, generation `7cba8e…`, `failure-frames.json`, `cut-window-receipt.json` SHA `b09fc9…` y health `13380b…`. No se adopta el frozen cut anterior ni se toca la primera ventana `7ce422e1…`, que sigue `HEALTHY_V1`.
- Fix productor aplicado: `bound_v2` actualiza `code`, `tree_fingerprint` y `environment_fingerprint` al verificador actual y se valida antes de `publish`; la transición `SUCCESSOR_BOUND` permite esos campos sin reescribir el baseline. Regresión `test_successor_bound_advances_verifier_tree_without_rewriting_baseline`; inventario completo **127 passed /119,16 s**; CI exact-SHA pendiente.
- **No tercer intento automático.** El head inválido y la reserva requieren una reparación C1/ledger decidida por PLAN/HUMAN y una autorización explícita nueva; el fix no repara por sí solo los bytes históricos inválidos. E5/E6, aceptación compartida, activación y limpieza final no se ejecutaron. El marker permanece `PENDING`; no se declara PASS/READY.

## Estado vivo tras el primer intento E4c — antecedente pre-segundo-intento

**E4b-1 implementado.** `CutWindowGo` y `CutWindow` admiten `window_limit_seconds: Literal[1800, 5400]`, la identidad `human-dc45-cut-5400-20261019` y un `attempt_id` F3 explícito sólo para 5400. La suma GO, el prefijo, el reloj monotónico, las admisiones/remanente y `RuntimeMaxSec`/`TimeoutStopSec` comparten el mismo valor tipado; 1800 legacy conserva su default y validación. Los límites R1/SQL 120 s/apertura 180 s/lector44 120 s y la recuperación en el deadline ORIGINAL quedan intactos. Los recibos históricos no se reescriben.

Pruebas focalizadas: `tests/unit/workspace/test_workspace_v2_cutover.py` **35 passed /9,42 s**; `tests/integration/application/test_data_chassis_closure_cli.py` **78 passed /8,70 s**; controlador global feliz **2 passed /17,14 s**; Ruff/format/diff-check PASS. E4a no se repitió.

**E4b-2 GO 5400 único.** Evidencia `E4b-5400-go-20261019.json` SHA256 `0dbf298cc8cd145f3078b668c3c8619edf491c631896ff22b6b961e2e28e6a2b`; F3 `df649c34-1df3-4f70-a45a-84ba4ce142c6`; cotas freeze180/comparación600/operacional300/plan600/upload420/restore2100/activación180/cierre300 = **4680 s**, margen300, prefijo plan60, techo absoluto de prefijo420 y reserva300; total plan **5040 s <=5400**. `cut-go.json` SHA256 `9e2f0ab957d68923c098d9f5f9f07a1af35d268f6fe321017511e4f64ff851bf`; presupuestos `cut-budget.json` `8b4aa8462d98631d0be313948426d452aab01c0991cdf3672c089567e2aba203` y `readback-budget.json` `35e0850076c6dd265da65dabf934c2a6bfa81b1836beaa4d957cf438f2ddb17b`. El GO admite un intento acotado; no certifica éxito.

**E4c intento único, aborto seguro.** Ventana `7ce422e1-be8e-417f-907a-c014d817d0a7`, F3 `df649c34-1df3-4f70-a45a-84ba4ce142c6`, fase terminal `HEALTHY_V1`; `cut-window-receipt.json` SHA256 `0155603a9c35704fde36e0bc5861bf3aaaf67bf4c4e6b833788221c9ae2b9b6c` y `cut-recovery-phase.json` SHA256 `480f1aa00a491380d81610c445185a24ff7abb886896217c6ebfb4ace9288b37`. El monitor abortó antes de catchup con `host_capacity_unavailable_or_below_floor`: primer sample `host_free_bytes: null`, sample siguiente `36963520512` y capacidad viva `36963524608`; `E4c-cut-attempt-failure-20261019.json` SHA256 `835870541ffa016588f9b1297a44f930a2232ad724346bfcd4d8b81b28b2e917`. No hubo reserva ni mutación F3; v1 recuperó PID30533 y HTTP200/200. **No se reintenta:** la ventana única está consumida y una segunda ventana 5400 exige decisión humana explícita; no se rebaja el floor ni se elimina el gate de host. E5/E6 siguen pendientes; no PASS/READY.

## Punto único de reanudación — antecedente pre-5400 (2026-10-19 UTC: E4a terminal, E4b NO GO)

Checkpoint de partida `measure-coverage-W2-20261019/checkpoint-sleep-20261019T071654Z/checkpoint.json`, SHA256 `e2a9298469296d047c80d5e9183ac0f8fcb26e2ae138c2f7b73d74b09217264a`: 28 hashes verificados al reanudar. Cambios locales conservados sin aplicar el patch. Handoff fuera de allowlist protegido y excluido de publicación. E0–E3 no se repitieron. B/F0/W/F2/W2, padre remoto, journals y release44 conservados. F3 no reservado; v2 no activado; v1 sigue en servicio.

| Fase | Artefacto/bindings | Resultado/reutilización | Siguiente acción |
| --- | --- | --- | --- |
| E0–E3 | Recibos terminales W2, inventario `1854b189…eaef`, certificado `73db6543…9b66` y release44 | DONE; conservar, no repetir migración/plan/upload/restore/readback/retiros acreditados | Ninguna ejecución repetida |
| E4a | `measure-coverage-W2-20261019/comparison-typed-measurement-phase.json`, SHA256 `494b0fcc559318a46ed1a73bad5e2097aaf820f3a925c0df971c5019f6cc9643`; worker `resource-run-19e3c015e4614b379554c4d4c0e8c750` | TERMINAL 421,085675 s; físicamente RO, sin imports/reserva ni cambios de DB/head | Reutilizar esta comparación de calibración, no sustituirla por los 77 s anteriores |
| E4b | Tabla conjunta y `E4b-go-assessment-20261019.json` en el mismo directorio | NO GO; no se ha fabricado un CutWindowGo | Resolver la frontera de ventana/slot descrita abajo con PLAN |
| E4c | No hay F3, CutWindow ni nueva quiescencia | No iniciado; v1 activo | Sólo después de GO completo, mediante `cut` |
| E5 | Evaluador Q4-F implementado/probado; cero lotes operativos v2 | Aceptación operacional y gates finales C5 pendientes | Compartir los dos lotes Q5/Q6/Q7 después de activar; retirar al liberar consumidores |
| E6 | HEAD publicado `60d9a3f…4326` más delta local conservado | No nuevo candidato ni CI; no PASS/READY | Candidato final/CI/AUDIT/FINALIZE después de E4c/E5 |

E4a comparte documento y metadatos físicos en una consulta por lote de IDs/rango available_at, <=256/1 MiB. Misma hidratación tipada, identidad, metadatos, Decimal/UTC y hash canónico; sin ART pesado ni índice persistente nuevo. EXPLAIN bajo settings reales confirma scans secuenciales en los lookups anteriores; el compartido añade el rango, pero continúa siendo el coste dominante. Los planes y settings están en `native-settings.json` del worker, y sus bytes productores en `source-bindings.json`/`code`.

Resultado completo de la MISMA pasada: fuente raw929518/observaciones530565/métricas2063282/diagnósticos2053; baseline conocido929333/530071/2053921/2036; novedades185/494/9361/17. Fallback métrico137891:128530 conocidos con hash JSON distinto y9361 nuevos. **Cero discrepancias semánticas y cero discrepancias de metadatos** entre conocidos: los128530 hashes canónicos coinciden con W2. No se ha supuesto equivalencia por ID ni muestreado. Métricas: SQL inicial33,098363 s, lookups334,609531 s, hidratación1,755583 s, canonicalización/validación5,278453 s, stream/digest5,613818 s; total familia381,418954 s. Pico cgroup1973960704 B/RSS1461751808 B; eventos0/0/0; límites nativos1GB por DB, spill agregado<=1GB. IO rchar206763205685 B/read_bytes346794066432 B: son lecturas acumuladas, **no crecimiento de disco**. Escritura syscall1187211387 B incluye temporales ya cerrados, no datos importados.

Regresiones relevantes: límites por lote y catchup/sello incremental0/1/3/JSON no canónico: **6 passed /31,53 s**, log `E4a-shared-read-regressions-20261019.txt`. Las8 pruebas de calibración se ejecutaron:7 pasaron y1 falló en el binding del fixture al coincidir una edición de instrumentación con esa prueba; ese caso se repitió con fuente congelada y pasó /5,62 s. Fallo de ejecución reconocido, no diferencia de datos ni fallo ocultado. La medición REAL posterior mantuvo su código congelado hasta terminal. C2 conserva hashes `bf35934d…56bd` y `dceec7e4…8cc`; C4 no se editó.

### E4b — tabla conjunta: observación parcial no equivale a cota certificada

| Paso | Trabajo/volumen | Productor/policy y evidencia reutilizada | Medición o límite efectivo; cota completa |
| --- | --- | --- | --- |
| freeze | Stop writers, fingerprint DB v1 y generación física, locks/sesión nuevos | `quiescent_source_cut`, C1/R1 actual; F2 sólo antecedente | **UNKNOWN**; no se reutiliza una quiescencia histórica ni se detiene para medir |
| comparison_delta | Conjuntos completos actuales,185 raw/494 obs/9361 métricas/17 diagnósticos nuevos observados, imports/intents/fsync | Código archivado del worker19e3…, `comparison-budget.json` | Comparación421,085675 s; **imports/checkpoints no incluidos, cota completa UNKNOWN** |
| operational |12 familias, estado vigente F3 y captura pareada Q1–Q3 | `_catchup_live`/`_capture_cut_bytes`; F2 op57225416 B sólo volumen histórico; collector v2W2 previo15,729419 s | **UNKNOWN** para origen+destino+estadísticas; no sumar dos veces el mismo collector |
| physical_plan | Inventarios padre/W2 completos, DB mutable actual, unidades nuevas, serialización y controles F3 | `plan-reuse-measurement-phase.json`; misma representación/R1 |478,191659 s **parciales**; fronteras de bloques corregidas después, sin certificar materialización final ni repetir el plan W2; cota completa **UNKNOWN** |
| upload | Objetos nuevos por digest y controles F3, lecturas independientes; cambios reales DB no presuntos | E1 `prepare-W2-20261018/upload-phase.json`, mismo namespace/config |294,586381 s para controles W2 (~354 MB), cero payload transferido; **no cota F3** y novedades no cuantificadas hasta sellar F3 |
| restore_open | LINK controlado completo, materialización mutable independiente, cobertura física/raw y lector44 del MISMO F3 | `_apply_controlled_link_page`, C4/R1; `coverage-native-phase.json`; release44 | Cobertura288,075689 s; apertura nativa<=180 s y proceso lector44<=120 s por límites efectivos. **LINK/mutables y cota completa UNKNOWN**; UNLINK340 s no acredita LINK |
| activation_health | Activación44 preparada sobre raíz/UUID correctos, binding/runtime/HTTP | LocalReleaseService/cut y pruebas de fallos existentes | **UNKNOWN** como cota productiva completa; fixtures no sustituyen volumen/entorno real |
| cancel_close_restart | Cancelar sólo worker propio, cerrar/invalidate C1, v1 saludable en mismo reloj; forward-v2 tras ACTIVATING | R1 stop_grace30 s, monitor+ExecStopPost y fallos focalizados existentes |30 s son cierre del worker, **no recuperación completa**; cota de reinicio/salud restante UNKNOWN |

Subtotal de cuatro **observaciones parciales** contadas una vez:421,085675+478,191659+294,586381+288,075689 = **1481,939404 s**. Deja318,060596 s antes de LINK, imports, freeze, operacional, apertura, lector44, activación, reserva y margen. Si se usan los topes efectivos actuales de apertura180 s/lector120 s/cierre30 s como cobertura conservadora de esas ausencias, el escenario parcial llega a **1811,939404 s**, ya sin margen y sin los restantes costes. Esto **no demuestra una duración física mínima ni una imposibilidad de toda optimización**; demuestra que los recibos/topes disponibles no acreditan GO1800. No se publica un GO tipado con campos desconocidos ni se ensaya una parada.

**Frontera concreta a PLAN:** el contrato actual de restore exige slot vacío y checkpoint ligado a UN manifest/destination; el retiro preparatorio exige imagen/restore/cobertura/readback acreditados. No representa un slot con sólo inmutables preensamblados que se complete posteriormente con el F3 nuevo, ni su abandono/retirada antes de una imagen completa. Preparar ese ensamblado antes del stop permitiría sacar LINK del camino crítico sin otra migración o restore del padre, pero BUILD no puede inventar esa transición ni rebautizar W2 como F3. Propuesta mínima para decisión: estado preparatorio ligado a certificado/inventario/testigo W2, exclusividad y prueba de no DB/WAL/mutable enlazado; transición explícita al mismo F3 por identidad/contenido y sólo novedades; checkpoint/recuperación/retirada por C4 preservando la cadena original incluso si falla antes de completar imagen. Mantener1800 s/R1/recuperación externa previa a activar y un único slot. PLAN debe autorizar esa extensión o concretar otra vía dentro de la ventana con cotas suficientes; no se cambia arquitectura, ventana ni aceptación desde BUILD. No repetir un restore/LINK completo bajo otro nombre para llenar esta ausencia.

### Q4-F integrado; escenarios y C5 todavía condicionados a v2

Policy `ordinary-batch-growth-v1`: <=30.000.000 B de crecimiento positivo DB+WAL **por lote**, en ordinary y same_inputs. El evaluador valida activación/UTC/runtime/UUID/scope/matriz/attempt IDs/fronteras cronológicas/exclusividad; suma positivos por archivo en CADA intervalo, incluidos huecos entre intentos y sin compensar caídas. Same_inputs exige scopes iguales y Q6 sin equivalentes nuevos. El informe v2 expone policy/unidad `B/batch`; v1 sigue leyendo/evaluando su regla semanal histórica. **39 passed /0,17 s** en el archivo de aceptación, incluida regresión legacy y negativos. El caller `report` ya consume el input tipado y publica el informe seleccionado por policy; no se añade otra CLI.

Compartir los intentos reales y sus refs con Q5/Q6/Q7 cuando v2 esté activo. No se han ejecutado ni declarado MET. Los digests de inputs/versiones deben incluir los parámetros y los checks ordinarios obligatorios de proveedor; no basta igualdad de contadores. La observación diaria queda no bloqueante y no se afirma B/día. C5: staging W2/cache independiente se conservan por consumidores F3/retry pendientes; v1 por servicio activo y gates origen; slot W2 ya retirado no se recreó. No borrar para aparentar cierre ni aplazar una retirada que efectivamente haya liberado sus consumidores.

## Antecedentes hasta el checkpoint de reposo — no son la receta vigente

Las órdenes y estados pendientes que siguen conservan trazabilidad histórica. Su ejecución queda supersedida por el punto único y la tabla E4b anteriores; no ejecutar estas recetas para volver a E0–E3 ni tratar una instrucción histórica de siete días como gate actual.

Último resultado real: comparación de **consultas** terminada en 76,941019 s,
`comparison-measurement-phase.json` SHA256
`d0e203d9b1ff1d440dccfd424c7df7fdda86903ef925a68aa5b86a8e222e42c3`,
worker `resource-run-3e1d44e3881745e2a6d0e99d56b76d80`, pico1927966720 B,
sin OOM/oom_kill/max. Ambos motores se abrieron físicamente READ_ONLY y no
cambiaron DB/head; sin imports/reserva. Fuente observada: raw929426 (93 nuevos),
observaciones530318 (247 nuevas), métricas2058759 (4838 nuevas), diagnósticos2045
(9 nuevos). Conocidos W2: 929333/530071/2053921/2036. Hay **133368 métricas** que
requieren fallback tipado, incluidas 128530 conocidas: motivo aún no clasificado.
Los 77 s NO incluyen ese fallback ni escrituras, por lo que NO son GO.

Corrección posterior guardada: `_validate_successor_comparison_rows` agrupa
get_many y metadatos por <=256 modelos y <=1 MiB, conserva IDs/Decimal/UTC y el
hash canónico, validación de identidad y rechazo de metadatos alterados. Evita
una consulta por cada fila lenta. La medición reutiliza repositorios tipados
sobre un segundo cursor registrado del MISMO motor físicamente RO; no abre el
facade v1, que intentaba modo RW y provocaba conflicto en fixtures. No cambia
schema, R1, C2 histórico ni C4. Código de estas mejoras archivado en el checkpoint,
pero la medición tipada REAL todavía NO se ejecutó. Pruebas actuales: 8 passed
/45,14 s (lectura/0/1/3, WAL, generación, cambio durante lectura, formato JSON
no canónico y metadatos inválidos); 2 passed /1,13 s (lotes métricas/diagnósticos,
1 MiB/256 y hashes); 1 passed /7,96 s (catchup+recovery real en fixture con JSON
no canónico). Ruff/check-format de los cinco archivos afectados y diff-check
correctos; no CI nueva y no suma de grupos solapados.

**Siguiente acción exacta:** revisar el plan de los lookups get_many del fallback
con los index scans actuales desactivados (riesgo de scans repetidos, no un
fallo confirmado). Después ejecutar `prepare-delta --delta-action measure-comparison`
con mismo W2 y `comparison-budget.json`, nueva salida exclusiva
`comparison-typed-measurement-phase.json` si sigue ausente. El cuerpo actual
incluye el fallback por lotes y sus contadores de discrepancia hash/metadatos;
no sobrescribir el recibo anterior de consultas ni repetir plan/upload/restore.
Mantener v1 activo y una sola fase pesada, congelando código hasta terminal.
Con su cota, cerrar las otras filas de GO antes de cualquier parada. C5 final,
aceptación, publicación/CI y Q4 continúan pendientes; no READY/PASS anticipado.

E4 — comparación pendiente de cota real: el coordinador existente incorpora
`prepare-delta --delta-action measure-comparison`. Comparte exactamente los
constructores SQL del catchup raw/observaciones y comparación financiera;
ambas DB se abren con `read_only=True`, índices/proyecciones sólo temporales,
sin hidratación financiera, imports, reserva ni quiescencia declarada. Rechaza
WAL, generación no certificada/reservada y cambios de identidad de DB/head
durante la lectura. El recibo distingue filas conocidas y pendientes de validar
tipadamente; no acredita comparación final ni activación. La medición no
incluye appends, fsync de checkpoints ni transferencia operacional, que deben
presupuestarse por separado. Pruebas: 9 passed /55,49 s (0/1/3, WAL, cambio
durante lectura, generación ajena y tres catchups reales con el SQL compartido),
dispatch 11 passed /1,21 s. Funciones C2/C4 históricas sin modificar. Siguiente
acción: ejecutar una medición en la unidad R1 con v1 activo, misma generación
W2 y delta sellado; controles `measure-coverage-W2-20261019`, presupuesto nuevo
`comparison-budget.json` (dos DB/1 GB cada una, spill agregado 1 GB), salida
exclusiva `comparison-measurement-phase.json`. No repetir E1/E3 ni el plan W2.

Primer intento de esa medición: `resource-run-da999d4bcd40432da4ccd528882c8e20`,
terminado con `OutOfMemoryException/native_memory` al ejecutar la consulta raw
SHA `e716fcc8a4472575977dbb4575d362bc02314bda3b2b6beb43b9d1df904b185e`.
`failure-frames.json` y `native-settings.json` conservan diagnóstico/productor;
sin recibo de éxito, imports ni sucesor, v1 activo. No fue OOM del cgroup:
última muestra eventos 0/0/0, 1,45 GB cgroup y 149 MB temporales.
Corrección de esa costura: las tablas TEMP raw/observaciones ya no crean ART
auxiliar PRIMARY KEY; la carga valida explícitamente duplicados. Se desactiva
preservación de orden de inserción sólo en la sesión de comparación; todo orden
de significado conserva ORDER BY. Schema/índices persistentes intactos, R1 sin
aumentar. Se captura EXPLAIN antes de ejecutar para diagnosticar el siguiente
resultado. Base técnica: [DuckDB OOM](https://duckdb.org/docs/lts/guides/troubleshooting/oom_errors)
y [tuning](https://duckdb.org/docs/current/guides/performance/how_to_tune_workloads).
Pruebas posteriores: 9 passed /56,21 s (incluye el catchup real con adiciones)
y 6 passed de proyección (duplicados, >3 MiB e interrupción). Reintentar sólo
esta medición con mismo W2/presupuesto/salida aún ausente; no otros pasos.

Delta E4 acotado: `measure_prepared_plan` y `prepare-delta --delta-action
measure-plan` miden la ruta W2→F3 sin publicar plan, reservar sucesor ni regenerar
W2. Se valida el inventario sellado completo; los inmutables requieren el
testigo UNLINK W2 y su identidad actual. La DB se lee de nuevo. Como máximo
tres unidades delta existentes (≤8 MiB cada una) prueban escritura/fsync en el
temporal admitido y se retiran al salir; no se copia la DB completa. El recibo
es calibración parcial, no GO: faltan las otras fases, controles y margen.
Pruebas focalizadas: 6 passed /17,25 s (sin modificación de W2/DB, testigo,
temporales y raíz ajena) y 10 passed /1,07 s (dispatch certificado, reserva,
verificador y bindings negativos; grupos sin sumar). Funciones C2 y lector C4
históricos conservados. Comando ya ejecutado (no repetir): misma generación W2, controles
`measure-coverage-W2-20261019`, presupuesto `coverage-budget.json`, acción
`measure-plan`, salida exclusiva `plan-reuse-measurement-phase.json` y testigo
`retire-W2-20261019/unlinks-W2/page-00003627.trailer.json`. Mantener v1 activo.

Medición terminal `plan-reuse-measurement-phase.json`, SHA256
`c3d79aabd5a6bc767de6e3e5ac171861cbc682f9f5e135898df4c527874bb405`:
478,191659 s total (24,005905 índice padre; 450,843363 lectura/codificación),
pico cgroup 1913823232 B, eventos 0/0/0. Cobertura 930598 archivos y
5907411805 B; sin plan publicado. Tres unidades de escritura, máximo observado
0,007734 s/unidad. v1 PID283 siguió activo/HTTP200; unidad terminó/MainPID0.
Este recibo produjo 2582867135 B delta en la simulación: detectó pérdida de
alineación al omitir las longitudes de bloques delta entre fragmentos padre.
Corrección puntual posterior: `_plan_prepared_delta_files` pasa todos los
límites sellados; `_plan_delta_file` sólo concede origen padre a un fragmento
padre cuyo contenido actual coincide. Un bloque delta coincidente conserva
origen delta y su object key; ninguna igualdad de la DB se supone. Se conservan
el recibo y productor anteriores, sin cambiar sus hashes. No usar sus bytes
simulados como la transferencia F3 ni regenerar W2 para corregirlos.
Pruebas afectadas: `prepared-boundaries-tests.txt`, 15 passed /36,93 s
(intercalado padre/delta/padre, 0/1/3, restore sin staging y testigo C4);
`boundaries-controller-tests.txt`, 2 passed /17,65 s (coordinador integrado
0/3, manifest/restore del mismo epoch y activación44 en fixtures). Duraciones
no sumables con grupos previos. Siguiente gate E4: completar cotas de las fases
restantes; esta medición aislada no es GO.

El mismo presupuesto de comparación incluye la corrección de
`_load_recovered_core_projection`: sustituye miles de INSERT por páginas por
una carga nativa desde una proyección temporal raw/observaciones; conserva las
comparaciones de identidad/contenido y metadatos, sin reconstruir finanzas.
La longitud CSV se deriva de los datos, incluyendo escape UTF-8, sin límite
nuevo de dominio. Pruebas: `core-native-unit-tests.txt`, 6 passed /0,77 s
(0/1/257 filas, metadatos de más de 3 MiB, tabulaciones/saltos/comillas,
interrupción y eliminación del temporal); `core-native-controller-tests.txt`,
6 passed /49,72 s (ruta integrada 0/1/3 y recuperación antes/después de
ACTIVATING). Logs en `measure-coverage-W2-20261019`. No convertir esas duraciones
de fixtures en cotas de producción ni modificar los proofs históricos C2.

E0 y E1 tienen recibos terminales, documentados abajo. W2 permanece certificado,
sin sucesor reservado; F3 no existe y v1 continúa activo. No repetir el plan ni
upload W2. E3 restore y readback terminaron; no repetir ninguno.

E3 readback terminal (2026-10-19T01:38:16Z):
`prepare-W2-20261018/readback-phase.json`, SHA256
`d42b1fde1f0e5a4941da5f7bccc8d7595cc56dd6bf0f482c4e0cb49b152168c3`;
**489,382800 s**. Cobertura completa: 930598 archivos /5907411805 B,
929333 raw presentes y 929333 entradas del índice, digest de corpus
`86eab67d1a60a2511daedef245b774552a14fc400c2dc641446b41f088dfd9fd`.
Lector44 instalado real: 929333 raw, 530071 observaciones, 2053921 métricas y
2036 diagnósticos. Modelo readback archivado `compatible-recovery-readback.f06515c6bd596735d53b082bd6c0ac8efaf28d1ac139ee4e1bc843182c5aea1c.json`;
restore serializado SHA `4d35daed143e11f6b70a454e76208e6b7ba1a593273db1d14e507f504262427c`.

E3 restore terminal (2026-10-19T01:27:30Z):
`prepare-W2-20261018/restore-phase.json`, SHA256
`0ec52fc1d691d5b88642615a6865ab707c79f086069579858c31593cc90113c7`;
**1837,190770 s**, 930598 archivos, epoch/manifest/files W2 sin cambios,
APPLYING. Se enlazaron 928660 archivos /3324524951 B; ninguna DB/WAL enlazada.
Transferencia remota: upload/download payload y controles **0 B**, reuso del
cache independiente real ya acreditado. RSS HWM proceso 275931136 B; no
confundir con pico completo del cgroup. Testigo LINK W2:
`links-W2/page-00003627.trailer.json`, SHA256
`25b9bce5624aac15f46f0d8d8bfe3b6fe89122e7f187e72e9cc0a82019468ae9`.

**E4 NO GO medido:** restore solo ya supera 1800 s, antes de readback y demás
fases. No parar v1 ni repetir restore W2. Los timestamps de las confirmaciones
existentes sitúan LINK en 00:55:54–01:20:26 UTC, aproximadamente 1472 s. Es el
coste dominante a resolver dentro de C4, preservando intent antes de mutación,
fsync antes de cursor, detección de cambios y compatibilidad de testigos.

Delta E4/C5 justificado por ese NO GO: `_apply_controlled_link_page` produce las
mismas páginas C4, sincronizando cada directorio afectado una vez por página
antes de confirmar/cursor; revalida identidades tras la barrera. Una página
interrumpida usa `ImmutableLinkJournal.apply_page` original y rehash de los
afectados. El lector/replayer original y su hash `39873614…8aa8` no cambiaron;
el productor nuevo queda identificado separadamente en el snapshot del worker.
No atribuirle retrospectivamente el restore E3 de 1837 s ni reescribir testigos.
`_restore_controlled_parent_links` y retiro comparten la lectura del antecesor
con lookahead acotado a 256. `_verify_full_recovery_coverage` inserta el mismo
conjunto raw con dos arrays de hasta 256, conservando hashes, cobertura y joins.

Pruebas nuevas: 16 passed /11,33 s (LINK/UNLINK, interrupción después de intent,
mutación, fsync, confirmación y trailer; ctime desconocido antes/durante barrera);
19 passed /14,64 s de retiro preparatorio, incluida foto W2 con inventario C4
histórico cuyos ordinales difieren del manifest actual. Grupo anterior 40 passed
/45,16 s solapa estos casos: no sumar. `retire_preparatory_slot` une por path/hash/
longitud y preserva ordinales originales; la foto W2 usa su epoch/certificado/
DB/cobertura reales, sin relabel como COMPLETE.

Acción C5/E4 terminada: retirado sólo el ensayo W2; slot ausente y v1 activo
con HTTP 200 después del retiro. Controles `ops45/retire-W2-20261019`, operación
`7cb0d7f5-f3df-5aa2-9d9e-d41f2558f235`; input SHA256
`1ba4d4ed04fdea2d55fa9590f910715b38537d98ec5c6a3edd7568b57e4298d4`.
Se conservan las doce pruebas y los journals fuera del slot. Recibo terminal
`retirement-phase.json` SHA256
`dc192445d8b5aa63c9b591f37fe742febab8d04b5e764aa5ccc9c13e4ccc1a7e`:
869,259189 s; 930598 archivos /5907411805 B confirmados, APPLYING preservado.
Prueba `preparatory-retirement-proof.json` SHA256
`406acbb3ab518b4ec01c6f35db51c189da1c9b1b53e2ec001c132ea4dd509b30`;
testigo `unlinks-W2/page-00003627.trailer.json` SHA256
`35ac1908ee81ff9c75870ae07feb742eaa9f27745bb7ecee247c8dcd5259f35f`.
UNLINK optimizado medido entre confirmaciones: 01:56:26–02:02:06 UTC (~340 s).
No atribuir este tiempo a LINK ni usarlo como GO por sí solo. Pico cgroup
1897807872 B; RSS HWM 270180352 B. Siguiente acción: cerrar captura pareada
Q1–Q3 en la misma sesión F3 y las ocho cotas conservadoras antes de parar v1.

Captura pareada integrada: `_capture_cut_bytes` corre después del catchup y
antes de cerrar el lector de la misma sesión F3. Conserva sesión/corte/checkpoint,
código del colector, bytes de cada raíz y payloads analíticos persistidos: UTF-8
v1 frente a columnas/referencias v2, contando contenidos/secuencias/segmentos
compartidos una vez; funding incluye dependencias métricas transitivas. Sus
consultas usan el plazo SQL ya admitido. No reconstruye documentos financieros
ni convierte igualdad de conteos en prueba de equivalencia. El informe debe
ligar esos bytes a la comparación completa F3; esta captura no declara Q1–Q3 MET.
Pruebas de integración con captura: 14 passed /93,54 s; cobertura/colector
actuales: 6 passed /17,76 s (incluye linaje compartido/transitivo, UTF-8,
tipo escalar desconocido, epoch/root ajenos y DB preservada).

La costura de inserción de cobertura cambió; se mide únicamente esa cobertura
y el colector v2 sobre la foto retenida W2 mediante `recovery-readback
--coverage-only`. No crea un slot ni recibo restore, no vuelve a Drive ni al
lector44; exige head BASELINE_VERIFIED sin reserva, epoch/UUID/manifiesto W2 y
una sola asignación admitida del candidato. Conserva E3 con sus productores y
tiempos originales. Controles de la medición:
`ops45/measure-coverage-W2-20261019`, salida terminal `coverage-phase.json`,
SHA256 `8a310f23643c39b166c8d99063ba3ec08b35b72393706126228a0ff18ad6c5af`.
Terminó 2026-10-19T02:57:33Z: 471,816545 s totales, 458,111186 s cobertura y
13,303567 s colector; mismas 930598 rutas /5907411805 B y 929333 raw/index,
digest `86eab67d…f9fd`. No fue restore ni volvió al lector44/Drive. Payloads
v2 medidos: métricas 1894868400 B /2053921 filas, diagnósticos 5824167 B /2036,
funding 599161667 B /586975 raíces, con referencias/dependencias compartidas.
Son medidas W2, no denominadores pareados F3 ni aceptación final.
Pico cgroup 2686369792 B (RSS 1540993024 B), eventos finales 0/0/0 y sin spill;
el pico supera el umbral Q7 del escenario, no se presenta como Q7 MET.

El perfil durable `resource-run-7a2a1b739c40445086669c1804761b5f/native-settings.json`
registra 3631 INSERT de lotes con 140,457936 s, de 155,208711 s SQL totales.
Corrección acotada: una proyección temporal path/checksum en el directorio de
spill admitido y una carga SQL nativa; cero payloads/modelos hidratados. Una
sola pasada valida orden, digest y toda la contabilidad del manifest además de
comparar cobertura; no elimina checks. La proyección se retira en éxito/fallo,
y comparte el límite global temporal de 1 GB. El reclaimer ya existente del
cgroup propio se habilita también en readback; no cambia R1 ni Q7. Recibo
anterior y productor `25a42821…caee9` conservados. Pruebas nuevas: 8 passed
/23,36 s (incluye inventario alterado, digest/contabilidad, vacío, faltantes,
extras, huérfanos, índice corrupto, una sola lectura y temporal retirado).
Pruebas con plazos SQL: 2 passed /16,84 s (ruta con tres novedades y pérdida
tras appends; subconjunto de los 14, no sumar). Worker de la primera medición:
`resource-run-7a2a1b739c40445086669c1804761b5f`, PID 50345, una DB/1 GB,
v1 PID 283 activo. Comando ejecutado (no repetir tras recibo terminal):

```bash
DC45_OPS=/home/marjuraru/.local/share/investment-analyst/ops/data-chassis-45
DC45_V2=/home/marjuraru/.local/share/investment-analyst/workspaces/data-chassis-43-rehearsal/restore-v2
PYTHONPATH=src:scripts /home/marjuraru/projects/investment-analyst/.venv/bin/python scripts/close_data_chassis.py recovery-readback \
  --coverage-only \
  --source /home/marjuraru/.local/share/investment-analyst/workspaces/default \
  --destination "$DC45_V2" --recovery-root "$DC45_V2" \
  --controls-root "$DC45_OPS/measure-coverage-W2-20261019" \
  --operation-id 3b3a2a56-aec1-4f3b-8bce-ef4e3e6fc0c8 \
  --code-sha 60d9a3f21ba199c136f8dac32b78021d1a714326 \
  --delta-root "$DC45_OPS/physical-delta-W2-20261018" \
  --resource-budget "$DC45_OPS/measure-coverage-W2-20261019/coverage-budget.json" \
  --output "$DC45_OPS/measure-coverage-W2-20261019/coverage-phase.json"
```

Medición de la carga nativa terminada: mismo comando/época/presupuesto y candidato,
con salida distinta `coverage-native-phase.json`; no sobrescribe el recibo
anterior. Worker `resource-run-f7369664fc26481a8d154e617adab8f4`, PID 67487.
Pruebas actuales adicionales: 19 passed /29,70 s (C4 lector/replay original y
coverage-only; hash C4 `39873614…8aa8` idéntico); ruta integrada con tres novedades,
restore/lector44/activación y salud de fixture: 1 passed /10,75 s. No sumar
subconjuntos ni atribuir CI 60d9a3f a estos cambios locales. Siguiente acción:
`coverage-native-phase.json` SHA256
`1366af44ffae0f9e70c7d6bff881dd269ef8919bee6ff6d9c68a9be0b9e58d79`:
304,285007 s totales, 288,075689 s cobertura y 15,729419 s colector; mismo
digest/cobertura y mismos bytes por familia que el recibo anterior. Pico cgroup
1915580416 B, RSS 1527984128 B, eventos 0/0/0; proyección temporal retirada.
64 llamadas SQL /17,220877 s en el perfil durable, frente a 3694 /155,208711 s.
El reclaimer propio completó diez acciones sin errores y sin cambiar límites.
Este escenario aislado queda por debajo de 0,9*MemoryHigh; no sustituye todos
los escenarios Q7 ni la admisión global del corte.

Integración del plan preparado: el coordinador `cut` exige `--prepared-delta`
del W2 y referencia a su manifest en `go.prerequisites`. Antes de parar valida
epoch/UUID/raíz y DB contra el certificado W2. El plan F3 recorre ese inventario
sellado, conservando su hash y procedencia de bloques parent44; DB y archivos
modificados se hashean de nuevo, y los objetos delta W2 sólo se reutilizan por
digest. No reinterpreta bloques delta como parent ni vuelve a generar el W2.
Cada inmutable reutilizado exige coincidencia del testigo y observación fresca
de su identidad; la observación final también suministra tamaño/ctime. La
cobertura comparte el stat seguro de cada archivo sin caché persistente.
Pruebas: 12 passed /28,32 s (plan/restore 0/1/3, fuente padre preservada,
staging inaccesible y ctime alterado antes/después de leer testigo), 3 passed
/6,93 s (root/epoch ajenos y digest con replay del mismo prefijo). Ruta integrada
actual 7 passed /61,23 s: 0/1/3, red, sesión perdida, activación ambigua y epoch
ajeno rechazado con cero stops. El reader C4 histórico mantiene `39873614…8aa8`.
La observación de 304 s conserva su productor `9c3cc081…a7bc`; estas pruebas
adicionales acreditan los cambios de metadatos/plan, sin reetiquetar el recibo.

### Entrada única del corte, después de GO

No ejecutar la siguiente entrada hasta disponer de las ocho cotas, margen,
reserva y presupuestos reales validados. `cut-budget.json` fija el nuevo UUID
una sola vez; no reutiliza F0/F2/W2 ni libera reservas. La ventana y su fuente
quiescente sólo nacen en el worker. GO sigue pendiente: los comandos históricos
de catchup/delta/activate individuales no son una alternativa.

```bash
DC45_OPS=/home/marjuraru/.local/share/investment-analyst/ops/data-chassis-45
DC45_V2=/home/marjuraru/.local/share/investment-analyst/workspaces/data-chassis-43-rehearsal/restore-v2
DC45_CUT="$DC45_OPS/cut-f3-20261019"
DC45_F3=$(/home/marjuraru/projects/investment-analyst/.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["operation_id"])' "$DC45_CUT/cut-budget.json")
PYTHONPATH=src:scripts /home/marjuraru/projects/investment-analyst/.venv/bin/python scripts/close_data_chassis.py cut \
  --source /home/marjuraru/.local/share/investment-analyst/workspaces/default \
  --destination "$DC45_V2" --controls-root "$DC45_CUT" \
  --operation-id "$DC45_F3" --reconciliation-id 3b3a2a56-aec1-4f3b-8bce-ef4e3e6fc0c8 \
  --code-sha 60d9a3f21ba199c136f8dac32b78021d1a714326 \
  --resource-budget "$DC45_CUT/cut-budget.json" \
  --cut-readback-budget "$DC45_CUT/readback-budget.json" \
  --cut-go "$DC45_CUT/cut-go.json" --cut-window "$DC45_CUT/cut-window.json" \
  --prepared-delta "$DC45_OPS/physical-delta-W2-20261018" \
  --delta-root "$DC45_CUT/physical-delta" \
  --parent-index "$DC45_OPS/physical-delta-base-20261015/physical-delta-index.jsonl" \
  --parent-manifest /home/marjuraru/.local/share/investment-analyst/ops/data-chassis-44/drive-restore-staging/remote-backup-manifest.json \
  --parent-manifest-sha256 7817ebd3d21d9d9431809849701e5d6bd3a95e338fb0d2cdf885ee8ad48b43e4 \
  --immutable-parent-proof "$DC45_OPS/recovery-W-20261016/immutable-parent-proof.json" \
  --immutable-link-witness "$DC45_OPS/retire-W2-20261019/unlinks-W2/page-00003627.trailer.json" \
  --immutable-link-journal "$DC45_CUT/links-F3" --reuse-parent-immutables \
  --journal-byte-budget 1000000000 \
  --recovery-root "$DC45_OPS/recovery-W-restore-rehearsal" \
  --transport-config "$DC45_OPS/recovery-W-20261016/drive-transport-20261017.json" \
  --independent-read-root "$DC45_OPS/independent-reads-W-20261017" \
  --output "$DC45_CUT/cut-phase.json"
```

Salida esperada: `HEALTHY_V2` con recuperación del mismo F3 y binding/root/UUID/
release/HTTP coincidentes. Fallo antes de ACTIVATING: ExecStopPost conserva el
progreso, invalida el owner muerto y devuelve v1 saludable dentro del reloj.
Después de ACTIVATING: recuperación compatible v2; nunca arranque automático
del v1 antiguo. Si se pierde el cliente, observar unidad/recibos; no relanzar
`cut` ni matar su worker, y no reiniciar el plazo.

E2 integra la generación v2 real hasta recuperación del mismo epoch, intención,
activación compatible y salud, sin fabricar reconciliación legacy. El controlador
`cut` es MainPID de la unidad transitoria: conserva una sola sesión y un reloj
de 1800 s desde antes de stop; monitor propio independiente de SQL/cliente y
`ExecStopPost` obligatorio para recuperación. La unidad limita el trabajo a
1800 menos la reserva medida; su tiempo de preflight también se descuenta de
la admisión conservadora. Hay un único TimeoutStopSec igual a esa reserva.
No se debe ejecutar catchup/delta/activate manualmente fuera de esa entrada.

Pruebas E2:

- Ruta integrada no vacía 0/1/3 y fallos: **14 passed /92,80 s**. Incluye proceso
  real muerto antes/después de reservar y tras appends confirmados; DB/reservas
  preservadas, SQL/red/host, fuente modificada/faltante/adición inválida, SQL
  interrumpido al consumir la reserva, y pérdida después de ACTIVATING. HTTP
  usa puerto efímero de fixtures; el lector es el release44 instalado real.
- Layout operacional, coordinador y ventana: **112 passed /16,43 s**. Incluye
  pérdida del cliente sin parar la unidad, espera de ControlPID/ExecStopPost y
  nuevo boot que sólo puede invalidar el reloj anterior, sin renovarlo.
- Recuperación de activación interrumpida: cinco casos incluidos en el grupo
  **9 passed /11,47 s** con cuatro negativos de boot; no sumar grupos solapados.

Sólo se simulan fronteras OS/Drive en fixtures; no se paró v1 real para estas
pruebas. Tras ACTIVATING la recuperación sólo avanza sobre el mismo v2; nunca
arranca el v1 antiguo por ausencia de un flag. Una sesión muerta conserva su
reserva y queda invalidada por C1; no se reasignan UUID ni se repite su corte.

Se corrigieron dos conexiones descubiertas por la ruta completa: freeze y
catchup comparten la misma instancia/admisión nativa; el layout operacional
clasifica estrictamente controles C1/v2 ya existentes. En una foto recuperada
comprueba sus bytes locales contra las referencias originales, sin reescribir
paths/UUID del ledger ni convertir la foto en un nuevo owner. Desconocidos,
controles inválidos y referencias corruptas siguen rechazándose.

C2 contenido/raw conservan respectivamente `bf35934d…456bd` y
`dceec7e4…d8cc`; no se repitieron sus verificaciones históricas reales. Los
snapshots de nuevas operaciones incluyen cutover/operational_state además de
los módulos anteriores; los snapshots históricos permanecen intactos.

**Readback E3 acreditado** con `readback-compatible44-budget.json`
(2 GB, dos hilos, una DB). La propuesta
histórica `readback-budget.json` de 1 GB se conserva como antecedente; se detectó
su incompatibilidad antes de abrir datos. No autoriza GO. Las cotas/pares Q1–Q3
del F3, limpieza final, CI del candidato y Q4 permanecen pendientes.

### Antecedente preservado: pausa anterior a E0/E1

**Pausa solicitada por el usuario para apagar (2026-10-18 UTC):** transferencia
W2 detenida por `_stop_owned_worker`, mantenimiento MainPID=0/inactive; ningún
corte nuevo. **Corrección E0:** `ops45/checkpoints/latest-checkpoint.json` no
existe: la revisión automática no pudo completar la autorización de esa copia
por límite de uso y el comando no se ejecutó. La afirmación anterior de haberlo
creado era incorrecta. El código sigue en los cambios locales conservados del
writer; los snapshots realmente existentes están en `resource-run-*/code`.
El cursor durable real es `physical-delta-W2-20261018/upload-checkpoint.json`:
528128/930598 archivos, 0 bloques en ese cursor; no equivale a bytes transferidos.
Está ligado al manifest `082ef346785092572660361dc0d71b99684d50433ad6ec439dcca008e8df22a8`
y files `1854b1891cdaf5a9080dde549e74838f817917371ef3a07457aeaec93974eaef`.
No existen todavía `upload-receipt.json` ni `prepare-W2-20261018/upload-phase.json`.
Tras la limpieza temprana E0, reanudar el comando upload documentado abajo: comprobar
primero si hay upload terminal; si falta, continuar el
mismo upload W2 con el cache existente y sus prefijos, sin replan. El supervisor
registró `worker_exited_without_terminal_receipt` por esta parada deliberada;
no constituye corrupción ni obliga a repetir migración, W o recuperación padre.

Se retoma desde `60d9a3f21ba199c136f8dac32b78021d1a714326`; la CI de ese
SHA está verde, pero la integración adicional del coordinador necesita su propia
validación/publicación. No se repiten migración, restore padre, W financiero ni
el ensayo físico W terminado. El handover que declaraba sólo operación pendiente
queda corregido por los gates vivos y por esta tabla.

| Fase | Artefacto / bindings | Resultado | Reutilización / siguiente acción |
| --- | --- | --- | --- |
| R0/R1 | V1 default y admisión nueva por worker | V1 activo; no corte ni activación nuevos | Capacidad fresca, cgroup 2,5/3 GiB, swap 0, spill agregado 1 GB |
| W recuperación | `recovery-W-integrated-20261017/phase-result.json`; manifest W `3a87c39b…22a7` | Terminal: 560,314826 s; 929762 archivos; 929001 raw↔índice; lector44 real | Reusar cobertura/readback; no repetir restore ni descarga |
| R2/C1 | `recovery-W2-20261017/baseline-certification-phase.json`; generación `3b3a2a56-aec1-4f3b-8bce-ef4e3e6fc0c8` | BASELINE_VERIFIED; certificado `73db6543f785d4d23a73a0c601141a0424f7d981a6d90cbba382d3b412ad9b66`; publicación 17,960772 s | C1 PREPARED y eventos preservados; CAS completo; ningún sucesor reservado |
| R2/C2 relaciones | `recovery-W2-20261017/relationships-verification-phase.json`; DB `fd18cb94…cd0` | Terminal, 430,645211 s; 929333 raw / 530071 observaciones / 2053921 métricas / 2036 diagnósticos | `generation-relationships.json` reutilizable con misma DB, generación y verificador; no repetir grafo |
| R2/C2 contenido | `recovery-W2-20261017/corrective-verification-code-bound-phase.json`; mismo ID y DB | Terminal, 191,782291 s; W⊆W2 completo, añadidos tipados; digests financieros frescos iguales al sello F2 | Reusar `corrective-content-proof.code-bound.json` (`2a330967…97c2`); el recibo anterior queda histórico por el incidente de binding |
| C4/R2 enlaces | `links-verification-phase.json`, journal `links-W` fuera del slot | Terminal, 739,158080 s; 928660 enlaces previos acreditados una vez; trailer `0bcdadb6…da4` | No DB/WAL, copia full ni descarga adicional; reusar por identidad exacta, registrar unlink controlado |
| R2 cobertura raw | `raw-coverage-phase.json`, `generation-raw-coverage.json`; DB W2 y contenido code-bound | Terminal, 422,659451 s; 929333 raw↔archivos; 928043 testigos y 1290 hashes nuevos | Cobertura completa; no sustituida por muestras del lector; incorporada al certificado BASELINE_VERIFIED |
| C4/R2 retiro | `preparatory-retirement-phase.json`; operación `0d3cf604-1f0a-509d-b75b-7724e161feb8`; `unlinks-W` fuera del slot | Terminal RETIRED, 1989,390784 s con v1 activo; 929762 archivos /6066150717 B lógicos cubiertos | Slot W ausente; proof `1effd36a…63ef`; UNLINK `60e5dd21…763e`; preservar controles, reutilizar slot único para W2 |
| C1 reserva huérfana F1 | `orphan-F1-invalidation-20261018/phase-result.json`; intento `17de4bc6-c22b-4ed1-919b-4c3d6525a41c` | SESSION_ENDED durable, 4,365422 s; evento `f763d1be…c2` | Reserva/identificadores preservados; head W2 sin cambios; no repetir invalidación |
| R3 plan físico W2 | `prepare-W2-20261018/plan-phase.json`; epoch W2; `physical-delta-W2-20261018` | Terminal, 615,528156 s; 930598 archivos; 5907411805 B lógicos; 2012899217 B nuevos /3894512588 B padre; files SHA `1854b1891cdaf5a9080dde549e74838f817917371ef3a07457aeaec93974eaef` | Manifest W original conservado; subir sólo objetos sin lectura independiente reutilizable, después recuperar W2 en el slot único |
| R4–R7 | Ventana global y F3 final | NO GO; no stop/corte/activación/retiro | Integrar y probar supervisor; medir todas las cotas antes de parar; luego F3/recovery/44/aceptación |

La invalidación C1 admite ahora la reserva generacional durable que precede a
la publicación del head reservado: exige el certificado BASELINE_VERIFIED del
head actual, dataset, intento, sesión y PID/start/boot/cgroup de su admisión
histórica exactos. Conserva reserva, certificado y head; el evento bloquea el
intento. Rechaza también cualquier binding v2 actual **o anterior**. Validación
actual: 17 casos focalizados pasaron en 3,21 s. Incluyen que el evento pueda
anunciar la siguiente generación: exigir el mismo ID que el certificado anterior
habría impedido esa transición. Esta condición se corrigió y probó antes de
emitir eventos ligados a una reserva v2 real; no se alteró el evento legacy F1 ya
acreditado. Los grupos anteriores se solapan y no se suman. Sólo cambia
la invalidación operacional: las funciones C2 de contenido/raw conservan sus
digests, y la transferencia en curso ejecuta el código físico archivado al
iniciar su worker. Esto no acredita todavía la integración completa del sucesor
F3 ni del supervisor global; ambos siguen siendo condiciones previas al GO.

Los dos nuevos comandos `reconcile-generation` y `verify-generation` son fases
aditivas del coordinador existente. No cambian los comandos ordinarios ni autorizan
usar W/F0/F2 como un corte operativo nuevo. El primero publica sólo PREPARED; el
segundo produce una prueba de contenido, sin elevar por sí solo a BASELINE_VERIFIED.
El proof W2 valida 332 raw, 2389 observaciones, 22392 métricas, 68 diagnósticos,
677 contenidos y 19513 secuencias/segmentos añadidos. La comparación nativa se
particionó por identidad para respetar el spill; la relación raw→observación
preserva el contrato existente que permite source_id derivados distintos.
El grafo se ejecutó en una sola instancia sin ATTACH; errores nativos conservan
su causa original y no quedan ocultos por DROP de temporales en una transacción abortada.

Corrección posterior de trazabilidad: BUILD editó el módulo mientras corría la
comparación de 190,848555 s. Su llamada final a `inspect.getsource` resolvió líneas
desplazadas y produjo `ff398f00…a995`, distinto del verificador actual. Ese recibo
y `corrective-content-proof.json` se conservan como resultado histórico de datos,
pero no acreditan por sí solos el binding de código y no elevan el baseline.
No se cambia su digest. La prueba adicional usa
`corrective-content-proof.code-bound.json` y
`corrective-verification-code-bound-phase.json`, reutilizando el grafo válido.
La prueba adicional terminó en 191,782291 s y la cobertura raw completa en
422,659451 s. El primer recibo no se usa como gate.
La captura del verificador ahora ocurre antes del trabajo y el supervisor conserva
los bytes de código en cada `resource-run-*/code` con `source-bindings.json`.

### Secuencia vigente E0–E6 y reanudación exacta

| Etapa | Estado / recibo | Siguiente acción permitida |
| --- | --- | --- |
| E0 | DONE: `cleanup-E0-20261018/E0-result.json`, SHA `962845e4091b1510b30c37e8f07c5ebd528746fa47b393f2f933f7e27ce8d429`; v1 HTTP200 | Reusar retiros terminales; conservar consumidores declarados |
| E1 | DONE: `prepare-W2-20261018/upload-phase.json`, 294,586381 s; cursor 930598/930598; manifest `082ef346…22a8` | Reusar transferencia, no repetir upload; E2 antes de E3 |
| E2 | Pendiente integración v2 real y supervisor global | Probar con fixtures no vacías antes de E3; no detener v1 |
| E3 | Sin restore/readback W2 terminal | Sólo después de E1/E2: comandos preparados abajo, mismo slot y epoch |
| E4 | NO GO; sin F3 reservado | Ocho cotas medidas + margen/retorno dentro de 1800 s; luego F3/recovery/activación |
| E5 | No elegible aún para v1/W2/cache Drive | Aceptación activa, comparadores y retiros C5 al liberar consumidores |
| E6 | PENDING; CI publicada sólo HEAD anterior | Publicar candidato final, CI exacta, AUDIT y aceptación; Q4 requiere calendario real |

Delta E0/C5: `retire_owned_payload`, modelos tipados y despacho
`retire-owned-payload` en el coordinador existente. Alcance inicial admitido:
objects W antiguo y las cuatro cachés regenerables exactas. W2/cache Drive y
scratch siguen fallando cerrado hasta integrar sus gates finales E5. No DB/WAL
ni raíz amplia. Inventario streaming fuera del payload, pruebas de recuperación
ligadas a epoch/manifest/files/readback44, comprobación de hashes y metadatos,
cursor sealed-directory y reanudación después de detach/borrado/terminal.
Pruebas focalizadas: lifecycle 24 passed /5,56 s (incluye packed y staging);
despacho con retiro real de fixture y rechazo de bindings 3 passed /1,30 s.
No se suman con subconjuntos ni con CI. Ruff y diff-check correctos.
El verificador C4 conserva `39873614…8aa8`; no se reemiten pruebas históricas.

Primer preflight real E0 rechazó `resource_temporary_boundary_mismatch`, antes
de worker/DB/retiro: el presupuesto nuevo conservaba el temp_directory de W2.
Se corrigió sólo esa ruta hacia `cleanup-E0-20261018/resource-temporaries/<op>`.
La operación W conserva ID `51a44815-5b75-56fa-a2e4-282d5495161c` y el input
`cleanup-E0-20261018/W-owned-payload-input.json`; resultado esperado:
`W-owned-payload-phase.json` y receipt/cursor del mismo ID. No contarlo terminal
hasta leer ese resultado.

Resultados reales E0 (todos bajo `cleanup-E0-20261018`):

| Fila C5 | Resultado / consumidor preciso | Recibo / liberación |
| --- | --- | --- |
| W antiguo objects | RETIRED: 2792 objetos, 2156896543 B lógicos /2159882240 B asignados, 21,137269 s | `W-owned-payload-phase.json`; ID `51a44815-5b75-56fa-a2e4-282d5495161c`; controles W intactos |
| backup-v2 packed | RETIRED: 680 archivos, 2245084320 B lógicos, 32,833996 s | `packed-phase.json`; checkpoint `1a30c06c-6400-58e1-bed1-02f9e0fd5ca7`; sin nuevo backup |
| cuatro cachés exactas | RETIRED en este E0; no incluye regeneraciones legítimas de pruebas posteriores | `primary-{pytest,ruff}_cache-phase.json`, `writer45-{pytest,ruff}_cache-phase.json` |
| restore-staging44 | RETAINED: `links-W/header.json` y `unlinks-W/header.json` aún fijan el parent_manifest en esa ruta canónica; E3/F3 consumen esa cadena | Después del último consumidor C4 y de admitir controles retenidos fuera, usar retiro role restore_staging; no duplicar inventario/journal para liberarlo |
| temporales propios abortados | ABSENT en rutas literales de admisiones; sin `.drive-batch-`/`.remote-read-`/`.restore-` inmediatos W/W2/cache | 0 B retirados; nunca directorios resource-run ni sus snapshots |
| target-v2 y complement-b-20261013 | ABSENT | No recrear ni repetir retiros |
| W2 objects y cache Drive | RETAINED: upload W2/retry y recovery W2/F3 pendientes | Tras recibos terminales y último consumidor E4/E5 |
| slot recovery-W-restore-rehearsal | ABSENT por retiro W previo; asignado al ensayo W2 E3 | No repetir retiro W; nueva época requiere su propio journal |
| f-complement y physical-delta-pre-f | RETAINED: cobertura de únicos/controles para F3 aún pendiente | Resolver con recuperación final F3 y pruebas de cada único antes de E5 |
| default v1 | RETAINED: servicio activo y origen de F3/comparadores | Sólo E5 tras activación y recuperación del mismo F3 |

El primer preclean packed se detuvo sin mutar por un falso consumidor: el
supervisor tenía `--retire-root` en su cmdline. La repetición diagnosticada
usó la misma `main(argv)` del coordinador con argumentos internos (supervisor
sin descriptor del backup); la unidad y detección de lectores reales quedaron
activas. No se cambió el detector ni se ignoró un lector. Los controles packed
se conservaron en restore-staging44, donde ya existían byte-exactos; no se
copiaron los 421 MB de inventario/journal. El checkpoint packed pequeño sí se
conservó como prueba del retiro, mediante el método existente.

Fila releases C5: método aditivo `LocalReleaseService.retire_unused_release`,
reutiliza `SealedDirectoryRetirementCheckpoint/Receipt` y preserva el manifest
original fuera del runtime. Comprueba current/previous/bindings/unidad/symlink,
lectores y referencias de recuperación; código contra el mirror ya existente,
sin fetch ni copia full. Rechaza cambios y archivos desconocidos. Pruebas
focalizadas nuevas: 14 passed /1,46 s; incluye lector real, binding actual y
anterior, release44 protegido e interrupción con archivos nuevos. El grupo de
31 passed /1,60 s incluye las 17 pruebas owned-payload: no sumarlo con ellas.
Inventario de candidatos en `release-candidates.json`: 22 sin consumidores y
3 protegidos (current0e93…, previouse10c…, compatible44dbbdfd…). Cada retiro
produce `release-<SHA>-phase.json`, inventario, cursor, manifest pequeño y recibo.
Los nuevos snapshots por worker incluyen también el módulo local_release;
no cambia la semántica de las pruebas C2 ni reescribe certificados W2 anteriores.

Los bytes de inventarios son lógicos/asignados Linux, no ahorro físico de C:.
Los releases comparten hardlinks; no sumar sus asignaciones como espacio liberado.

Comando E1 después de terminar E0; reutilizar un terminal exacto si ya existe:

```bash
IA45_OPS=/home/marjuraru/.local/share/investment-analyst/ops/data-chassis-45
IA45_SOURCE=/home/marjuraru/.local/share/investment-analyst/workspaces/default
IA45_DEST=/home/marjuraru/.local/share/investment-analyst/workspaces/data-chassis-43-rehearsal/restore-v2
IA45_PY=/home/marjuraru/projects/investment-analyst/.venv/bin/python
PYTHONPATH=src:scripts "$IA45_PY" scripts/close_data_chassis.py prepare-delta \
  --delta-action upload --source "$IA45_SOURCE" --destination "$IA45_DEST" \
  --controls-root "$IA45_OPS/prepare-W2-20261018" \
  --operation-id 3b3a2a56-aec1-4f3b-8bce-ef4e3e6fc0c8 \
  --reconciliation-id 3b3a2a56-aec1-4f3b-8bce-ef4e3e6fc0c8 \
  --code-sha 60d9a3f21ba199c136f8dac32b78021d1a714326 \
  --resource-budget "$IA45_OPS/prepare-W2-20261018/plan-budget.json" \
  --delta-root "$IA45_OPS/physical-delta-W2-20261018" \
  --parent-manifest-sha256 7817ebd3d21d9d9431809849701e5d6bd3a95e338fb0d2cdf885ee8ad48b43e4 \
  --transport-config "$IA45_OPS/recovery-W-20261016/drive-transport-20261017.json" \
  --independent-read-root "$IA45_OPS/independent-reads-W-20261017" \
  --output "$IA45_OPS/prepare-W2-20261018/upload-phase.json"
```

### Continuación histórica R3 W2, una única ruta y slot

`prepare-W2-20261018/plan-phase.json` es terminal: no ejecutar de nuevo plan.
La transferencia iniciada usa `physical-delta-W2-20261018`, el mismo config de
Drive y `independent-reads-W-20261017`; se reutilizan sólo objetos cuya lectura
independiente todavía acredita identidad, longitud, namespace y hash local.
`upload-phase.json` debe ser terminal antes de la recuperación dependiente.

El path del slot retirado se asigna ahora a la **nueva época W2**. Esto no
reconstruye la foto antigua W: los manifests y recibos W quedan intactos fuera
del slot. C4 encadena UNLINK W → LINK W2 para los inodes ya acreditados usando
el inventario W original. Cada miembro debe coincidir también por path/hash/
longitud con W2. Los demás archivos se materializan por rangos del padre o
lecturas reales de Drive; DB/WAL nunca se enlazan. El recibo W2 referencia el
journal nuevo; el campo se omite si está vacío para preservar la serialización
exacta de los recibos anteriores. Verificador C4 y digest serializado del
recibo W permanecen `39873614…8aa8` y `7c5062e7…0990` respectivamente.

Comandos dependientes preparados, **todavía no ejecutados** al registrar esta
sección. Si existe un resultado terminal, leerlo y reutilizarlo; no repetir por
rutina. Una interrupción se reanuda con las mismas entradas y journal, sin
eliminar reservas, páginas ni prefijos confirmados.

```bash
IA45_OPS=/home/marjuraru/.local/share/investment-analyst/ops/data-chassis-45
IA45_V2=/home/marjuraru/.local/share/investment-analyst/workspaces/data-chassis-43-rehearsal/restore-v2
IA45_PY=/home/marjuraru/projects/investment-analyst/.venv/bin/python
PYTHONPATH=src:scripts "$IA45_PY" scripts/close_data_chassis.py prepare-delta \
  --delta-action restore \
  --source /home/marjuraru/.local/share/investment-analyst/workspaces/default \
  --destination "$IA45_V2" --controls-root "$IA45_OPS/prepare-W2-20261018" \
  --operation-id 3b3a2a56-aec1-4f3b-8bce-ef4e3e6fc0c8 \
  --reconciliation-id 3b3a2a56-aec1-4f3b-8bce-ef4e3e6fc0c8 \
  --code-sha 60d9a3f21ba199c136f8dac32b78021d1a714326 \
  --resource-budget "$IA45_OPS/prepare-W2-20261018/restore-budget.json" \
  --delta-root "$IA45_OPS/physical-delta-W2-20261018" \
  --parent-index "$IA45_OPS/physical-delta-base-20261015/physical-delta-index.jsonl" \
  --parent-manifest-sha256 7817ebd3d21d9d9431809849701e5d6bd3a95e338fb0d2cdf885ee8ad48b43e4 \
  --recovery-root "$IA45_OPS/recovery-W-restore-rehearsal" \
  --reuse-parent-immutables \
  --immutable-link-witness "$IA45_OPS/recovery-W2-20261017/unlinks-W/page-00003627.trailer.json" \
  --immutable-link-journal "$IA45_OPS/prepare-W2-20261018/links-W2" \
  --journal-byte-budget 1000000000 \
  --transport-config "$IA45_OPS/recovery-W-20261016/drive-transport-20261017.json" \
  --independent-read-root "$IA45_OPS/independent-reads-W-20261017" \
  --output "$IA45_OPS/prepare-W2-20261018/restore-phase.json"

PYTHONPATH=src:scripts "$IA45_PY" scripts/close_data_chassis.py recovery-readback \
  --source /home/marjuraru/.local/share/investment-analyst/workspaces/default \
  --destination "$IA45_V2" --controls-root "$IA45_OPS/prepare-W2-20261018" \
  --operation-id 3b3a2a56-aec1-4f3b-8bce-ef4e3e6fc0c8 \
  --code-sha 60d9a3f21ba199c136f8dac32b78021d1a714326 \
  --resource-budget "$IA45_OPS/prepare-W2-20261018/readback-compatible44-budget.json" \
  --delta-root "$IA45_OPS/physical-delta-W2-20261018" \
  --recovery-root "$IA45_OPS/recovery-W-restore-rehearsal" \
  --recovery-receipt "$IA45_OPS/physical-delta-W2-20261018/restore-receipt.json" \
  --output "$IA45_OPS/prepare-W2-20261018/readback-phase.json"
```

El primer resultado acredita materialización íntegra y cadena de inodes; el
segundo añade cobertura completa raw↔índice y lectura compatible44 con una sola
instancia admitida (2 GB, dos hilos, spill agregado ≤1 GB). Ninguno activa v2.
No editar el coordinador ni el módulo ejecutado durante una fase real; esperar su
recibo terminal. El caso `source_capture` reproduce la desviación de líneas a mitad
de ejecución. Esta corrección no justifica repetir migración, W financiero, padre
remoto, restore W ni el journal ya emitido.
Sus intents operacionales inmutables permiten reanudar la misma generación tras
pérdida de sesión. Prueba focalizada de esta integración: 28 passed en 10,10 s;
Ruff y diff-check correctos. La suite completa no se repite localmente.

Pruebas del retiro preparatorio: 16 casos pasaron en 16,55 s, incluidos
interrupción antes del trailer de unlink, después de renombrar, durante/después
del borrado y antes del recibo terminal. Se corrigió el contexto de locks para
no traducir un error del cuerpo en un falso fallo de adquisición. Archivos
únicos, cambios desconocidos de ctime, DB enlazada, writer activo, namespace
distinto y reserva sucesora impiden el retiro. Ningún retiro real ejecutado
al registrar estas pruebas. El primer intento real falló antes de mutar porque
el consumidor usó `canonical_json_bytes` donde el productor del readback usa
`restore.model_dump_json().encode()`. Se corrigió únicamente el consumidor y
su fixture, sin reescribir hashes históricos. Tras la corrección, lifecycle y
coordinador completos pasaron: 99 tests en 30,71 s. Los dos casos nuevos de plan
con testigo UNLINK pasaron en 5,61 s: reutilización sin releer el payload y
rechazo de ctime desconocido. Estos grupos no son CI ni se suman con subconjuntos.

Revisión focalizada del futuro catch-up: se retiró la aceptación de un hash
fuente `NULL` en el fast path raw. Reutilizar un miembro exige ahora hash
FUENTE real igual al checksum y metadatos fuente válidos. `document_json` es
NOT NULL en el schema v1 vigente: no se altera ese contrato ni se crean filas
NULL para forzar una prueba. Los casos focalizados 0/1/3 pasaron (3 tests en 11,86 s).
La semántica afectada es sólo el fast path de
`_append_recovered_raw` para el próximo corte; el retiro activo no ejecuta esa
función. C2 contenido/grafo/raw y C4 permanecen ligados a sus archivos de código
archivados; no se cambian sus hashes ni se repiten sus pruebas reales por esto.

El retiro terminó el **2026-10-18T03:39:33.230909Z**. Su duración de 1989,390784 s
fue preparatoria, con v1 activo; no consumió ni amplió una ventana de corte.
SHA del proof: `1effd36ab04807c6157a9dd8f3b86604b406be8421819866ad10d87667f763ef`.
Testigo UNLINK terminal:
`unlinks-W/page-00003627.trailer.json`, SHA
`60e5dd21f50ac6c0799b22c6b9602d98353dfc037449345237111b5bf684763e`.
Se confirmaron los 928660 unlinks sin borrar el contenido retenido. El slot no
existe y el checkpoint es RETIRED. Se escribieron 598286947 B de controles/syscalls;
los 6066150717 B son cobertura lógica, **no** una medida de ahorro C:.
Probe posterior: v1 active/running PID319; overview y candidate-notifications HTTP200;
mantenimiento inactive/MainPID0. Última capacidad C: ~44,9 GB, spill0 y eventos0.
No repetir esta fase; los comandos siguientes se conservan para trazabilidad.

### Comando vigente del retiro preparatorio

Desde el worktree `data-chassis-45`, sólo para la operación ya admitida abajo.
El input tipado fija las doce referencias verificadas, el certificado W2,
el manifest W y el locator remoto; no reconstruirlo para variar bindings.
El slot se obtiene de ese input, no de un argumento ambiguo de limpieza.
La reanudación conserva intent, páginas y checkpoint; no borrar controles si falla.

```bash
DC45_OPS=/home/marjuraru/.local/share/investment-analyst/ops/data-chassis-45
DC45_CTRL="$DC45_OPS/recovery-W2-20261017"
PYTHONPATH=src:scripts /home/marjuraru/projects/investment-analyst/.venv/bin/python scripts/close_data_chassis.py retire-preparatory \
  --source /home/marjuraru/.local/share/investment-analyst/workspaces/default \
  --destination /home/marjuraru/.local/share/investment-analyst/workspaces/data-chassis-43-rehearsal/restore-v2 \
  --controls-root "$DC45_CTRL" \
  --operation-id 0d3cf604-1f0a-509d-b75b-7724e161feb8 \
  --reconciliation-id 3b3a2a56-aec1-4f3b-8bce-ef4e3e6fc0c8 \
  --code-sha 60d9a3f21ba199c136f8dac32b78021d1a714326 \
  --resource-budget "$DC45_CTRL/retirement-budget.json" \
  --preparatory-retirement-input "$DC45_CTRL/preparatory-retirement-input.json" \
  --immutable-link-journal "$DC45_CTRL/unlinks-W" \
  --journal-byte-budget 1000000000 \
  --output "$DC45_CTRL/preparatory-retirement-phase.json"
```

Resultado esperado: recibo `preparatory-retirement-proof.json` con `phase=retired`,
checkpoint RETIRED, journal UNLINK terminal fuera del slot y v1 aún activo.
`APPLYING` se conserva en la evidencia W y no autoriza activación. Tras recibo
terminal no volver a ejecutar la fase con el mismo `--output` exclusivo; reutilizar
el recibo. Ante fallo anterior al terminal, diagnosticar el `resource-run` exacto
y reanudar sus controles, sin aumentar límites ni borrar reservas.

## Enmienda y estado histórico anterior (2026-10-17, 17:15 UTC)

El cuerpo canónico actualizado de #345 autoriza C1 (invalidaciones y head
generacional), C2 (certificación correctiva W2), C3 (raw grande por repositorios)
y C4 (journal link/unlink y retiro preparatorio), más la ventana global.
Las fronteras PLAN descritas en la historia siguiente quedan resueltas por esa
enmienda; sus diagnósticos y recibos no se reescriben. Manifest vigente:
`41a920207764ea8c748a9315ebbffa40abe98467b7001d0ebbaf9b891a01ad77`.

| Fase | Artefacto / bindings | Resultado actual | Reutilización | Siguiente acción |
| --- | --- | --- | --- | --- |
| R0 | B/F0/W/F2 y expediente `resume-review-20261017T165543Z` | Preservados; v1 activo PID 538092 | Hechos W con sus pruebas; F2 como progreso no acreditado íntegramente | Implementar/probar la enmienda sin parar v1 |
| R1/C3 | Helpers aditivos raw/serialización y migrador | Spool byte-exact, I/O <=1 MiB; raw grande solo, heap del parser presupuestado | Contratos ordinarios intactos; pruebas pequeñas actuales | Completar negativos/adopción y recursos |
| C1/C2 | Companions v2, invalidación y head CAS | Implementación/pruebas en curso; ningún head/evento real emitido | Certificados W históricos y filas F2, sin inferir equivalencia fuente F2 | Cadena completa, evidencia correctiva y nueva generación W2 bajo R1 |
| C4/R3 | Restore W y objetos remotos exactos existentes | Se conserva slot W; no retirada ejecutada | Padre44, manifest/restore W, lecturas independientes válidas | Integrar cobertura/readback44, testigo de enlaces y retiro seguro |
| R4 | Ventana global stop→health 1800 s y GO medido | Persistencia/deadline/reserva probados en fixtures; integración pendiente | Cotas reales anteriores como observaciones, no GO | Integrar supervisión/recuperación; medir ruta crítica con v1 activo |
| R5–R7 | F3 nuevo, recovery/activación44, retiros/CI/aceptación | No ejecutados; Q4 no iniciado | Release44 preparado y pruebas compatibles exactas | Sólo después de GO; no nueva parada diagnóstica |

La tabla de cotas GO debe incluir freeze, comparación/delta, operacional,
plan físico, red/controles, restore/apertura, activación/salud y reserva de
cancelación+cierre+reinicio. Ningún reinicio de comando reinicia el reloj; no
se admite una cota desconocida ni se aumenta el límite al fallar. Los comandos
del handover F2 permanecen históricos y no constituyen receta ejecutable.

## Revisión histórica del handover F2 (2026-10-17, 16:56 UTC)

La revisión del handover encontró F2 `ff244b14-2cc5-4003-b788-ed5efba68d63`
en `catchup_complete`, pero sin delta ni recuperación independiente de ese
epoch. El servicio estaba detenido desde 12:53:38 UTC. R4 obliga a recuperar
servicio al exceder la ventana: v1 se arrancó normalmente y se acreditó HTTP
200 en `/api/v1/overview` y `/api/v1/candidate-notifications` a las
16:56:25.124636 UTC, PID 538092. No hubo activación v2. F2 conserva sus datos y
recibos, pero su quiescencia operacional no autoriza una activación posterior.

Expediente durable: `ops45/resume-review-20261017T165543Z`. Contiene el código
recibido, handover original, referencias SHA y recibos de decisión y salud.
Detalle y límites: [revisión del handover F2](incidents/data_chassis_45_wsl_spill_20261016.md#revisión-del-handover-f2-2026-10-17).

| Fase / artefacto | Bindings | Resultado observado | Reutilización permitida | Siguiente acción |
| --- | --- | --- | --- | --- |
| W físico: manifest y restore | Epoch `241e88e6-4218-427c-98f0-b988d95b9d88`; manifest `3a87c39b…22a7` | Restore terminal 07:47:45 UTC, 929762 archivos, 2051,30 s; readback 44 registrado 07:48:49 UTC | Imagen W y objetos verificados por identidad; la muestra de lectura no sustituye cobertura raw completa | Conservar manifest y slot; cerrar integración/cobertura y frontera de ctime sin repetir restore padre |
| F2: `cut-f2-20261017/f2-catchup-phase.json` | Epoch F2, source fingerprint `e1ddcd32…bc8a`; tool `b8eb6b28…0eac` archivado | Exit 0 y `catchup_complete`; 1756,41 s sólo en la última ejecución; hubo intentos anteriores en la misma parada | Filas y artefactos preservados como progreso; los bypass descritos impiden inferir equivalencia completa | Evidencia adicional de las comparaciones afectadas, sin cambiar hashes históricos ni repetir migración base |
| F2: recuperación/activación | Falta manifest/delta/restore del mismo F2 | No acreditadas; restore W permanece APPLYING y otro epoch | Release 44 preparado; padre remoto; bloques W cuyo digest coincida | No ejecutar el comando activate del handover con recibo W |
| Servicio | Release `0e93c6dd…302f`, raíz `workspaces/default`, sin binding v2 | V1 recuperado con salud HTTP 200; mantenimiento inactivo | Operación ordinaria v1 | Diseñar/probar con v1 activo; ninguna parada hasta GO verificable |
| Contratos y próxima reserva | Reconciliación W actualmente SUCCESSOR_BOUND a F2; sesión terminada; F2 operacional invalidado | Falta transición autorizada para preservar este antecedente y reservar exclusivamente un nuevo corte | B/F0/W/F2, escrituras e identidades se conservan | PLAN debe concretar invalidación/sucesión y evidencia correctiva antes de otro corte |

Corrección local: retirados los hooks que reemplazaban hashes fuente por hashes
W, alteraban validación del fingerprint, reutilizaban `source_session`,
sobrescribían `progress-core-comparison` o evitaban `execute_bounded`. Se restauró
`RuntimeMaxSec=1800`; esto recupera el límite por proceso, **no implementa todavía
el plazo global stop→health**. Prueba focalizada actual: 46 passed en 7,81 s en
`tests/integration/application/test_data_chassis_closure_cli.py`; Ruff correcto.
No CI ni aceptación del candidato local por esos resultados.

La frontera PLAN de sesión que figura abajo ahora debe cubrir explícitamente
un sucesor ya sellado antes de activar: invalidar vigencia operacional sin
reescribir COMPLETE ni liberar exclusividad de forma implícita; preservar
reservas/recibos y autorizar una sola generación nueva con evidencia de los
appends. También siguen pendientes la allowlist raw >1 MiB y la representación
de ctime por enlaces/retiro del slot. No se inventan en el coordinador.

## Checkpoint histórico W anterior al handover (2026-10-17)

La tabla siguiente describe el checkpoint anterior al handover F2; para operar
manda la sección vigente anterior. W no es F1. F0 permanece APPLYING; se conservan B, membresía B,
certificados y recibos. Todos los paths de recibos parten de
`~/.local/share/investment-analyst/ops/data-chassis-45/recovery-W-20261016`,
salvo donde se indica otra raíz. Ninguna fila autoriza reescribir evidencia vieja.

| Fase / artefacto | Bindings | Resultado | Reutilización permitida | Siguiente acción |
| --- | --- | --- | --- | --- |
| R0: checkpoints y antecedente F0 | F0 `1dec60f8-5091-4e1f-a18a-660443f12bf5`; membresía B `99e666f4…4885a` | Preservados; F0 incompleto | Procedencia y pertenencia B; nunca quiescencia actual | Conservar; no reanudar F0 |
| R1: admisión y supervisión | Presupuesto agregado, cgroup propio, host fresco, recibo por ejecución | Aplicados en ejecuciones reales; dos paradas por memory.high diagnosticadas | Código/pruebas compatibles; capacidad y admisión se renuevan | Admitir cada nueva operación antes de I/O pesado |
| R2: `wal-recovery-phase.json` | Reconciliación `241e88e6-4218-427c-98f0-b988d95b9d88`; DB W `e5557a41…914e` | Replay terminado, 10,75 s; WAL cerrado | DB recuperada y recibo | No repetir replay ni migración base |
| R2/R3: `financial-baseline-memory-composition-phase.json` | Certificado `ee03429b…c50`; B⊆W, código/entorno archivados | BASELINE_VERIFIED; 2031529 métricas, 1968 diagnósticos; 9276,61 s | Hechos financieros exactos y compatibles de W | Comparar W⊆F1; no revalidar íntegramente W por rutina |
| R3: `relationships-bucketed-phase.json` | DB W, B, política y verificador | Cobertura/relaciones/PIT/ciclos completos; 415,17 s | Misma imagen y semántica de verificador | Invalidar selectivamente sólo invariantes afectados |
| Core: `core-baseline-phase-20261017.json` | Prueba `b769e74c…88f`; DB W, metadatos raw y digest observaciones | 929001 raw / 527682 observaciones; 13,99 s | Core acreditado, sin reabrir todos los payloads | Importar sólo novedades con validación tipada |
| Padre: `immutable-parent-phase-20261017.json` | Restore 44, padre `6c69b9df-d530-5502-8c95-5c9b756eec86`, índice/manifest, verificador | Prueba emitida; 21,92 s | Blobs inmutables con identidad/tamaño/ctime acreditados; nunca DB/WAL | Código físico cambió: conservar prueba vieja y emitir evidencia nueva compatible, sin adaptar hashes |
| Delta W: `physical-delta-W-phase-20261017.json` | Manifest sellado en `ops45/physical-delta-W-20261017`; files SHA `491f0f56…d94`; epoch de reconciliación W | **405,15 s**, 929762 archivos, 2156958321 B nuevos / 3909192396 B reutilizados; APPLYING | Foto W y objetos exactos por digest; no aceptación de F1 | Transferir y recuperar independientemente sin regenerar manifest |
| R4: F1 | Nueva sesión/quiescencia y sucesor exclusivo | No reservado; v1 activo | W como progreso, no quiescencia F0 | Resolver pérdida de sesión, raw grande, recuperación y plazo global antes de parar |
| R5: restore/activación | Mismo F1 terminal y recuperable; release 44 `dbbdfd02…1067` | Pendiente | Release preparado y padre ya acreditado | Ensayo W con v1 activo; después F1 y activación real |
| R6/R7 | Recibos de recuperación/activación, comparaciones y SHA publicado | Pendientes; Q4 no iniciado | Retiros ya probados, sin repetirlos | Retirar redundancias al cumplir gates; CI del candidato final; siete días UTC tras activación |

`ops45` significa `~/.local/share/investment-analyst/ops/data-chassis-45`.
Los digests abreviados son localizadores humanos; las comparaciones usan los
SHA completos de los artefactos. El último checkpoint de preparación precede
al éxito físico: el recibo final de 405,15 s es la evidencia de esa ejecución.
No repetir los 676 volúmenes, compresión, restore padre ni preparación de release 44.
El informe `docs/incidents/data_chassis_45_planner_handoff_20261017.md` se preserva
fuera del commit por no pertenecer a la allowlist. La operación se documenta aquí.
BUILD actualiza su único marker existente; archive-first pertenece al AUDIT histórico.

## Registro histórico de reanudación (2026-10-16)

Los estados PREPARED y pendientes de esta sección describen aquel momento;
para reanudar se usa exclusivamente la tabla anterior junto a los artefactos vivos.

- R0: incidente versionado, controles y código anterior archivados; marker BUILD `PENDING`
  con manifest consolidado `c5396622b50919a3cfb06abce49db0edcb29709db48076179bcb1e2fcc0110ec`.
- R1: pruebas focalizadas y admisión real supervisada. El presupuesto entra en
  `duckdb.connect(config=...)` antes de WAL; cgroup 2,5/3 GiB, swap 0, spill agregado
  1 GB, fuente cerrada en el worker de replay y supervisor externo a SQL.
  Las primeras pruebas de readback del setting resultaron insuficientes: véase
  el hallazgo posterior en el incidente. La apertura corregida activa el gestor
  nativo en un motor vacío antes de adjuntar el archivo real. Las fixtures de
  presión verifican cuotas 0/1000000 B; WAL, cursores y lector 44 real pasan.
  Los recibos anteriores se conservan sin reinterpretarlos como R1 terminal.
- R2 físico: replay completado `2026-10-16T17:52:19.955990Z`, operación
  `241e88e6-4218-427c-98f0-b988d95b9d88`. Recibo durable
  `ops/data-chassis-45/recovery-W-20261016/wal-recovery-phase.json`.
  Tiempo 10,75 s; RSS máximo 836952064 B; última muestra cgroup 2476834816 B,
  spill observado 0, eventos de memoria `[0,0,0]`; C: 71618908160 B libres.
  DB: 2485661696 → 2459185152 B. WAL previo 7437337 B aplicado, sin WAL al cerrar.
  La generación inmutable del certificado tiene SHA-256
  `abb98e53f79c9f733a287b6ba148a96b48ef87a953171b21d739679bf5bde79a`.
  Conserva hashes F0, membresía B, código anterior y cada intento/preimagen.
  Estado **PREPARED**, sin inventario W acreditado ni sucesor reservado.
- R2 semántico pendiente: validar B⊆W y contenido financiero antes de publicar
  `BASELINE_VERIFIED`. El escaneo financiero usa hashes del padre original sólo
  para IDs acreditados, reconstrucción tipada para novedades o representaciones
  no canónicas, páginas ≤256 y caché de fragmentos ≤1 MiB. Cada página conserva
  cursor, digest y offset/checksum de W\B; una cola escrita antes del checkpoint
  sólo se reutiliza si coincide exactamente, sin truncar historia.
- R3 relaciones/PIT/ciclos: prueba completa real terminada
  `2026-10-16T20:36:11.829779Z`, recibo
  `recovery-W-20261016/relationships-bucketed-phase.json` bajo ops45.
  Duración 415,17 s; RSS máximo 1283727360 B; escrituras físicas 1671168 B.
  Cubre 929001 raw, 527682 observaciones, 2031529 métricas y 1968 diagnósticos,
  DB SHA-256 `e5557a41790037eb28e820bf6fc14c00dc5882875705d8877b9ec05f3fad914e`.
  Se reutiliza sólo con esa imagen física, membresía B, política y hashes de
  verificador/recursos idénticos. No acredita por sí sola contenido financiero.
  El perfil real por joins separados terminó en 7,76 s, con 42319 B escritos:
  `recovery-W-20261016/relationships-cardinality-staged-phase.json` bajo ops45.
  689882 secuencias/segmentos de dependencias, 546210 aristas métrica→secuencia,
  689882 secuencia→segmento y 958086 segmento→métrica de disponibilidad igual.
  El mayor grupo entre 16 por disponibilidad contiene 66617 aristas métricas.
  Este recibo es `profile_only`, no una prueba de ciclos ni aceptación de W.
- R4–R7 pendientes; Q4 todavía no empieza. v1 sigue activo, PID 291 en la
  comprobación posterior al replay. No reanudar F0 ni invocar `catchup` con sus bindings.

Los párrafos históricos de checkpoint/spill al final describen el estado anterior
a la compactación administrativa y a este replay; sus cifras no son capacidad actual.

Receta vigente de #345. Los runbooks de 43/44 y sus recibos son evidencia histórica enlazada;
no se repiten migración A→B, compresión, descarga de 676 volúmenes ni restore base.
El Issue canónico, AGENTS y los guards vivos conservan autoridad. Este documento no concede
permisos ni convierte un checkpoint, fixture o upload en aceptación operacional.

## Raíces y artefactos conservados

| Función | Identidad o localizador |
| --- | --- |
| Fuente v1 | `~/.local/share/investment-analyst/workspaces/default`, UUID `19cf83b5-8df3-4663-9369-4b61f86eceea` |
| Candidato elegido | `~/.local/share/investment-analyst/workspaces/data-chassis-43-rehearsal/restore-v2`, UUID `9ffeb53b-4342-4b4a-82d4-16c83d517d7b` |
| Padre restaurado de Drive | backup `6c69b9df-d530-5502-8c95-5c9b756eec86`, 676 volúmenes |
| Manifest padre reconciliado | SHA-256 `7817ebd3d21d9d9431809849701e5d6bd3a95e338fb0d2cdf885ee8ad48b43e4` |
| Índice padre | `ops/data-chassis-45/physical-delta-base-20261015/physical-delta-index.jsonl` |
| Controles pre-F verificados | `ops/data-chassis-45/physical-delta-pre-f-20261015` |
| Release integrado preparado | `dbbdfd021b0fee20be31b2d2a845874b0e861067`, tree `6fb34f0d4e72ea5b03ce775e6cb8902a87e4fd06` |
| F0 histórico incompleto | operación `1dec60f8-5091-4e1f-a18a-660443f12bf5`, congelado `2026-10-15T21:03:22.272057Z`; permanece `APPLYING` |
| Controles F | `ops/data-chassis-45/cut-f-20261015` |

Los paths relativos de operaciones parten de `~/.local/share/investment-analyst`.
El receipt `drive-restore-20261014.json` termina con `downloaded_volumes=0` por reanudación:
enlazar también journals/checkpoints/logs originales de 44 y manifest-reconciliation;
no atribuirle una descarga completa en su última invocación.
Custodia externa de ambas claves crypt confirmada por el usuario el 2026-10-15;
no imprimir configuración OAuth, password, salt ni contenido de `service.env`.

Mediciones del mismo F, conservadas antes de retirar v1:

- `cut-f-20261015/source-F-payload-byte-aggregates.json`: 929001 raw, 527682 observaciones,
  2031529 métricas y 1968 diagnósticos. Las métricas ocupan 7105157755 B de `document_json`
  UTF-8. El recibo declara por separado familia/activo, tiempos y código; no mide JSON
  reconstruido ni acredita ahorro v2. SHA-256
  `e66343fcdd90534a05881290836667d467f3466132fcb2d4221abd31a044e790`.
- `cut-f-20261015/source-lifecycle-inventory.json`: 929753 archivos y 14204991816 B
  lógicos; 929708 recuperables, 45 históricos operacionales y cero desconocidos.
  Es cobertura de metadatos/rutas, pendiente de equivalencia y recuperación F completas.
  SHA-256 `ba44d238d1bb25d9af67c8cac3b9d35f0745313a1d928abad698fa96ef45e8d5`.
- `f-complement-20261015/local-complement-manifest.json`: 45 archivos, 55978117 B.
  Conserva manifest v1, estado y eventos procesados. SHA-256 del archivo
  `cbb2de78345162bd93c02f1eae04aa39bd130fe8e8df8bf8d7b071e04faafc56`.
  `.git`, `.agents`, `.codex` y `exports` del origen se observaron vacíos; se revalida su
  clasificación antes del retiro. Preservar este complemento no autoriza borrar v1.

## Secuencia y reanudación

1. **preclean:** usar lifecycle para cada raíz exacta probada; conservar contenido único,
   controles y recibos fuera de la raíz. `target-v2` y complemento B ya retirados se reutilizan
   como evidencia, no se recrean. Packed/staging mantienen dependencias explícitas hasta retiro.
2. **prepare:** reutilizar release 44 e índice sellado. Verificar capacidad del host por API:
   mínimo `max(25000000000, 5000000000 + crecimiento de la siguiente unidad)` bytes.
   `df` y asignación Linux se registran aparte. Ningún full backup nuevo.
3. **catchup:** detener normalmente el único servicio antes de crear F. Mantener fuente y
   candidato cerrados bajo locks existentes. La sesión quiescente comparte fingerprint e
   inspección; tras crash verifica otra vez lo necesario y adopta sólo el mismo F inmutable.
   No borrar checkpoints A/B ni usar la fecha máxima como pertenencia al padre.
4. **delta:** después de terminal financiero y operacional, preservar byte-exact los recibos
   F dentro de `state` mediante `preserve_recovery_controls`. Planificar bloques ≤8 MiB con
   buffers ≤1 MiB y cursor durable; objetos y controles nuevos por digest. Legacy sigue legible
   y no se sobrescribe. Upload y lectura independiente de novedades son pasos distintos.
5. **recovery:** resolver rangos padre contra su índice verificado y descargar de Drive todos
   los bloques nuevos. Materializar DuckDB en un único slot temporal reservado; comprobar
   hashes/cobertura y abrir realmente mediante Python/src del release 44. El smoke de lectura
   acotado declara su muestra; la equivalencia completa proviene del F pareado y su foto física.
   Retirar el slot por lifecycle con receipt `kind=restore` ligado a su raíz y proof persistido.
6. **activate:** `LocalReleaseService.activate` sobre el release integrado 44 y la misma raíz
   v2 elegida. Conservar binding, unidad cargada, PID/starttime/boot/cgroup, HTTP UUID y salud.
   Si falla el primer arranque v2, detener y recuperar datos actuales con código compatible;
   no reabrir automáticamente el v1 congelado. No instalar candidato 45 antes de merge.
7. **report:** pares Q1–Q3 del mismo F antes de retirar v1, continuidad/no-change por familia,
   identidad de runtime y escenarios de memoria. Reutilizar colectores existentes una vez por
   escenario; separar ejecución de colector y pico de escenario de pico de vida.
8. **retire-final:** recuperación F, v2 activo, Q1–Q3 pareados y gates funcionales completos;
   clasificar cada archivo v1, incluidos `.git/.agents/.codex`, y preservar lo único fuera.
   Raíz ambigua, contenido desconocido o writer activo impiden retiro. No esperar siete días
   para limpiezas ya probadas. Retiros se hacen exclusivamente mediante servicios tipados.

Si se reabre v1 durante mantenimiento, invalidar F operacional y crear otro epoch al próximo
corte; no descartar progreso financiero compatible. No dejar el servicio parado esperando CI
o calendario. Después de escritura nativa v2, rollback ordinario conserva esa raíz v2 actual.

## Herramienta de mantenimiento

Desde el worktree autorizado, consultar sin crear archivos ni abrir motores:

```bash
PYTHONPATH=src /home/marjuraru/projects/investment-analyst/.venv/bin/python \
  scripts/close_data_chassis.py status \
  --source /home/marjuraru/.local/share/investment-analyst/workspaces/default \
  --destination /home/marjuraru/.local/share/investment-analyst/workspaces/data-chassis-43-rehearsal/restore-v2 \
  --controls-root /home/marjuraru/.local/share/investment-analyst/ops/data-chassis-45/cut-f-20261015 \
  --operation-id 1dec60f8-5091-4e1f-a18a-660443f12bf5 \
  --code-sha "$(git rev-parse HEAD)"
```

Salida: tabla JSON de fases ejecutables/reutilizables/pendientes, sin mutación.
`--dry-run` aplica el mismo contrato a cualquier fase. `--help` declara paths de controles,
delta, configuración no secreta del transporte, recovery, aceptación y lifecycle.
Cada invocación mutante exige HEAD de mantenimiento exacto y registra aparte digest del script,
bindings, tiempos/RSS y cantidades medidas; cantidades no instrumentadas permanecen null.
Los contadores Linux de E/S distinguen bytes de syscalls del proceso y bytes de bloque;
excluyen hijos. Rclone publica sus contadores acumulados por intento, separados en payloads
y controles, subida y lectura independiente. Una transferencia omitida puede medir cero;
estadísticas ausentes siguen siendo desconocidas. No sumar snapshots periódicos ni equiparar
estos contadores a ocupación lógica, asignación Linux o bytes físicos de C:.
Un recibo histórico conserva su SHA original, sin renombrarlo como validación del nuevo HEAD.

## Informe y cierre

`workspace-v2-final-acceptance-v1` liga base, herramienta, runtime, entorno, universo, carga,
evidencia por path/digest, pares y gates. El evaluador verifica archivos y conserva siete objetivos:
Q1 ≤3000000000 B o reducción ≥55%; Q2/Q3 ≥90%; Q4 ≤30000000 B/día ordinario;
Q5 mediana por familia/activo ≥80% menor; Q6 cero equivalentes nuevos; Q7 pico cgroup
≤`floor(9*MemoryHigh/10)` y deltas `oom/oom_kill/max=0`. Datos incomparables o ausentes son UNKNOWN.
No reducir familias, gates ni denominador; fixtures/CI no acreditan objetivos operacionales.

Q4 comienza el primer día UTC completo posterior a activación. Excluir el día parcial de corte,
migración/backfill, días abiertos y ausentes; no rellenarlos con cero. El primer informe deja
checkpoint de observación y fecha más temprana de evaluación, manteniendo v2 en servicio.

El smoke consolidado del SHA candidato enlaza limpieza, F, Drive/recovery, runtime, Q1–Q7 y
gates. CI `Python 3.12 quality` del mismo SHA es la suite completa; sólo pruebas focalizadas
locales por delta relevante. BUILD PASS/READY requiere aceptación íntegra; AUDIT independiente
en sesión fresca y FINALIZE requieren los guards del protocolo. Hasta entonces no se declara
DATA-CHASSIS DONE ni se abre automáticamente otro bloque.

## Recuperación del incidente WSL del 16 de octubre

El checkpoint `ops/data-chassis-45/pause-checkpoint-20261016T065620Z.json`
conserva los hashes de 38 archivos y 33 evidencias. Su relectura tras el nuevo
arranque no detectó diferencias. La comparación financiera quedó en `verification`
(2031529 métricas, 1968 diagnósticos), transición `APPLYING`; no equivale a un F
sellado ni autoriza activación. v1 volvió a arrancar: P2.9 requiere otro epoch
operacional al próximo corte, conservando progreso financiero compatible.

La verificación SQL generó aproximadamente 59 GiB de spill. En la reanudación,
el directorio ya estaba vacío: el recibo `orphan-spill-retirement-20261016.json`
registra cero archivos/bytes retirados, sin atribuir ahorro a esta operación.
El lifecycle excluye lectores/writers mediante el lock POSIX del archivo DuckDB
y los locks existentes de v2; no abre la DB ni reproduce WAL al retirar spill.
Rechaza archivos desconocidos/symlinks y conserva inventario previo fuera del
workspace. La DB y su WAL de 7437337 bytes siguen conservados.

El coordinador limita explícitamente el spill SQL a 1000000000 bytes antes de
importar. Una consulta que exceda ese límite falla sin ampliar el presupuesto ni
eliminar progreso confirmado. Este límite contiene el fallo; no demuestra que
la verificación final esté optimizada o completada. La corrección de la consulta
y la recuperación siguen pendientes, conservando los bindings históricos.

La API Windows midió 8157978624 bytes libres en C: y un VHD de 153161302016
bytes. El piso de P2 es 25000000000 bytes: no reanudar trabajo voluminoso antes
de acreditar capacidad fresca. Liberar ext4 no implica recuperar espacio de C:.
La compactación del VHD es administración separada, requiere autorización humana
y no se ejecuta desde BUILD. No repetir backup/restore base para resolverla.

### Checkpoint posterior: W verificado, F1 pendiente

El recibo `ops/data-chassis-45/recovery-W-20261016/financial-baseline-memory-composition-phase.json`
terminó a `2026-10-17T01:06:34.536563Z`: 2031529 métricas, 1968 diagnósticos,
preservación completa de B y digests financieros iguales a los controles históricos.
El certificado `ee03429bc86a06684a07730901e948e3b90d6464786da547d04d0735cace2c50`
está en `BASELINE_VERIFIED`, sin sucesor reservado. La recuperación nativa y la
verificación completa de relaciones ya tienen recibos separados y reutilizables.
Este resultado posterior sustituye el estado pendiente descrito arriba, conservando
aquellas observaciones como historia del incidente.

Reanudar desde ese certificado y su delta de membresía, conservando F0 `APPLYING`
y el sello/membresía B. No repetir recuperación WAL, migración base, descarga,
backup completo ni pasada financiera W. Antes del corte real todavía se debe
integrar y validar el consumo de `SUCCESSOR_BOUND` por el importador, la excepción
exclusiva para F0 cubierto y la reutilización de pruebas con bindings compatibles.
La reserva de F1 sólo se crea dentro de una sesión de fuente quiescente viva y
admitida por R1; una reapertura de sesión no hereda esa reserva aunque su corte
financiero sea idéntico. v1 permanece activo durante el desarrollo y las pruebas.

El [registro del incidente](incidents/data_chassis_45_wsl_spill_20261016.md)
contiene tiempos, E/S medida, hashes y límites de interpretación. Esta evidencia
local no acredita CI de los cambios sin publicar, activación ni Q4.

### Integración del sucesor: evidencia local posterior

El importador admite explícitamente un `SUCCESSOR_BOUND` de su sesión quiescente
viva. La excepción al detector de incompletas cubre exclusivamente el F0 cuyo
control coincide por SHA con el certificado. Conserva F0 `APPLYING`, reutiliza la
membresía/sello B y rechaza cualquier otra transición incompleta. El reconocimiento
de un F1 terminado exige su recibo terminal y su comparación ligada al certificado;
la reserva o la cobertura por sí solas siguen sin autorizar backup completo o activación.

El lector de novedades hidrata sólo IDs de F1 fuera de W, verifica sus hashes y
disponibilidad y divide páginas por número (≤256) y bytes UTF-8 (≤1 MiB). Antes de
sellar, compara el inventario final completo en orden disponibilidad/ID; incluye
IDs tardíos y no concatena digests B+delta. Ejecuta una pasada optimizada de
relaciones para F1 y archiva B una vez. Las fixtures de deltas 0/1/3 y de interrupción
después de publicar el sello acreditan este comportamiento local.

La reutilización de W tras cambios del importador requiere un companion de
compatibilidad explícito: hashes de los cuatro módulos archivados/candidatos,
fingerprint completo anterior/nuevo, entorno, dataset, política y SHA del verificador
de compatibilidad. La comparación AST conserva el verificador W y sus dependencias;
permite sólo los cuerpos/entradas de sucesor declarados fuera de ese recorrido.
No ejecuta código archivado, no modifica proofs antiguos y no valida por sí sola
las funciones nuevas: éstas requieren sus pruebas separadas. Cambios del verificador
W, dependencias, modelos, política o entorno rechazan la reutilización.

Prueba relacionada ejecutada: 96 passed en 135,54 s para inventario, importador
histórico, migración y transiciones. Las comprobaciones específicas posteriores
del reconocimiento de F1/otras incompletas dieron 4 passed en 12,10 s. Esta evidencia
no es CI del SHA final ni un corte real. Permanecen pendientes la validación del
pipeline completo de corte, su presupuesto temporal y la recuperación/activación.

El coordinador acepta ahora `--reconciliation-id` y `--baseline-code-root` junto
al nuevo `--operation-id` de F1. Propaga la admisión de su worker a cutover/migración;
la fuente debe estar en una sesión quiescente real. La fixture del pipeline completo
pasó (1 passed, 3,35 s), rechazando cualquier llamada al recorrido financiero completo.
Las pruebas de cutover y CLI posteriores a esta integración dieron 42 passed en
16,38 s. No ejecutar ese comando sobre el servicio real todavía: quedan por verificar
el límite conjunto de corte/recuperación de v1 y la recuperación física posterior.

### Reutilización diferencial de raw y observaciones

El camino reconciliado exige además `--core-baseline-path`: un certificado propio
de W que enlaza los controles raw/observaciones conservados, la DB recuperada,
el código y la política de recursos. La verificación de W contrasta todos los
metadatos/proyecciones raw nativos con el binding terminal anterior y reconstruye
los bytes canónicos de observaciones desde columnas tipadas. Reutiliza la prueba
anterior de los blobs inmutables; no afirma haberlos leído nuevamente.

Durante F1, las comparaciones nativas recorren el inventario completo ordenado.
Sólo las novedades o representaciones no canónicas se hidratan mediante los
contratos existentes, con páginas y buffers acotados. Las filas confirmadas no
se transportan de nuevo. Cada inserción nueva tiene antes un intent inmutable,
ligado al corte, sesión, certificado y hashes de contenido. Los nuevos blobs se
sincronizan antes de confirmar su índice. La comparación core final conserva
las referencias a esos intents; una repetición reconoce las filas ya persistidas.
Esto no habilita reutilizar F1 después de perder su sesión quiescente.

Validación local: 116 passed en 84,18 s para inventario, migración, cutover y CLI.
La prueba posterior de interrupción tras commit raw/observaciones y repetición
del delta 3 dio 1 passed en 3,55 s. El pipeline prohíbe llamar a cualquiera de
los tres importadores completos. Las fixtures incluyen deltas 0/1/3, IDs tardíos,
Decimal exacto, metadatos inconsistentes, contenido divergente, pérdida de W y
JSON equivalente no canónico.

La comprobación core posterior sobre W real terminó el 2026-10-17T03:35:25Z,
en 13,992329 s y con RSS máximo de 781.725.696 B. Confirmó 929.001 raw y
527.682 observaciones, con los mismos digests y binding de metadatos anteriores.
El recibo `recovery-W-20261016/core-baseline-phase-20261017.json` referencia el
certificado core SHA-256
`b769e74cf1d0ac7de4d3a8579120197600fda508217825a03f16cfe60e08e88f`.
La lectura física del proceso fue 2.921.480.448 B; corresponde a esta comprobación
de DB/controles y no a una nueva lectura del corpus de blobs. La admisión R1 y
el último sample están en `resource-run-c95975f8c5f8411698aa8797e8918632`.

V1 permaneció activo. El certificado W original y F0 siguen intactos y no hay
F1 real reservado. Antes del corte siguen pendientes la invalidación/recuperación
tras reiniciar v1, el presupuesto conjunto de 1.800 s, la reutilización física
de inmutables del padre y la recuperación/activación real.

### Preparación física con v1 activo

`prepare-parent-reuse` enlaza el recibo de restauración de Drive ya verificado,
UUID/root/manifest del destino, índice del padre y código. Terminó en 21,922518 s,
con RSS máximo de 248.320.000 B, sin abrir DB ni leer blobs. Conserva el índice
SHA-256 `a397992566c6cd1e1cdb4e72fb3f843ca23caaa48bd24f425040b884f22f4485`
y el mismo padre `6c69b9df-d530-5502-8c95-5c9b756eec86`.

`--immutable-parent-proof` habilita reutilización sólo para raw/documentos
direccionados por contenido, con path/hash/tamaño del padre sellado y ctime
anterior a su restauración verificada. Un cambio posterior obliga a verificar
contenido; un hash de contenido incompatible se rechaza. DB/WAL y estado
operacional no reciben esta excepción. Las pruebas físicas y CLI dieron
55 passed en 47,72 s.

`prepare-delta` prepara exclusivamente W `BASELINE_VERIFIED` sin sucesor reservado;
comprueba que la DB fotografiada conserva el hash de W. Su manifest sigue
`APPLYING` y no autoriza activación. El presupuesto de preparación reserva
6.500.000.000 B de crecimiento y 2.000.000.000 B de controles, conservando el
piso de 25 GB y los límites R1 originales.

La primera preparación alcanzó memory.high: unos 211 MB anon, 1,34 GB file y
1,13 GB kernel/slab. R1 detuvo el worker y preservó 293.876.510 B confirmados
de inventario. La segunda se detuvo durante lectura rápida: file llegó a
2.424.512.512 B; la espera de dos segundos entre reclamaciones y la dependencia
del heartbeat de un segundo resultaron insuficientes. No hubo OOM ni parada de v1.

La corrección administra exclusivamente `memory.reclaim` del cgroup del worker,
verificando PID/start/boot y pertenencia a la unidad propia. Un monitor de 50 ms
permite una solicitud a la vez, con timeout de dos segundos y error persistente.
No eleva límites ni escribe en un cgroup padre. Mantiene el supervisor independiente.
La [documentación del kernel](https://docs.kernel.org/admin-guide/cgroup-v2.html)
define esta reclamación y su posible resultado parcial `EAGAIN`.
Las pruebas de CLI posteriores dieron 42 passed en 7,61 s; la prueba focalizada
posterior de lecturas rápidas dio 1 passed en 1,15 s. Después terminó el plan
físico real en 405,149908 s, con RSS máximo 263180288 B; el recibo es
`physical-delta-W-phase-20261017.json`. Las dos paradas anteriores se conservan
como fallos diagnosticados, no como evidencia de completion.

### Transferencia W y lecturas independientes temporales

La misma fase `prepare-delta` admite `--delta-action upload|restore` sobre el
manifest ya sellado, sin ejecutar de nuevo `plan`. Verifica el certificado W
sin sucesor y los bindings de raíz/UUID/epoch/manifest padre. Ninguna acción de
esta fase reserva F1, detiene v1 ni activa v2.

`--independent-read-root` debe ser disjunto del staging, padre y slot. Cada
objeto/control retenido procede de `fetch_sealed_object` de Drive y lleva
key/digest/longitud/configuración y política exacta del verificador. Antes de
consumirlo se vuelve a verificar el hash local. Si falta, está corrupto o su
prueba no es compatible, se obtiene de Drive; nunca del staging. Una configuración
distinta falla antes de recuperar. Cambios ajenos al verificador de lecturas
no invalidan esos objetos: su binding cubre los helpers concretos de validación,
no todo el módulo físico. La prueba histórica del padre conserva por separado
su binding original y no se edita para aparentar compatibilidad.

Pruebas actuales: ocho casos de lectura independiente pasaron en 33,51 s;
dos casos del coordinador para upload/restore sin replan pasaron en 0,94 s.
Tres casos de reserva/interrupción/reapertura pasaron en 9,98 s: acreditan
el rechazo seguro de una sesión nueva, no la capacidad de retirar una reserva.
No sumar estos grupos con los resultados históricos como tests únicos.

Comando de transferencia iniciado con v1 activo (el output es exclusivo: si
ya existe un recibo terminal, leerlo y reutilizarlo; no repetir esta invocación):

```bash
IA45_OPS=/home/marjuraru/.local/share/investment-analyst/ops/data-chassis-45
IA45_W=/home/marjuraru/.local/share/investment-analyst/workspaces/data-chassis-43-rehearsal/restore-v2
PYTHONPATH=src /home/marjuraru/projects/investment-analyst/.venv/bin/python scripts/close_data_chassis.py prepare-delta \
  --source /home/marjuraru/.local/share/investment-analyst/workspaces/default \
  --destination "$IA45_W" --controls-root "$IA45_OPS/recovery-W-20261016" \
  --operation-id 241e88e6-4218-427c-98f0-b988d95b9d88 \
  --code-sha fe11b14953c449977525f9aa628f0219929e08e7 \
  --resource-budget "$IA45_OPS/recovery-W-20261016/physical-delta-W-budget-20261017.json" \
  --core-baseline-path "$IA45_W/state/progress-core-reuse.241e88e6-4218-427c-98f0-b988d95b9d88.b769e74cf1d0ac7de4d3a8579120197600fda508217825a03f16cfe60e08e88f.json" \
  --parent-index "$IA45_OPS/physical-delta-base-20261015/physical-delta-index.jsonl" \
  --parent-manifest-sha256 7817ebd3d21d9d9431809849701e5d6bd3a95e338fb0d2cdf885ee8ad48b43e4 \
  --delta-root "$IA45_OPS/physical-delta-W-20261017" --delta-action upload \
  --transport-config "$IA45_OPS/recovery-W-20261016/drive-transport-20261017.json" \
  --independent-read-root "$IA45_OPS/independent-reads-W-20261017" \
  --output "$IA45_OPS/recovery-W-20261016/physical-delta-W-upload-phase-20261017.json"
```

Ejecutar desde el worktree writer. El comando supervisa su propia unidad R1;
resultado esperado: recibo `preparation_only=true`, transferencia sellada y
copias independientes verificadas. El SHA de commit identifica HEAD; el recibo
R1 y el hash del tool identifican además los bytes locales aún no publicados.

### Frontera PLAN pendiente: reserva cuya sesión murió

El contrato vigente sólo tiene PREPARED/BASELINE_VERIFIED/SUCCESSOR_BOUND y una
reserva exclusiva. `reserve_recovered_successor` rechaza otra sesión aunque
coincidan el ID y el fingerprint financiero. `_exclusive` rechaza un segundo
reconciliador. BUILD no puede borrar la reserva, reutilizar su ID en otra sesión
ni añadir una excepción implícita. Por tanto no se reserva F1 real hasta que
PLAN concrete esta frontera; el resto del ensayo W puede continuar.

| Momento del fallo | Comportamiento vigente | Transición que falta autorizar |
| --- | --- | --- |
| Antes de crear la reserva | W permanece sin sucesor; no hay escrituras del intento | Nuevo freeze/sesión bajo los guards existentes |
| Reserva durable, antes de publicar el pointer | El recibo inmutable conserva el intento; replay sólo en la misma sesión | Retirada explícita de la reserva huérfana sin eliminar ese recibo |
| Después de reservar/bindear | Otra sesión falla; la quiescencia no se hereda | Invalidar únicamente la vigencia operacional y liberar exclusividad para un nuevo intento trazable |
| Después de confirmar appends | Filas/intents quedan conservados; replay acreditado dentro de sesión | Reconciliar W más appends válidos y autorizar un nuevo sucesor exclusivo sin reinserciones ni revalidación íntegra de W |
| Después de activar v2 | v1 anterior no representa escrituras nuevas | No habilitar retorno automático a v1 |

Propuesta mínima para decisión PLAN, todavía **no implementada**: un evento
inmutable de invalidación de sesión/reserva, ligado al certificado anterior,
intento, sesión/identidad del proceso, generación de escrituras, motivo y UTC;
emisión exclusiva bajo lock tras demostrar ausencia del owner. Un certificado
sucesor referencia ese evento y los appends verificados, conserva B/F0 y reserva
un nuevo intento sin sobrescribir el anterior. PLAN debe fijar los estados o
eventos exactos, quién puede efectuar cada transición, y la exclusividad entre
certificados antes de que BUILD cambie la representación. Esto también condiciona
la cancelación global del corte y el reinicio de v1 antes de activar v2.

### Fronteras adicionales que PLAN debe concretar sin atajos de BUILD

**Raw grande y allowlist.** La ruta diferencial rechaza un raw nuevo mayor de
1 MiB en `workspace_v2_migration.py`. Quitar sólo ese `raise` desplaza el problema:
`JsonRawRecordRepository._verified_file_bytes` usa `read_bytes()`;
`RawV2Staging._save_chunk` materializa `canonical_json_bytes(record)` y `_write_blob`
vuelve a materializar el archivo. Esas implementaciones viven fuera de la
allowlist actual. BUILD no las duplicará con inserts SQL y escrituras ad hoc
en el coordinador para esquivar esa frontera.

Propuesta mínima para PLAN: autorizar una operación aditiva de lectura/escritura
raw con I/O acotado dentro de los repositorios existentes y su serialización,
manteniendo exactamente RawRecord, JSON canónico, identidad, SHA, proyecciones,
conflictos e idempotencia. Un modelo individual puede ser grande; los buffers
de transporte no deben convertirse en un límite de dominio. No añadir dependencia,
schema persistente ni cambiar los métodos ordinarios. Probar roundtrip >1 MiB,
Unicode/escapes, enteros grandes sin conversión float, corrupción, interrupción,
replay y pico de recursos. Extender la allowlist sólo para las rutas necesarias:

| Ruta propuesta | SHA-256 en base `dbbdfd021b0fee20be31b2d2a845874b0e861067` |
| --- | --- |
| `src/investment_analyst/storage/raw_records.py` | `3b92e92580e20c8eb5f7e090a4e76519cb37ac48d730fd158615ea6c138d89d0` |
| `src/investment_analyst/storage/raw_v2.py` | `3c723fa8228024fb3840b4fe19f309bdb9b7bddb18f3fc0bd34b7dd8dccb9ca0` |
| `src/investment_analyst/storage/serialization.py` (sólo helper aditivo de I/O raw, si hace falta) | `1e91f541f6266a36e7e2b5ef150b5e974d9fdd14e42c6c9386b2bd68a2dc58e3` |
| `tests/unit/storage/test_raw_records.py` | `55f0a01bac303d09ad9b72e30ebc6a2df818996a268d579c98205ce88fd6c0ea` |
| `tests/unit/storage/test_raw_v2.py` | `22dc73ea7a3fac8b422d93d357f36f9c2bd91df8f03ffcc221fca668ea80b836` |
| `tests/unit/storage/test_serialization_and_api.py` (si se usa ese helper) | `97e5572c46c8920b9313b98165fb21003c5b78e344d81ec2d8a9ae1c5343a7c1` |

Las dos implementaciones raw conservan esos mismos hashes en el working tree.
No se han modificado ni se ha eliminado el rechazo accidental simulando que
ya existe un lector/writer acotado.

**ctime de enlaces y retiro del único slot.** La prueba física actual liga el
restore previo y su fecha; no representa una secuencia de cambios de inode
producidos por enlaces/desenlaces controlados. El receipt de restore sólo cuenta
archivos/bytes enlazados. Esos conteos no acreditan qué inode cambió ni cuál es
su ctime válido después de retirar el slot. Ignorar ctime o sustituir la fecha
del restore anterior sería un atajo inválido.

Propuesta mínima para PLAN: concretar un testigo físico durable y acotado de
link/unlink en los companions de recuperación existentes, ligado a prueba del
padre, manifest/índice, raíz/UUID, filesystem/inode, path/digest/longitud,
mtime y ctime/nlink antes/después, verificador y operación bajo lock. Un prefijo
confirmado se reutiliza por streaming; cola no confirmada requiere hash del
archivo afectado. Cambios desconocidos obligan a verificar contenido o fallan.
Nunca aplica a DB/WAL. Debe cubrir también el unlink del retiro por lifecycle,
para liberar el único slot W antes del restore F1 sin forzar una nueva lectura
del corpus histórico. PLAN debe fijar los campos/referencias y su preservación;
BUILD no publica aquí un formato nuevo por iniciativa propia.

El retiro de slots actual exige COMPLETE. El retiro del ensayo W necesita la
excepción exacta de snapshot preparatorio: receipt APPLYING ligado al certificado
W intacto, cobertura/readback terminados, copia retenida idéntica y exclusividad;
no permite retirar restore-v2 ni v1. Esa compatibilidad debe quedar explícita
en el contrato antes de ejecutarla. R6 ya autoriza retirar el slot por función
cumplida; falta representar la evidencia específica de este W preparatorio.

No usar `CAST(JSON AS VARIANT)` como sustituto genérico del parser raw: el probe
local convirtió `184467440737095516160000` a float. La ruta acotada debe preservar
también enteros grandes, además de Decimal, Unicode y hashes canónicos.

Integración de readback todavía pendiente: `compatible_reader_instance` ya tiene
prueba con el release 44 instalado, pero `_readback` del coordinador aún debe
invocarlo en su subprocess. No ejecutar el string legado con apertura sin cap.
La cobertura completa de paths/raw-index y la muestra de modelos del lector 44
se acreditan separadas; el chequeo de compatibilidad debe usar sólo la DB del
slot admitido, sin abrir después el candidato fuera del perfil de una sola DB.

E1 terminal (2026-10-18T20:06:52Z): upload phase SHA256 808170fcb58285b0f83ce610bf196985219ba2b512c297a23ae45fbd4d673dd6; recibo físico upload SHA256 103b4e4b2bc2e5ab1ff7323326c0fbab0435fcddcb4882f96d5f9009d8707b15. Contadores: upload_payload=0, download_payload=0, upload_controls=354436037, download_controls=354349445 bytes (rclone incluye accounting/retries; no son tamaño lógico único). Epoch W2 y manifest/files intactos. Esta transferencia no acredita restore ni GO. Los 22 releases E0 terminaron RETIRED; siguen current/previous/44. No repetir retiros ni generar staging por volver a probar fixtures.
