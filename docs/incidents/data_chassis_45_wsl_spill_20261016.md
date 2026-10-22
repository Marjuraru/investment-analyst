# INC-DC45-WSL-20261016: spill SQL, WSL y recuperación preservada

> Histórico preservado. Decisión 2026-10-21: v2 nuevo; F5 y sus controles quedan archivados, sin borrado ni reparación como prerrequisito del arranque.

Estado: capacidad recuperada; recuperación lógica, reconciliación y optimización pendientes.
Work Block: [#345](https://github.com/Marjuraru/investment-analyst/issues/345).
R0–R7 describe la recuperación histórica. La receta vigente es ../data_chassis_v2_fresh_start_plan.md; este registro no autoriza reanudar F5 ni acredita aceptación.

## Hechos y límites del diagnóstico

El intento 9 del catch-up F0 empezó el 16 de octubre a las 04:24:32 UTC con
51.537.399.808 B libres en C:. A las 05:45:54 el checkpoint registraba comparación
de 2.031.529 métricas y 1.968 diagnósticos, en fase `verification`. Durante la
verificación SQL se observaron aproximadamente 59 GiB de archivos temporales
en `restore-v2/storage/v2/index.duckdb.tmp`.

El journal mostró errores `UtilAcceptVsock` y supervisión lenta. WSL se reinició;
la medición posterior de C: fue 4.433.825.792 B mediante statvfs del montaje
Windows. Es un método distinto de la API Windows usada antes: se conserva esa
distinción. No se encontró un registro OOM en los journals consultados.

La presión de disco por spill y expansión del VHD es la explicación principal
respaldada por la cronología y la recuperación física. No se perfiló el operador
exacto del pico ni se demostró que OOM fuera la causa del cierre o la causa única
de los bloqueos. No hay inventario sellado contemporáneo de los 59 GiB.

## Error de BUILD e impacto

BUILD confundió paginación de modelos con presupuesto del motor: <=256 modelos
por página no limita joins, DISTINCT, sort ni spill nativo. Las consultas globales
expandían referencias compartidas métrica→secuencia→segmento→miembro. La ruta
incluía además varias validaciones completas; no se afirma que todas alcanzaran
a ejecutarse antes del fallo.

La unidad transitoria de mantenimiento no tenía límites explícitos de cgroup.
Medir capacidad entre fases no protegía una consulta monolítica en ejecución.
La telemetría de producción no cubría el job de mantenimiento. V1 quedó detenido
aproximadamente nueve horas y 37 minutos hasta el primer reinicio observado.
Auxiliares guardados en `/tmp` se perdieron con el reinicio, aunque el código,
datos y checkpoints persistidos se conservaron.

La primera contención añadió un techo de 1 GB de spill después de abrir storage.
Eso no protegía el replay de WAL durante `duckdb.connect`. R1 exige ahora config
pre-connect, presupuesto agregado, cgroup, supervisor y deadlines probados.

## Recuperación administrativa del VHD

El usuario autorizó expresamente liberar espacio fuera del envelope BUILD.
TRIM terminó correctamente, pero sus 927 GiB eran rangos lógicos descartados,
no espacio recuperado en C:. El VHD seguía midiendo 153.161.302.016 B.

1. Intento 1: ChatGPT reconectó Ubuntu y volvió a ocupar el VHD. DiskPart falló.
2. Intento 2: con comprobación del cliente y espera exclusiva de 30 s, terminar
   sólo la distribución seguía sin liberar el VHD. DiskPart no se ejecutó. El
   reinicio posterior ocurrió después del fallo; no se atribuye este intento
   a la misma reconexión sin evidencia.
3. Intento 3: comprobar otros consumidores, parar normalmente v1, cerrar la VM
   con `wsl --shutdown`, acreditar acceso exclusivo y compactar desde Windows
   administrador permitió completar. No hubo force-detach ni borrado de datos.

| Medida | Antes | Después inmediato |
|---|---:|---:|
| Libre en C:, API Windows | 9.349.091.328 B | 72.161.185.792 B |
| Longitud del VHD Ubuntu | 153.161.302.016 B | 90.336.919.552 B |

Recuperación C: 62.812.094.464 B; reducción del VHD: 62.824.382.464 B.
No son la misma magnitud. El recibo final tiene `Status=complete` y
`MeetsCapacityFloor=true`. Docker no se limpió ni compactó. La compactación ya
terminó y no se repite como parte del nuevo BUILD.

El directorio de spill ya estaba vacío cuando se ejecutó su retiro por lifecycle;
su recibo registra 0 archivos/0 B. No se le atribuye liberar los temporales.

## Evidencia preservada

Raíz: `/home/marjuraru/.local/share/investment-analyst/ops/data-chassis-45`.

- `PLAN-handoff-incidente-WSL-20261016.md`, SHA-256
  `9d948c8ffb0ef455cd9aa714cc82b5f10412e3c714a3df3ae1192afa5c123e96`.
- `PLAN-handoff-incidente-WSL-20261016.evidence.json`, SHA-256
  `e80e93291bbcc2a7fb8caa20c0f8b49518c87e739215b2babb9624c4db900bf9`.
- `pre-compaction-checkpoint-20261016T152742Z.json`, SHA-256
  `b918871280bafdad2ef20d1181b6bf46970ae0868bcd87879e12a273f52023a3`.
- Recibos `vhd-compaction-attempt{1,2,3}-result-20261016.json`; scripts y
  diagnósticos de reconexión/shutdown referenciados por el inventario anterior.

39 archivos de trabajo y 38 evidencias del checkpoint se contrastaron sin
diferencias tras compactar. Esto acredita conservación, no integridad lógica
de DB/WAL ni aceptación de la migración. El HEAD publicado no contiene todos
esos cambios locales. No se hizo commit/push/merge durante la recuperación.

## Estado y trabajo obligatorio pendiente

F0 `1dec60f8-5091-4e1f-a18a-660443f12bf5` conserva `APPLYING` y `catchup_running`.
Raw: 929.001; observaciones: 527.682; métricas: 2.031.529; diagnósticos: 1.968.
DB/WAL registrados: 2.485.661.696 / 7.437.337 B. Dos probes de apertura con
128/512 MB fallaron por límites diagnósticos: ni corrupción ni recuperación
correcta quedaron demostradas. No se borró WAL ni se selló F0.

V1 se reabrió: su quiescencia/estado operacional F0 ya no son reutilizables.
R2 debe recuperar W y ligar un sucesor por certificado, conservando F0, B y
sus bindings. R3 debe demostrar el verificador optimizado sobre W con v1 activo.
No editar fingerprints antiguos, adoptar cursores como F1 o reiniciar la base.

Pruebas del delta previo de contención: 30 passed in 9.12s en los módulos unitarios
de migración/lifecycle; Ruff/formato y diff-check pasaron. Esa evidencia no prueba
los controles nuevos de R1 ni la reconciliación R2: requieren sus propios tests.

Riesgos abiertos: replay bajo presupuesto desde connect, recursos agregados y
supervisión/cancelación efectiva, SQL sin expansión masiva, preservación B⊆W⊆F1,
equivalencia operacional, recuperación padre+delta, activación integrada y Q1–Q7.
Q4 no comenzó: v2 nunca fue activado. La capacidad recuperada no cierra estos gates.

## Reanudación R1–R3: hallazgos posteriores al checkpoint

La recuperación física del 2026-10-16 cerró el WAL en 10,75 s. El recibo
`ops/data-chassis-45/recovery-W-20261016/wal-recovery-phase.json` conserva la
preimagen DB/WAL, controles F0, código anterior y postimagen. DB:
2485661696 → 2459185152 B; WAL previo 7437337 B, ausente al cierre.
No se modificó el companion `APPLYING` de F0 ni la membresía/sello B.
El nuevo companion permanece `PREPARED`: no acredita W ni cobertura de F1.

El primer perfil de ciclos por particiones se canceló tras más de 230 s:
consultas individuales cortas repetían escaneos del corpus. Se conserva el
diagnóstico en `resource-run-0dbeff360ce74cf69b483b3e3239659c/profile-stop.json`
bajo esa misma raíz de recuperación. La cancelación fue sólo del worker;
v1 siguió activo. El estado `failed` residual impidió un lanzamiento posterior
antes de abrir DB; se limpió únicamente esa unidad y se añadió `--collect`.

La siguiente variante de grafo global con IDs de texto excedió el presupuesto:
el supervisor midió **1546268672 B asignados** de temporales y canceló el worker.
Recibos: `resource-run-119bd84436bc46248846dbba084b5b88`. La consulta identificada
por `4be47a4fce6269617c0dc3d9698776c965197dbd896315bfb0d1d5bc34297fe6`
materializaba métrica→secuencia→segmento→métrica. No terminó ni emitió un PASS.
Al cerrar quedaron cero archivos en el directorio temporal de esa operación;
no se atribuye esta eliminación nativa a una limpieza manual ni a compactación.

**Error adicional de BUILD:** las pruebas iniciales verificaban config pre-connect
y `current_setting`, pero no ejercitaban presión real de spill. En DuckDB 1.5.4
instalado, una fixture con límite inicial de 1000000 B terminó con 153616384 B
de temporales. Repetir el mismo valor mediante SET antes de la consulta produjo
`OutOfMemoryException`. Es un resultado reproducido en esta instalación; no una
afirmación de que toda versión de DuckDB presenta el mismo comportamiento.
El readback de un setting no probaba el techo efectivo. Por tanto, la admisión
inicial y el replay anterior no acreditan retrospectivamente un R1 completo,
aunque durante ese replay no se observó spill. Se preservan como historia.

La corrección prepara un motor vacío con config explícita, activa el callback
del límite mientras el archivo real aún no está abierto y sólo entonces adjunta
la DB con su modo físico. Restaura el catálogo inicial para que los cursores
auxiliares sigan apuntando al archivo real. No crea tablas ni cambia el esquema
persistente. El lector 44 usa ese mismo motor mediante un adaptador local al
proceso; sus archivos instalados no se editan. Las fixtures nuevas prueban presión
con cuotas 0/1000000 B, read-only efectivo, conservación de filas confirmadas,
WAL nativo, cursores y el lector 44 real. No se amplían memoria, spill o deadlines.

R3 requiere ahora medir cardinalidades sin materializar todo el grafo y validar
una representación acotada antes de otra pasada real. Ningún perfil de tamaño,
contador o configuración equivale a `BASELINE_VERIFIED`. v1 permaneció activo,
PID 291 en las comprobaciones posteriores a ambas cancelaciones.

Fuentes primarias consultadas para contrastar el comportamiento, sin sustituir
la evidencia local: [configuración DuckDB](https://duckdb.org/docs/current/configuration/overview),
[gestor temporal de DuckDB v1.5.4](https://github.com/duckdb/duckdb/blob/v1.5.4/src/storage/temporary_file_manager.cpp),
[ATTACH/DETACH](https://duckdb.org/docs/sql/statements/attach).

## Continuación: relaciones completas y validación financiera de W

La prueba completa nueva de relaciones, PIT y ciclos terminó a
`2026-10-16T20:36:11.829779Z` en 415,17 s sobre 2031529 métricas y 1968 diagnósticos.
Recibo `recovery-W-20261016/relationships-bucketed-phase.json` bajo ops45;
RSS máximo 1283727360 B, escritura física 1671168 B. Conservó la imagen recuperada
`e5557a41790037eb28e820bf6fc14c00dc5882875705d8877b9ec05f3fad914e`, sin publicar
un sello W ni completar F0. Esta prueba acredita relaciones, no contenido financiero.

Los siguientes intentos financieros se mantuvieron dentro del envelope y con v1
activo. Todos se conservan; ninguno equivale a `BASELINE_VERIFIED`:

| Recibo `resource-run-*` bajo `recovery-W-20261016` | Resultado y corrección |
| --- | --- |
| `8098d1aad1b14b0eb9f0bd8825806903` | OOM al ordenar la unión con todas las columnas métricas; ninguna página confirmada. Sustituida por ordenación mínima de ID/disponibilidad/hash y lectura de contenido por páginas. Última muestra: spill asignado 760975360 B, eventos de memoria `[0,0,0]`. |
| `dbba68dc1b7240b990dc86653d7a1b83` | Parada propia por rendimiento insuficiente. Conserva checkpoint de 1024 métricas. Las lecturas individuales de fragmentos se agruparon con preflight de tamaño agregado ≤1 MiB. |
| `eccf1297bba343939c3b4fcd3500abac` | Error de BUILD: introdujo `financial-page`, fuera del vocabulario del supervisor. Se corrigió a `sql` y se añadió una aserción de compatibilidad en la fixture de reconciliación. |
| `416b47b5db684223b48ef8d7d8d72496` | OOM durante lectura por páginas, después de progreso confirmado; se añadió perfil y traza de funciones sin mensajes/valores upstream. |
| `1dee28b90bde470bb403824624ad3484` | El diagnóstico encontró otro error de BUILD en reanudación: comparaba cursor UUID con `result_id` persistido VARCHAR. Corregido contra el esquema actual; la fixture fuerza ahora un cursor no vacío antes de interrumpir. |
| `8a0c2a2ea8ad47bfba3bec63ace2eb9a` | La traza localizó el OOM en la consulta de una página de métricas, no en el verificador de ciclos. Perfil y `failure-frames.json` preservados. Se prepara comprobación de escaneo sin cargar índices ART; hipótesis pendiente de validación real. |

Las primeras fixtures financieras detectaban contenido alterado, Decimal/Unicode/UTC
y reanudación, pero no ejercitaban determinísticamente todos los límites de integración:
fase válida para el supervisor, tipo nativo del cursor y memoria del lector sobre el
corpus real. Esa carencia fue de BUILD. Se mantienen límites, datos y evidencias; no
se presenta la contención de estos fallos como finalización ni como ahorro físico.
Cada cambio del verificador conserva el namespace anterior y crea checkpoints ligados
al código nuevo; no se editan fingerprints históricos para adoptar páginas ajenas.

### Resultado posterior: baseline W validado el 17 de octubre UTC

La pasada `resource-run-7256399aa41e4e998739969a2b736055` terminó correctamente a
`2026-10-17T01:06:34.536563Z`. Su recibo es
`recovery-W-20261016/financial-baseline-memory-composition-phase.json` bajo ops45.
Duración del coordinador: **9276,606493 s**; RSS máximo **1218486272 B**.
La prueba verificó **2031529 métricas y 1968 diagnósticos**, incluyendo la
preservación por identidad/contenido de B (**1947523 métricas y 1777 diagnósticos**).
El delta de membresía mide **9936010 B**, SHA-256
`820f79af27a0808c283ce2207783d92b022c88d0cec2a258148b3af1d12cddaa`.

El certificado inmutable `progress-reconciliation-generation.241e88e6-4218-427c-98f0-b988d95b9d88.ee03429bc86a06684a07730901e948e3b90d6464786da547d04d0735cace2c50.json`
publica `BASELINE_VERIFIED`, sin reserva de sucesor ni cobertura F1. Su SHA-256 es
`ee03429bc86a06684a07730901e948e3b90d6464786da547d04d0735cace2c50`.
Conserva la DB recuperada `e5557a41790037eb28e820bf6fc14c00dc5882875705d8877b9ec05f3fad914e`,
la membresía/sello B y el companion F0 `APPLYING` intactos. No publica un sello W.
El dataset W queda ligado a `8da3c2289d41062df0f560dd69130f472b3d25d4998e079b5d2c548ce493c461`
y al fingerprint de los cuatro módulos
`41d2046d29a4a354491b405d3c8cdb6e6edf29b56e2fea5bacf8b2c2bfd29475`.

Los digests financieros coinciden con la comparación histórica conservada:
métricas `de2e5f9539467b0da4a785be41f87cc5b74ef4e250870dcde08f75adee1f0149`;
diagnósticos `5f675c2fa3e3e99ca6c58f50f6b0ec00008998dbfb93bf97866605732159e550`.
Esta coincidencia complementa la prueba fresca; no sustituye sus bindings.

La eliminación de escaneos ART durante esta verificación y la partición del
orden de referencias evitaron los OOM anteriores en la pasada final. Los intentos
intermedios y sus checkpoints se conservan. La ejecución registró **7662417116063 B**
en `rchar`, **1531986176 B** en `read_bytes`, **56555768 B** en `wchar` y
**163860480 B** en `write_bytes`. Distinguir lecturas repetidas/cacheadas de E/S
física: el volumen de `rchar` demuestra que el recorrido sigue siendo costoso.
Estos contadores excluyen hijos y no atribuyen cambios de espacio libre de C:.
No repetir esta pasada para continuar F1 si sus bindings siguen siendo válidos.

Al releer el resultado, v1 sigue `active/running`, PID 291, y la unidad de
mantenimiento está inactiva. Pendientes: integración del sucesor exclusivo con
el importador, cierre/recovery incremental y activación, gates Q1–Q7 y CI del
candidato final. Las pruebas de F1 en fixtures no acreditan un corte real.

## Revisión del handover F2 (2026-10-17)

Esta sección conserva el resultado recibido y distingue persistencia de datos,
integridad de la evidencia y vigencia operacional. No reescribe los recibos
históricos ni afirma corrupción de datos que no se haya demostrado.

### Estado recibido y evidencia comprobada

El handover externo informa catchup terminado y propone activar con el delta
W. Se leyó su archivo original, el Issue #345 vivo, el código actual y los
controles persistidos. HEAD sigue `fe11b14953c449977525f9aa628f0219929e08e7`;
el código recibido era local y no estaba publicado. El guard vivo resuelve
BUILD_PRODUCT/CRITICAL/AUTO, CONTINUE. Ese resultado estructural no acredita
la semántica de los hooks de runtime ni el cumplimiento de la ventana.

- F2: `ff244b14-2cc5-4003-b788-ed5efba68d63`;
  `cut-f2-20261017/cutover-checkpoint.json` declara `catchup_complete` y
  `migration_completed_at=2026-10-17T16:50:42.347311Z`.
- Fuente congelada a `13:00:58.625847Z`, fingerprint
  `e1ddcd3275a8a2eb5ed74af925b97078d8db17eb74141f84b3aadce0e707bc8a`.
- Último recibo catchup: inicio `16:22:40.709541Z`, fin `16:50:57.786556Z`,
  duración 1756,414795 s; tool SHA
  `b8eb6b282b500275edf790e16b1bf833c1ea8ff6c035d66f54660088670d0eac`.
  Esta duración no incluye los intentos anteriores ni delta, restore o salud.
- Totales reportados por ese recibo: 929333 raw, 530071 observaciones,
  2053921 métricas y 2036 diagnósticos. Digest métricas
  `9fa42210a246418a7469c9226076c4c25ea1b303f5ac3a86cd48c9ded4a7e253`;
  diagnósticos `e0d4d78e4b01df10f9193d86cb882efaaadb55e80221bebbaf51b8ea247aa0fc`.
  Se comprobaron los documentos y su parseo tipado; no se reabrió la DB real
  para repetir esos conteos ni se consideran prueba independiente nueva.
- Reconciliación W actual: SUCCESSOR_BOUND a F2. Parsean sin parche la reserva,
  el companion actual y el receipt histórico terminal. Parsear no valida la
  ejecución que produjo esos documentos.
- Existe restore W a `recovery-W-restore-rehearsal`, epoch
  `241e88e6-4218-427c-98f0-b988d95b9d88`, histórico APPLYING, 929762 archivos.
  Manifest `3a87c39b081f9c20173a54007567fbccbc39d316470e43928d473f7b3edd22a7`.
  Restore terminó `07:47:45.849453Z`, duró 2051,301442 s; hay readback 44
  registrado `07:48:49.492999Z`, con los conteos W, distintos de F2.
  No se repitió ese restore ni se descargaron de nuevo los 676 volúmenes.
- No existe el delta F2 en las raíces inspeccionadas. El comando activate
  propuesto referencia `delta-W-20261016` y un receipt W: no satisface igualdad
  de epoch ni histórico COMPLETE exigidos por el coordinador.

### Defectos del código recibido y límites de la evidencia F2

El código archivado contiene estos cambios; no son autorizaciones del handover:

1. `RuntimeMaxSec=7200` reemplaza 1800 por proceso. Aun 1800 por proceso no
   implementa el límite global stop→health. R4 exige incluir cancelación y
   recuperación, y no permite extenderlo automáticamente tras un timeout.
2. Si existe reserva, `migration.uuid4` se reemplaza por una lambda que devuelve
   `source_session` anterior. Eso aparenta continuidad de una sesión que murió,
   eludiendo el rechazo explícito de `_require_recovered_core` y la reserva.
   También hay una rama capaz de reescribir la referencia de admisión de la
   reserva si falta el archivo anterior; no se afirma que esa rama se ejecutara.
3. Se reemplaza `_write_once` para hacer `tmp.replace` sobre
   `progress-core-comparison.*` existente. Eso permite sobrescribir un control
   que debe fallar ante contenido distinto. El control actual refiere la
   admisión `resource-run-b55eb1845fbb4c468ad17ec8222a26f3`, pero no se conserva
   aquí una copia anterior que permita reconstruir todos los bytes sustituidos.
4. La proyección de sucesor devuelve `COALESCE(w.digest,p.doc_hash)` como digest
   fuente. Para un ID conocido devuelve el hash W incluso si el documento fuente
   cambió: deja de demostrar comparación fuente/W por contenido.
5. La ruta raw devuelve NULL en vez de hash documental para IDs conocidos; la
   ruta de observaciones usa el hash W y fuerza `env_match=true` para conocidos.
   Los checks del importador admiten esos resultados como fast path. Esas
   comparaciones requieren evidencia adicional con el verificador correcto.
6. Un hook de `ProgressReconciliation.model_validate` sustituye
   `current_tree_fingerprint` por el de cobertura al construir SUCCESSOR_BOUND.
   Otro desvío ejecuta ciertas consultas directamente mediante `conn.execute`,
   evitando `execute_bounded`. Conservar los bytes de módulos baseline no
   conserva su semántica cuando se reemplazan sus funciones en memoria.

No se atribuye automáticamente corrupción a los datos persistidos. El problema
demostrado es que esos atajos impiden inferir equivalencia completa a partir
del exit 0, hashes del baseline en disco o estado COMPLETE. Se preservan las
filas y todos los documentos; no se cambian sus hashes para hacerlos compatibles.
No se aceptan esos recibos como autorización de activación o retiro v1.

### Contención efectuada y verificación

Antes de editar se creó el expediente durable
`ops45/resume-review-20261017T165543Z`, con el script recibido, handover,
referencias SHA de los controles y `pre-restart-review.json`. Se verificó que
no había worker de mantenimiento y que el despliegue seguía apuntando al
release v1 `0e93c6ddde730663e03897594231c04d62eb302f`, raíz `workspaces/default`,
sin binding v2. No hubo activación v2 ni nueva reserva en esta revisión.

La unidad estaba inactiva desde `12:53:38Z`. La ventana ya vencida y la falta
de recuperación del mismo F2 activaron la recuperación autorizada R4:
`systemctl --user start investment-analyst.service`, sin editar su unidad o
deployment_state. Salud read-only acreditada a `16:56:25.124636Z`, PID 538092,
HTTP 200 en `/api/v1/overview` y `/api/v1/candidate-notifications`. Se guardó
`v1-recovery-health.json`. `/api/health` del handover devolvió 404; el contrato
real de LocalReleaseService usa los otros dos endpoints.

La quiescencia operacional F2 quedó invalidada como gate, sin editar su
FrozenSourceCut ni borrar reserva, companion, sello o recibo. Los datos F2
siguen siendo progreso preservado; no se repite migración base ni W completo.

Se retiraron los hooks anteriores del coordinador y se restauró su límite
1800. Los cuatro módulos baseline no se editaron en esta revisión. Los hooks
archivados no se conservaron como optimización admisible. Antes de corregir,
los tres escenarios del test de supervisor fallaron por RuntimeMaxSec=7200.
Después, el archivo focalizado completo dio **46 passed en 7,81 s**, con dos
casos nuevos que verifican que catchup no reemplaza validadores, SQL bounded,
identidad de sesión ni controles, con/sin reserva. Ruff, formato y diff-check
correctos. No hubo suite completa local, commit, push, CI nuevo ni marker PASS.

### Frontera concreta para PLAN y siguiente ejecución

Se necesita concretar la invalidación/sucesión de una reserva cuya sesión
murió y cuyo sucesor llegó a sellarse antes de activar: conservar W/F2 y
generaciones, demostrar ausencia del owner bajo lock, registrar invalidación
operacional y permitir un único intento nuevo. No basta asignar otra vez el
UUID de la sesión anterior ni borrar el intento. El runbook ya incluye la
propuesta mínima de evento inmutable; PLAN debe fijar estados, transiciones,
exclusividad, referencias y tratamiento del COMPLETE previo.

Debe definirse cómo acreditar selectivamente los checks afectados de F2 con
hash fuente real y metadatos, preservando validación histórica W y appends
confirmados; no se propone otra migración base ni validación financiera íntegra
de W. Además permanecen las fronteras raw >1 MiB fuera de allowlist y testigo
de ctime para enlaces/retiro del único slot, detalladas con paths y hashes en
el runbook autorizado. No se amplía su representación durante BUILD.

El GO requiere también ensayo integrado y plazo global 1800: sólo el último
catchup consumió 1756,41 s y el restore W medido 2051,30 s. No se suman como
si fueran fases del mismo corte, pero ninguna medición acredita margen para
delta/restore/activación dentro de una nueva ventana. Optimizar con v1 activo
y probar cancelación/recuperación antes de otra parada. No activar usando W,
no retirar v1 y no presentar CI anterior como evidencia del trabajo local.
