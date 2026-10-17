# DATA-CHASSIS-45: operación y aceptación

Registro versionado del incidente y límites del diagnóstico: [INC-DC45-WSL-20261016](incidents/data_chassis_45_wsl_spill_20261016.md). La receta operativa vigente es R0–R7 del Issue #345 consolidado; los apartados anteriores que usan P0–P5 conservan contexto histórico y deben reconciliarse antes de operar.

## Punto vigente: enmienda C1–C4 (2026-10-17, 17:15 UTC)

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
