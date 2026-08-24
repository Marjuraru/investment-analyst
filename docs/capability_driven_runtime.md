# Runtime por capacidades y operación durable

Esta entrega mantiene los contratos públicos históricos y añade una capa de resolución tipada por
`asset_id`, proveedor, dominio y frecuencia. AAPL continúa como adaptador fundador compatible; las
decisiones nuevas no comparan símbolos.

## Contratos y resolución

`CapabilityDrivenRuntimePlan` publica bindings `runtime-capability-binding-v1`. Cada binding contiene
la clave completa de dispatch, los `source_ids` exactos y una ruta compatible de mercado listado,
cripto spot diario, cripto spot intradía o fundamentales corporativos. El plan se deriva de
`market-asset-universe-v3` y falla antes de abrir storage o contactar un proveedor si la capacidad no
existe. `GET /api/v1/capabilities` expone este inventario inmutable.

Todo criptoactivo del catálogo debe declarar `crypto_profile`; no existe fallback para un perfil
ausente. La taxonomía analítica distingue `bitcoin`, `ethereum`, `altcoin` y `unsupported`;
stablecoins y activos wrapped se clasifican explícitamente como no soportados. Solo Bitcoin tiene
pipelines spot específicos adicionales. BTC y ETH comparten spot diario y derivados; Ethereum y
altcoins no reutilizan fórmulas on-chain ni diagnósticos spot específicos de Bitcoin.

La configuración Coinbase declara producto, `asset_id`, source ID, granularity, unidad base y unidad
de cotización. `coinbase_source_id(product_id, granularity_seconds)` conserva exactamente las
identidades BTC existentes y permite resolver otro producto sin copiar constantes de BTC. Añadir
otro producto al catálogo no activa por sí solo ingestión o análisis productivos.

La configuración Deribit exige conjuntamente currency, instrumento perpetuo y las capacidades de
funding horario, DVOL diario y snapshot. Sólo esa resolución completa habilita el job
`deribit:<asset_id>:crypto-derivatives`; una selección sin las tres capacidades no lo registra. El
job conserva dominio `crypto_derivatives`, offset +10 minutos, freshness 36h y planner rolling de 90
días. No añade rutas HTTP/UI, polling ni WebSocket. Véase
[`crypto_derivatives.md`](crypto_derivatives.md).

## Lecturas y writer

`AaplLocalController` conserva un solo mutex writer. Las cachés de lectura usan otro lock y el health
se publica como snapshot inmutable completo. Por ello `GET /api/v1/overview` no espera una descarga o
cálculo largo: devuelve `operational-overview-snapshot-v1`, sin historiales, con objetivo p95 menor
de 100 ms y payload menor de 20 KiB. `GET /api/overview` permanece compatible.

El benchmark repetible se ejecuta con el servicio activo:

```bash
.venv/bin/python scripts/benchmark_runtime_reads.py --samples 50
```

Mide p50, p95 y bytes para overview compacto, catálogo, capacidades y estado compatible. No ejecuta
proveedores ni escribe en el workspace.

## Aceptación operacional exact-SHA

La aceptación de una release candidata permanece fuera del runtime cuantitativo. El gestor de
releases expone `candidate-stage` y `candidate-update` sólo para el ref exacto
`refs/pull/<pr-number>/head`; exige PR positivo y SHA completo, verifica la carrera del ref, commit y
tree, y conserva el rollback de la release previa. `stage` y `update` siguen siendo main-only.

`scripts/observe_release_acceptance.py` implementa `release-acceptance-observation-v1` como CLI
one-shot read-only. Sus únicas fuentes son GET loopback allowlisted, `systemctl --user show` y
`/proc/<MainPID>/status`. No abre workspace, EnvironmentFile, storage, providers, scheduler ni
reconcile; no hace POST ni restart. La evidencia compacta enlaza SHA/tree/service, UTC y monotonic,
latencia p50/p95, status/tamaño/JSON válido, PID/NRestarts y RSS/HWM/swap. JSONL es append-only y el
summary es atómico; gaps, 503, restart, SHA drift y salida inválida impiden PASS. La memoria queda
como correlación observacional, sin afirmación causal.

## Operaciones manuales

Las rutas síncronas anteriores continúan válidas. La API versionada permite encolar las mismas
operaciones sin mantener abierta la solicitud HTTP:

```text
POST /api/v1/manual-operations
GET  /api/v1/manual-operations/<operation_id>
```

`manual-operation-request-v1` admite `complete_refresh`, `market_daily`, `market_intraday` y
`fundamentals`. El payload se valida contra el contrato estricto de la operación seleccionada antes
de calcular su fingerprint o persistirlo. La cola persiste
`state/manual_operation_state_v1.json`, deduplica solicitudes equivalentes solo mientras estén
`queued` o `running`, usa un único worker y el mismo writer de la fachada. Tras reiniciar, una
operación que quedó `running` vuelve a `queued` con `recovery_count` incrementado y conserva su
identidad. El reloj auditado se mantiene no decreciente aunque el reloj de pared retroceda, por lo
que el worker no se detiene. Los resultados conservan el schema y los conteos de la operación; la
cobertura y trazabilidad solo se publican cuando la respuesta permite derivarlas, y `null` significa
no evaluable. Los errores usan mensajes fijos, y las claves que parecen credenciales se rechazan
antes de persistir.

## Registro reconciliable

El registro de jobs ya no queda fijado durante todo el proceso. Las preferencias persistentes
seleccionan activos y el scheduler publica atómicamente un nuevo tuple de jobs construido desde este
plan y el catálogo, sin ejecutar proveedores. Cada tick conserva su snapshot; retirar un job no
cancela el callback activo y reactivarlo reutiliza el mismo `job_id` y su historia. Los contratos y
la precedencia están en [`asset_preferences.md`](asset_preferences.md).

## Telemetría

Cada intento nuevo del scheduler añade `provider-job-telemetry-v1`: job, proveedor, dominio,
timestamps, duración, conteos creados/reutilizados, cobertura o categoría de fallo. Llamadas y bytes
quedan `null` cuando el transporte actual no puede medirlos con precisión; no se estiman. Estados
históricos sin este campo siguen cargando.

## Backup y restore

`WorkspaceBackupService` crea un directorio temporal bajo coordinación del writer, inventaría todos
los archivos regulares relevantes, verifica tamaño y SHA-256, registra los cuatro conteos de storage
y solo entonces publica el destino. Locks y temporales no se incluyen. Una copia incompleta nunca
aparece en la ruta final. Tanto la creación como la verificación rechazan cualquier enlace simbólico,
incluido un enlace interno que apunte fuera del workspace o backup.

El CLI toma el mismo lock de proceso que el servicio local; si el servicio está activo, rechaza el
backup. Debe detenerse de forma ordenada antes de copiar para que ningún writer externo pueda abrirse
durante el snapshot.

```bash
.venv/bin/python scripts/backup_workspace.py \
  --workspace /ruta/workspace \
  --destination /ruta/backup

.venv/bin/python scripts/restore_workspace.py \
  --backup /ruta/backup \
  --destination /ruta/workspace-restaurado
```

Restore acepta únicamente un destino nuevo o vacío. Antes de activarlo verifica el manifest, formato
v1, inventario, hashes, workspace UUID y conteos mediante `WorkspaceService.inspect`. Nunca
sustituye el workspace de origen. No hay nube, compresión nueva, dependencia ni migración de schema.

## Riesgos pendientes

- El mutex writer sigue siendo único: una cola de proveedores lenta retrasa otras escrituras, aunque
  ya no bloquea el overview compacto.
- Las rutas síncronas compatibles aún pueden mantener una conexión HTTP durante un refresh; los
  clientes nuevos deben preferir la cola versionada.
- La telemetría de llamadas y bytes requiere instrumentación explícita por transporte para dejar de
  ser desconocida.
- BTC y ETH disponen de spot diario y derivados productivos; intradía y análisis spot específicos de
  Bitcoin no se generalizan implícitamente a otros perfiles.
