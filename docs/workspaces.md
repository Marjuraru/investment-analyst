# Persistent workspaces

A workspace gives application commands one durable, explicit location for local data. It centralizes
path resolution, layout validation, manifest compatibility, and safe `LocalStorage` opening. Analysis
services remain unchanged and still use the existing storage contracts.

## Layout

Each initialized workspace contains:

```text
<workspace>/
├── manifest.json
├── storage/
├── exports/
└── state/
```

`storage/` is passed to the existing `StoragePaths.from_root(...)` API. Its current internal raw,
processed, DuckDB, and Parquet-export layout is not migrated or renamed. `exports/` and `state/` are
reserved workspace-level directories for future application services.

`state/asset_preferences_state_v1.json`, cuando existe, conserva la watchlist, favoritos y refresh
programado como revisiones auditables. Al aproximarse a 1 000 revisiones o 4 MiB, los segmentos
históricos hash-bound viven en `state/asset_preferences_state_v1_archives/`; forman parte del mismo
estado lógico y del inventario verificado de backup/restore. Su ausencia es válida: el servicio usa
una semilla CLI en memoria y no escribe automáticamente el workspace. Este estado no modifica
DuckDB, Parquet ni el manifest v1.

## Resolution precedence

Workspace resolution is deterministic and does not use `/tmp` by default:

1. An explicit `--workspace` path.
2. `INVESTMENT_ANALYST_WORKSPACE`.
3. `$XDG_DATA_HOME/investment-analyst/workspaces/default`.
4. `~/.local/share/investment-analyst/workspaces/default`.

Configured paths must be absolute after `~` expansion. A relative environment value is rejected, so
commands do not silently depend on the current working directory. Tests may pass an explicit
`tmp_path`.

The application does not read `.env`. Workspace files never contain API keys, credentials, request
headers, or other secrets. Different users or workflows can select different paths without rigid
profiles such as development, staging, or production.

## Manifest

`manifest.json` identifies the application, a stable workspace UUID, format version `1`, and its UTC
creation timestamp. It is canonical UTF-8 JSON with a final newline and is installed through a
same-directory temporary file and replacement. Absolute paths and secrets are deliberately absent.

A compatible manifest is reused. A malformed manifest, another application name, or an unsupported
format version fails explicitly and is never overwritten automatically. Initialization writes the
manifest only after existing storage initialization succeeds, so a failed setup does not leave a
manifest that appears complete.

## Read-write and read-only access

Read-write mode preserves the existing behavior: directories may be created and the current DuckDB
schema may be initialized. Only one process should act as the writer. DuckDB does not provide a
multi-process writer guarantee for this application, and no custom lock file is introduced.

Read-only mode first verifies that the database already exists, then opens DuckDB with the physical
configuration compatible with the process-local writer and begins an explicit `READ ONLY`
transaction before schema validation or queries. It does not create the database, tables,
migrations, directories, raw files, or Parquet exports, and SQL mutation is rejected by that
transaction. This allows a reader to retain its committed snapshot while one writer in the same
process is active; it neither broadens the single-writer rule nor guarantees compatible access to
writers in other processes. Lock and access failures are wrapped in application-level workspace
errors while preserving their original causes.

## Initialize and inspect

```bash
python scripts/init_workspace.py
python scripts/inspect_workspace.py
```

The first command creates the layout, initializes existing storage, and writes the manifest. Running
it again is idempotent: the workspace ID remains unchanged and data is neither duplicated nor deleted.

Inspection is strictly read-only. It reports manifest compatibility, layout presence, database and
raw/Parquet storage presence, and compact counts for raw records, normalized observations, metric
results, and diagnostic results. It never returns stored documents or creates missing structures.

Select another workspace with an environment variable:

```bash
INVESTMENT_ANALYST_WORKSPACE="$HOME/investment-data/personal" \
python scripts/init_workspace.py
```

An explicit `--workspace /absolute/path` has higher precedence. Persistent data should normally live
outside the source repository. `/tmp` remains suitable only when a test explicitly selects it.

There is no automatic workspace-format or storage migration yet. Incompatible versions require an
explicit future migration design rather than silent rewriting.

## Backup y restore verificados

Los scripts `backup_workspace.py` y `restore_workspace.py` operan mediante
`WorkspaceBackupService`. El backup se publica de forma atómica con manifest v1, inventario,
tamaños, SHA-256, UUID del workspace y conteos de raw records, observaciones, métricas y diagnósticos.
El restore verifica todo el contenido y vuelve a inspeccionar DuckDB en modo read-only antes de
activar un destino nuevo o vacío. Nunca sobrescribe un workspace con contenido ni modifica el
workspace fuente. Consulta [`capability_driven_runtime.md`](capability_driven_runtime.md).
La creación y la verificación rechazan enlaces simbólicos en cualquier nivel; el inventario solo
acepta archivos regulares contenidos físicamente en el árbol validado.
El inventario incluye el documento de preferencias cuando existe; restore conserva exactamente sus
revisiones y fingerprint. Un workspace que nunca creó preferencias sigue siendo respaldable y
restaurable.
