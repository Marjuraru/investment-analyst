# V2 nuevo: arranque, aceptación y medición

Decisión humana de 2026-10-21, Work Block DATA-CHASSIS-45 / Issue #345 / PR #346.
Estado: PLAN aprobado como enfoque; implementación y operación pendientes. Este documento NO
acredita activación. Sustituye la receta de completar A→B→F antes de arrancar v2.
La autoridad ejecutable es el Issue activo, su manifest y los guards vivos; los recibos históricos
mantienen sus propios bindings. Main sigue siendo la autoridad de lo integrado.

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

## Secuencia única BUILD

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
- Dos lotes: delta positivo DB+WAL≤30MB por lote, sin netear bajas ni llamar B/día a B/lote. Una
  descarga inicial y su warm-up se miden aparte y no se presentan como lote de reutilización.
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
El runbook45 contiene únicamente la ejecución vigente; los cinco intentos antiguos están archivados.
