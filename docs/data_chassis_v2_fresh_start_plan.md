# V2 nuevo: arranque, aceptación y medición

Decisión humana de 2026-10-21, Work Block DATA-CHASSIS-45 / Issue #345 / PR #346.
Estado observado 2026-10-22: v2 activo en la release `daad58d`; #346 MERGED en `b5e714a`; UUID
`70c64668-1738-4dfe-ba1f-7e72026bb7fe`; DB `281.817.088 B`; coverage200 sin writes. El candidato de
cierre incorporó F11 (enum contractual del comparador) y F12 (colector v2 resuelto por manifest).
Scheduler habilitado con13 jobs/3 activos; 2 jobs13F incomplete por backlog contractual y FRED
external-unavailable. Replay local de5 familias con ventana90d y digests exactos:0 creadas/6.996
reutilizadas, DB+WAL0; las14.758 métricas de un ensayo full-range distinto se conservan como carga
distinta, no como idempotencia PASS. El punto recuperable c0080d96 fue restaurado de forma
independiente sobre `252.456.960 B` (SHA-256 `bebdd299...`); el delta posterior de `29.360.128 B`
requiere un backup nuevo. v1/F5 intactos. La autoridad ejecutable es el Issue activo, su manifest y
los guards vivos; los recibos históricos mantienen sus propios bindings.

## Estado de cierre 2026-10-22

Este plan describe la ejecución histórica de 45. El estado vigente, el backup/restore c0080d96 y la
secuencia obligatoria 46→47→48→49 están en el
[registro de continuidad](data_chassis_continuity_plan.md).

El punto de reanudación es mantenimiento del v2 activo, no otra inicialización ni un sexto cutover.
F11 debe demostrar HTTP AAPL+AMD200 con fachada real, PIT/lineage y sin writes; F12 debe contrastar
el delta DB/WAL real por intento con la medición del colector v2 y un replay sin cambios. La
aceptación conserva los fallos históricos77.594.624/48.234.496B como incumplimiento del criterio
anterior, no como instrucciones. El backup final c0080d96 se fijó tras las pruebas mutantes con
inventario/corte T y comparación de UUID, conteos, digests y últimas páginas; fue restaurado de forma
independiente. El delta posterior requiere un backup nuevo en 47. Después se reanudan servicio y
scheduler y se declara cualquier delta posterior.

Main sigue siendo la autoridad de lo integrado.

## Objetivo y decisión

Crear un workspace v2 nuevo, probarlo con adquisición reciente y warm-up mínimo suficiente,
activarlo mediante el servicio de releases y operar con observabilidad por intento. Conservar v1
y F5 parcial como fuentes históricas; planificar su incorporación selectiva sin convertirla en
requisito del arranque. No borrar datos para empezar ni activar el restore-v2 parcial.

El bloque ADVANCES DATA-CHASSIS, que permanece NEXT. No declara Q1–Q7 satisfechos comparando una
base vacía con toda v1; no cambia el protocolo ni el registro canónico. FUNDAMENTALS-COVERAGE
permanece PLANNED. Después: archivo/inventario histórico → piloto de incorporación selectiva →
lotes necesarios y cierre de los objetivos aún pendientes. Son fases futuras, no Issues ya creados.

## Contratos reutilizados y compatibilidad

- WorkspaceService.initialize(..., format_version=2), manifest con UUID nuevo, ApplicationRuntime,
  LocalStorage y repositorios v2. Un v2 sin transición histórica pendiente puede funcionar vacío;
  no fabricar sello histórico ni receipt de migración COMPLETE.
- LocalReleaseService, DeploymentWorkspaceBinding, candidate-stage/candidate-update y recuperación
  forward-v2 existentes. Un release candidato se adquiere por refs/pull/346/head y SHA/tree exactos;
  no se hace pasar por main ni por release44. CI verde del SHA que se ejecuta es obligatoria.
- WorkspaceV2BackupService y transporte cifrado remoto existente para el workspace NUEVO; los
  manifests viejos no acreditan cobertura de los datos nuevos ni de las últimas semanas de v1.
- Preferencias, planner incremental, scheduler, observaciones por intento, reports y observer HTTP
  existentes. No esquema SQL, proveedor, algoritmo, identidad o fórmula nuevos.
- Única ampliación CLI planificada: init_workspace.py --format-version {1,2}; sin argumento conserva
  exactamente el comportamiento previo, incluido reutilizar un manifest existente compatible.
  Elegir explícitamente 2 sobre un v1 existente falla sin convertirlo ni escribirlo.
- Reutilizar contratos de evidencia existentes. No nuevo framework de migración, daemon de métricas,
  flags que desactiven integridad ni protocolo paralelo de activación. Correcciones estrechas en
  repositorios/lectores/observabilidad/releases sólo para defectos reproducidos de este recorrido.

## Raíces y capacidad operativa

Raíz nueva exacta: ~/.local/share/investment-analyst/workspaces/active-v2-20261021.
Controles nuevos: ~/.local/share/investment-analyst/ops/data-chassis-45/fresh-start-20261021.
Un solo slot de restore nuevo: mismo ops/fresh-start-20261021/restore-probe.
Si alguna raíz existe, comprobar UUID y receipt propio; nunca reutilizar contenido ajeno, vaciarla,
renombrar default ni crear sucesivos slots para evitar un error.

Conservar default v1, restore-v2/F5, ops de los intentos, backups remotos y controles. No importar
outbox, cursores o intentos antiguos como si fueran intentos nuevos. Reaplicar sólo preferencias
vigentes mediante sus servicios públicos; identidad/fecha/procedencia y ausencias se registran.

Antes de abrir DB real o adquirir datos: presupuesto nativo explícito en connect, un writer,
cgroup mantenimiento High2684354560/Max3221225472/Swap0, SQL120s/open180s y spill agregado≤1GB;
1GB DuckDB por instancia, agregado≤2GB. Runtime: comprobar límites efectivos antes de habilitar
scheduler; DuckDB≤2GB, threads≤2, spill≤1GB y cgroup sin límite infinito. Mantener estos techos,
no elevarlos por OOM. Configurar mediante mecanismos existentes y verificar padres/margen para v1
mientras coexista la prueba; serializar fases pesadas. C: libre≥max(25GB,5GB+crecimiento máximo
siguiente unidad+spill+staging). Un dato ausente no es cero ni permiso para escribir.

## Secuencia de arranque y evidencia conservada

Pasos1–7 ya tienen entregas/recibos en5fb973d: comprobarlos, no reinicializar ni repetirlos por
rutina. Las brechas de aceptación siguen pendientes. Reanudar por la enmienda de estabilización.


1. **Preflight y archivo lógico.** Guard, SHA/árbol/entorno, bindings del servicio, inventario de
   capacidades configuradas, preferencias y jobs reales. Verificar checkpoint F5/hash y controles
   existentes sin reparar el sello. Crear índice de preservación por raíz/UUID/manifest/receipt,
   cobertura remota conocida y delta aún no respaldado. Ninguna pérdida ni retiro; no exigir otro
   restore completo de v1 para poder probar una base nueva.
2. **Implementación mínima y matriz.** CLI opt-in y pruebas de arranque/backup/restart. Matriz por
   consumidor público y capacidad: inputs/activo/fuente/rango/known_at, salida esperada, cobertura,
   paginación, traza y coste. Partir de catálogo y jobs configurados, no de los tests que existan.
   Listar también capacidades no aplicables/externamente indisponibles; no declararlas probadas.
3. **V2 nuevo con v1 activo.** Inicializar raíz nueva. Ejecutar adquisiciones mediante fachadas y
   clientes oficiales existentes, secuencialmente, sin habilitar una descarga general. Incluir una
   ejecución real de cada familia/proveedor configurado disponible; todos los consumidores se
   prueban además con datos deterministas. Selección reproducible desde watchlist/capabilities;
   no privilegio AAPL ni activos nuevos. Registrar rango reciente + mínimo warm-up requerido por
   cada contrato; no asumir que 30 días bastan ni usar --full indiscriminadamente. SEC se consulta
   en cada bootstrap; mantener revisión/PIT. Una respuesta vacía válida se distingue de fallo.
4. **Pruebas completas de comportamiento.** full/incremental exactos para mismos inputs; no-change
   con reloj distinto; 0/1/3 novedades y revisión histórica; reinicio/reanudación; fallo de proveedor
   sin perder etapas previas; lectura PIT pasada/actual; todas las páginas y exportaciones; lineage
   hasta raw/documentos; aislamiento entre fuentes/dominios. Pruebas pequeñas de fallo en scratch,
   no corrupción inyectada en datos reales. Ausencia de warm-up se muestra, no se rellena con ceros.
5. **Eficiencia de lectura/escritura.** Aplicar los criterios de la sección siguiente sobre la
   misma matriz. Medir primero, corregir sólo los caminos que los incumplan y repetir esos casos.
   Una tanda focalizada, Ruff y una CI del candidato cohesivo. No suite local completa duplicada.
6. **Recuperabilidad nueva.** Con writer nuevo cerrado, backup de datos nuevos, transferencia
   cifrada existente, restore independiente en único slot y lectura por el runtime exacto.
   Comparar inventario completo/digests por familia, documentos y estado operacional; demostrar
   que se leen también últimas páginas. Si hay dato nuevo después del backup, actualizar la
   evidencia del punto recuperable antes de proclamarlo. No usar parent W2 como sustituto.
7. **Activación controlada del workspace nuevo.** Sólo con pasos anteriores, CI exacta y startup
   ya probado: capturar intención, release anterior, binding, UUID nuevo y recuperación; parar
   normalmente el único servicio v1, conservar su raíz cerrada y arrancar el release candidato
   verificado en el workspace nuevo por LocalReleaseService. Inicialmente scheduler deshabilitado
   mediante configuración pública; validar HTTP/PID/root/UUID/release, después habilitar los jobs
   declarados gradualmente. No otro F ni sexto cutover de migración. Autoriza UNA activación nueva;
   presupuesto global≤5400s con límites de readiness efectivos y recuperación reservada, sin usar
   el GO de F5 ni inventar observaciones. Abortos pre-writer pueden volver a v1 bajo guard; después
   de posible escritura v2 la recuperación conserva v2, nunca rollback ciego a v1. Un fallo previo
   al stop se corrige dentro del scope sin nueva consulta; una activación abortada no autoriza bucle.
8. **Aceptación operacional finita.** Un lote ordinary y uno same_inputs con mismos inputs/versiones/
   parámetros y reloj distinto; refresh/restart/HTTP mientras se consulta; prueba de nueva evidencia
   vía ejecución manual del job normal, sin esperar días. Cada intento tiene resultado terminal,
   evidencia y telemetría; fallos externos visibles, no contados como éxito. Reusar los dos lotes
   para crecimiento/memoria/latencia/reutilización. Observación posterior queda funcionando sin
   convertir una semana de espera en gate. Entregar BUILD, AUDIT independiente y política HUMAN.

No ejecutar datos reales desde código que luego cambie sin registrar el productor. Una corrección
posterior exige nuevo candidato/CI y repetir sólo evidencia invalidada. No editar release instalado.
La integración final utiliza el mismo workspace/UUID; nunca abre otra raíz para aparentar limpieza.

## Leer sólo lo necesario, pero leerlo completo

Filtro por activo/fuente/familia/frecuencia/rango/available_at y proyección de columnas en storage
ANTES de hidratar modelos. IDs y metadatos para listar; cargar raw, documentos o explicación sólo
si el consumidor los necesita. Leer dependencias transitivas y warm-up necesarios aunque estén
fuera del intervalo visible; registrar por qué. No imponer LIMIT que trunque una respuesta completa.

Páginas≤256, keyset estable, sin saltos/duplicados y con final verificable. En pruebas, el total de
IDs/digests devuelto por TODAS las páginas coincide con el universo esperado; probar 0,1,256,257,
última página, empate temporal, revisión y concurrente según snapshot contractual. Cero hidratación
de modelos ajenos; cero descarga por GET read-only y cero escaneo de archivo v1 en consultas v2.

Prueba de escala aislada: misma consulta relevante con corpus ajeno 1× y 10×; comparar filas
materializadas/bytes de payload/consultas. Modelos ajenos=0 y no hay N+1 por elemento; plan SQL,
filas examinadas e I/O se registran separadamente. Un scan de columnas indexadas no se confunde
con cargar documentos, pero crecimiento físico que haga exceder la cota requiere corregir pushdown.
Digest completo y hash raw se mantienen; cache acotada a sesión/UUID/fingerprint, invalidada ante
revisión. No cache stale para bajar latencia ni precargar todo el universo.

Escritura: un writer, lotes existentes, durable antes de checkpoint, idempotencia por identidad.
No comparar tablas completas por cada fila; verificación global sólo en backup/aceptación explícita.
Lectura concurrente conserva snapshot coherente. Sin nuevos esquemas, índices persistidos o APIs
financieras como atajo; cualquier necesidad demostrada de ellos vuelve a PLAN con caso concreto.

## Medición y criterios del arranque

Denominador: matriz completa de capacidades/consumidores del alcance, no todo el histórico v1.
“Completo” significa salida íntegra dentro de cada solicitud y cobertura declarada, no toda la
historia descargada ni rendimiento perfecto a cualquier volumen.

- 100% de casos requeridos de matriz con resultado/evidencia; ninguna ausencia convertida a PASS.
- Cero divergencias Decimal/PIT/IDs, pérdida de página o duplicados equivalentes en same_inputs.
- Cero modelos ajenos hidratados, cero accesos a v1 por consultas nuevas y cero OOM/oom_kill/max
  nuevos; pico cgroup≤floor(0,9×MemoryHigh). No extrapolar RSS de una consulta al servicio completo.
- Dos escenarios según enmienda: adquisición ordinary acotada con delta DB+WAL íntegro y
  recursos medidos; replay local con inputs idénticos, delta positivo DB+WAL≤30.000.000B y cero
  métricas equivalentes nuevas. Los dos excesos previos siguen incumpliendo el criterio anterior.
  No es evidencia de B/día ni cambio del Q4 global; no excluir13F ni confundir fecha con inputs.
- Registrar duración por fase/job, p50/p95 HTTP con número de muestras y escenario, filas leídas/
  hidratadas, bytes físicos/representación, cache hits/misses cuando exista contador, cgroup/spill y
  bytes escritos. Ausente=UNKNOWN. Para lecturas técnicas conservar cotas ya contratadas por tests;
  comparar v2 antes/después con mismos inputs/entorno, no fijar “80% mejor” por borrar historia.
- Reusar colector por intento, reporte storage y observer HTTP. No recorrer corpus completo por
  request ni añadir polling de DB constante. Medir overhead en el ensayo y publicar coste; cualquier
  nueva telemetría debe ser acotada y separada del dato financiero, sin nuevos demonios.

Q1–Q7 históricos siguen en su registro: resultados no comparables conservan UNKNOWN. El arranque
no cierra DATA-CHASSIS ni borra los gates globales del protocolo.

## Limpieza y continuidad

Antes de borrar, lista literal de paths+bytes+último consumidor+recuperabilidad+receipt; usar lifecycle
existente, nunca glob/rm/manual SQL. Este arranque NO autoriza retirar default, F5 o controles.
Sólo staging NUEVO propio regenerable tras recuperación probada puede retirarse en este bloque.
La recuperación física de C: no se infiere de du/TRIM; compactación WSL es otra operación.

El inventario y las reglas de selección histórica están en data_chassis_v1_reintegration_plan.md.
El runbook45 contiene únicamente la ejecución de 45; los cinco intentos antiguos están archivados.

## Enmienda de estabilización — estado verificado 2026-10-21

Esta enmienda continúa el mismo bloque. El punto de reanudación es v2 ACTIVO, no init/cutover.
PR346 head 5fb973d9883ace4c4d55cf39bdc587e0b8dc66bc; CI39667169223 SUCCESS (38m05s).
UUID70c64668-1738-4dfe-ba1f-7e72026bb7fe, raíz active-v2-20261021, v1 detenido e intacto,
F5 preservado. Scheduler deshabilitado. Backup cifrado pre/final transferido y restore independiente
con UUID/conteos iguales según receipts: final raw226992/observaciones17332/métricas7703/diagnósticos6.
Evidencia: ~/.local/share/investment-analyst/ops/data-chassis-45/fresh-start-20261021/
activation-receipt.json, consumer-probe.json, build-blocker.json y remote-restore-final.json.
Son hechos del candidato, no integración en main, ni BUILD PASS. No repetir OAuth, inicialización,
adquisición inicial o restauración histórica. Reutilizar bindings válidos; registrar por qué una
prueba necesita repetirse y acreditar en el nuevo SHA todo gate afectado.

### Trabajo único y secuencia restante

1. **FIX-READ-ONLY.** Reproducir RED en DB v2 en disco abierta read_only=True y en transacción
   read-only: find/find_bundle invocan ensure y éste ejecuta CREATE. Separar lectura de creación
   dentro de ExecutionReuseV2Store. Las búsquedas sólo consultan; ensure/save/save_bundle conservan
   inicialización en escritor autorizado. Para tablas opcionales ausentes devolver cache miss sin
   DDL; para layouts incompletos no devolver un hit ni fabricar outcome/resultados. Mantener
   compatibilidad de ausencia de outcomes y validación existente; no capturar genéricamente todos
   los errores SQL como miss. No alterar schema, IDs, keys, Decimal, available_at o semántica de
   conflicto. No abrir otra conexión writable desde GET, ni inicializar globalmente para ocultarlo.
   Contrato REUTILIZADO: cache opcional de ejecución, identidad por key existente y UUID del workspace;
   corrección de acceso, ninguna nueva representación del dominio. El caller atraviesa analytics,
   pero eso NO autoriza editar analytics: la corrección queda en storage. Tres paths añadidos a
   allowlist: implementación, unit test e integración application del recorrido de coverage.
   Regresiones: hit/miss individual y bundle, tablas ausentes, PIT antes/después, reloj distinto,
   orden íntegro de IDs y ausencia de escrituras; save/conflicto siguen funcionando. La integración
   usa workspace2 y evidencia suficiente para alcanzar valoración→lookup_bundle, no sólo un
   universo vacío que salte esa rama. No mockear find_bundle para declarar la regresión resuelta.
2. **Consumidores y verdad de cobertura.** Tras fix, GET universe-coverage sobre el UUID activo debe
   responder200 con contenido/traza/PIT correctos y sin writes. Repetir el probe de consumidores.
   market-comparison devolvió400; probe PLAN read-only confirmó invalid_request: “comparison
   requires a visible daily market asset”. No presentarlo como PASS ni cambiar el contrato para
   admitir evidencia inexistente. Resolver visibilidad/known_at/warm-up con datos existentes y
   adquisición mínima ya autorizada; conservar el negativo400 y demostrar solicitud soportada
   con evidencia visible200. Si aparece un defecto distinto, adjuntar reproducción y archivo,
   sin ampliar por anticipado frontend/analytics/providers. Derivados tardó32,83s: medir repetición
   acotada con misma carga, reportar muestras/recursos; no inventar un objetivo porcentual.
   Matriz: 102 jobs inventariados y 10 casos representativos, no 102 ejecuciones exitosas. Cada job
   debe mapear a caso/familia/cobertura y estado; cada consumidor requerido tiene evidencia propia.
   FRED/ALFRED sigue external-unavailable con fallo visible, sin PASS ficticio ni cambio provider.
   Esa indisponibilidad ya contemplada en F02 no bloquea familias independientes; no declarar
   cobertura macro nueva. Registrar error saneado/última cobertura y tarea futura concreta.
3. **F07: decisión explícita de denominador y crecimiento.** Los lotes previos de 77.594.624 y
   48.234.496 B excedieron el criterio anterior de30.000.000B/lote; conservarlos como incumplidos
   respecto de ese criterio. Same-date NO es same_inputs. Los223339 checksums únicos no prueban
   por sí solos ausencia de duplicación semántica ni igualdad de inputs. No eliminar 13F/Deribit,
   filtrar casos caros, restar evidencia nueva del delta observado ni reescribir receipts.
   Para el arranque se sustituye explícitamente el gate de dos lotes≤30MB por DOS escenarios:
   (a) adquisición ordinary acotada por matriz/rangos/warm-up/cursores existentes, midiendo delta
   positivo DB+WAL completo y por familia, bytes/registro/filing, revisiones, inputs y recursos;
   no límite fijo30MB cuando se incorpora evidencia necesaria nueva. Antes de escribir declarar
   el conjunto/rango/límite de páginas contractuales y presupuesto de espacio de la siguiente
   unidad; sin descarga histórica general, bucles de descubrimiento ni levantar recursos.
   (b) replay real local de inputs ya persistidos, sin adquisición nueva, mismo digest de evidencia,
   parámetros/algoritmos/UUID y reloj posterior: delta positivo DB+WAL≤30.000.000B y cero métricas
   equivalentes adicionales, con trazabilidad completa. Usar fachadas analíticas existentes;
   nombrarlo replay local, no job de provider exitoso ni prueba de descarga externa. Cubrir todas
   las familias disponibles de la matriz, incluidas13F y derivados; distinguir raw no duplicado,
   idempotencia de observaciones y reutilización analítica donde aplique. Si no hay replay de una
   familia, la evidencia sigue pendiente, no se quita del denominador. SEC continúa consultándose
   en cada bootstrap real; un replay analítico local no es bootstrap. No usar relojes de octubre17
   como adquisición efectiva de datos consultados octubre21: auditar esa procedencia en lotes
   existentes; no modificar historia, acreditar PIT con available_at verificable y reloj UTC real.
   Inventario, digest, versiones, conteos antes/después y DB/WAL se guardan por escenario. Ambos
   conservan techos de memoria/spill/disco y cero OOM. Esta decisión limita la aceptación de
   arranque, NO modifica el objetivo global Q4 ni acredita ahorro sobre v1 o B/día. Los registros
   globales no comparables siguen UNKNOWN; los fallos físicos observados permanecen explícitos.
4. **Nuevo candidato y actualización del servicio.** Ruff/pruebas focalizadas (incluidas las tres
   regresiones declaradas), diff-check y CI Python3.12 quality del nuevo SHA. Autorizar por servicios
   existentes candidate-stage/candidate-update del candidato corregido sobre MISMA raíz/UUID v2,
   con parada ordenada, un writer, límites efectivos y forward recovery. Esto es mantenimiento
   de v2 activo, no segunda inicialización ni sexto corte F. No editar un release instalado.
   Si hay fallo corregible dentro del scope, FIX y nueva validación del candidato; no solicitar
   otra autorización por cada iteración de ese fix. Sin bucles ciegos de restart ni pérdida de writes.
5. **Comisionado acotado.** Con lectura sana y recursos verificados, habilitar gradualmente jobs
   configurados mediante configuración pública existente. Registrar conjunto habilitado/excluido,
   motivo y siguiente ejecución; observar despacho real y resultado terminal sin doble writer y
   HTTP concurrente. No disparar102 jobs simultáneos para “probar todo”. No llamar scheduler activo
   a una colección manual mientras --no-scheduler sigue vigente. Fallos externos visibles sin
   bucle de reintentos; usar backoff existente. Si no existe control por job, utilizar la selección
   pública soportada, no introducir un scheduler nuevo. Prueba finita, sin esperar días. Backup:
   conservar recibos previos; si cambió el inventario, actualizar punto recuperable cifrado y
   readback independiente antes de PASS. No repetir toda v1 ni crear nuevos slots/cuentas.
6. **Cierre documental y handoff.** Actualizar cabeceras fresh_start/runbook45/closure y reflejar
   candidato activo, UUID, scheduler, FRED, lotes históricos fallidos, nuevos escenarios y evidencia
   faltante en una única receta. Reconciliar README/roadmap/ruta/readiness si contradicen ese estado;
   no reescribir cronologías archivadas ni Q1–Q7. Renovar marker BUILD propio con digest del manifest
   enmendado; PLAN no lo modifica. Mapear F01–F10/I01/N01–N03 a evidencia del SHA candidato, repetir
   sólo gates afectados, ningún PASS heredado. BUILD PASS→AUDIT fresco→receipt HUMAN posterior.

No hay cleanup de datos autorizado. Sólo staging nuevo propio, con path literal, bytes, último
consumidor y restore verificado conforme lifecycle existente; ante duda conservar. No VACUUM,
borrado raw/13F, checkpoints, backups ni v1/F5 para maquillar crecimiento. Optimización adicional
sin defecto probado queda fuera; el objetivo restante es lectura correcta, operación acotada y
aceptación honesta, no rediseño de storage o reimportación de semanas.
