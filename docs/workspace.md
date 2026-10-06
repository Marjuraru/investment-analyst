# Workspace local y staging de datos

`investment-analyst` conserva evidencia financiera en un workspace local con
identidad propia. El formato v1 sigue siendo el default. Un manifiesto puede
seleccionar explícitamente el backend v2 para un workspace nuevo; seleccionar
ese formato no migra ni activa automáticamente un workspace existente.

## Directorios y autoridad

`WorkspaceService` resuelve e inicializa el workspace explícito o configurado,
su manifiesto y los directorios de storage, exports y estado. La composición de
la aplicación usa `ApplicationRuntime` y las rutas resueltas por el servicio;
el comportamiento no depende del directorio actual del proceso.

## Backend seleccionable v2

`WorkspaceService.initialize(..., format_version=2)` crea un manifiesto v2 y
`ApplicationRuntime` abre el backend indicado por ese manifiesto mediante las
mismas interfaces `LocalStorage` y repositorios. La API pública y el formato
default no cambian. El índice DuckDB vive en `storage/v2/index.duckdb`; los
blobs raw content-addressed viven bajo `storage/v2/raw/`, y exports y documentos
conservan sus rutas del workspace. El backend tipado guarda observaciones,
métricas y diagnósticos sin duplicar el modelo completo en `document_json`.

La representación compacta conserva Decimal como texto exacto, parámetros,
identidades y lineage ordenado por referencias content-addressed y segmentos de
hasta 256 miembros. El archivo histórico mantiene un sello append-only separado
de las nuevas escrituras `LIVE`. El backup usa
`workspace-v2-backup-manifest-v1`, separado de los manifiestos de staging raw.
El backend v2 admite acceso read-only/read-write; seleccionarlo no implica
cutover, importación del workspace permanente ni rollback productivo.

El workspace permanente puede contener historia local valiosa. La inspección
debe ser read-only cuando no se requiere escribir. Los procesos de staging
reciben un destino nuevo, disjunto de la fuente, y una única conexión escritora.
Los importadores y servicios de backup usan las abstracciones de storage; no
editan directamente filas del workspace de origen.

## Importación y archivo al backend v2

El staging `raw-v2-staging-v1` mantiene blobs raw con identidad SHA-256 y un
índice DuckDB tipado. `normalized_observations_v2` conserva las observaciones
normalizadas con Decimal como texto exacto y una referencia verificable al raw.
Las importaciones por páginas tienen un máximo de 256 modelos. Cada página se
escribe idempotentemente, se relee y verifica antes de que el checkpoint
confirme el cursor y digest. La reapertura comprueba la fuente, el staging, los
digests y el prefijo durable antes de reanudar.

El archivo histórico analítico conserva métricas y diagnósticos v1 ya
persistidos en tablas tipadas separadas. Conserva sus UUID y contenido
completo, incluyendo parámetros de ejecución; representa Decimal sin pérdida y
almacena componentes, citas y enlaces como relaciones ordenadas. Los enlaces
compartidos usan secuencias y segmentos content-addressed de hasta 256 miembros.
La verificación recorre inventarios por páginas y valida referencias PIT y el
DAG; `verify_complete()` es una comprobación read-only.

Los importadores raw, de observaciones e histórico ligan sus estados al mismo
workspace de origen, fingerprint y `staging_id`. Los estados son portables con
el backup y permiten reanudar tras restaurar a otra ruta. Un contenido durable
escrito justo antes de la confirmación queda disponible para reutilización
idempotente en la siguiente ejecución.

## Backups de staging y restauración

`RawV2StagingBackupService` publica un inventario con rutas relativas, tamaños,
SHA-256, `staging_id`, checkpoints y conteos verificados. Cada nueva familia
tipada amplía el manifiesto de manera versionada. El manifiesto v6 liga el
archivo de métricas/diagnósticos, su checkpoint y sus digests con los
inventarios raw y de observaciones. Si no existe archivo histórico, se conserva
la versión anterior apropiada; los manifiestos v1–v5 siguen siendo legibles y
restaurables.

Para un workspace v2, `WorkspaceV2BackupService` inventaría los archivos no
transitorios y los liga en `workspace-v2-backup-manifest-v1`. Verifica el sello
histórico y los repositorios v2 antes y después de restaurar. El restore copia
a un destino nuevo y vuelve a validar checksums, identidad del workspace y
conteos antes de declararlo listo.

La restauración copia a un destino nuevo, verifica de nuevo cada archivo y la
estructura tipada, y sólo después promueve el directorio temporal al destino.
Para un checkpoint parcial verifica todas las filas históricas durables y el
prefijo confirmado; los enlaces de páginas aún no confirmadas pueden seguir
pendientes. Para un estado completo compara todos los conteos, cursores,
digests, checksums y relaciones antes de promoverlo. Una discrepancia falla
cerrado y no reemplaza un destino preexistente.

## Límite de activación

La selección del formato v2 no modifica ni borra historia v1, no rebautiza
resultados históricos como resultados `LIVE` ni acredita reducción de espacio
en el host. Migración masiva, comparación bidireccional, cutover, rollback
productivo y cualquier limpieza requieren sus propios criterios, backup
verificado y autorización explícita. Las pruebas de smoke usan workspaces
temporales y no consultan proveedores externos.
