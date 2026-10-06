# Workspace local y staging de datos

`investment-analyst` conserva evidencia financiera en un workspace local con
identidad propia. Su almacenamiento v1 sigue siendo la fuente de lectura de la
aplicación. Los formatos v2 de esta etapa son staging aislados para importar,
comparar y respaldar; no se activan como workspace productivo.

## Directorios y autoridad

`WorkspaceService` resuelve e inicializa el workspace explícito o configurado,
su manifiesto y los directorios de storage, exports y estado. La composición de
la aplicación usa `ApplicationRuntime` y las rutas resueltas por el servicio;
el comportamiento no depende del directorio actual del proceso.

El workspace permanente puede contener historia local valiosa. La inspección
debe ser read-only cuando no se requiere escribir. Los procesos de staging
reciben un destino nuevo, disjunto de la fuente, y una única conexión escritora.
Los importadores y servicios de backup usan las abstracciones de storage; no
editan directamente filas del workspace de origen.

## Importación al staging v2

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

La restauración copia a un destino nuevo, verifica de nuevo cada archivo y la
estructura tipada, y sólo después promueve el directorio temporal al destino.
Para un checkpoint parcial verifica todas las filas históricas durables y el
prefijo confirmado; los enlaces de páginas aún no confirmadas pueden seguir
pendientes. Para un estado completo compara todos los conteos, cursores,
digests, checksums y relaciones antes de promoverlo. Una discrepancia falla
cerrado y no reemplaza un destino preexistente.

## Límite de activación

El staging no cambia el workspace activo, no modifica ni borra historia v1, no
rebautiza resultados antiguos como resultados v2 y no acredita reducción de
espacio en el host. Migración masiva, comparación bidireccional, cutover,
rollback productivo y cualquier limpieza requieren sus propios criterios,
backup verificado y autorización explícita. Las pruebas de smoke usan
workspaces temporales y no consultan proveedores externos.
