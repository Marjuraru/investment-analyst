# Application runtime

`ApplicationRuntime` is the common composition root for command-line tools. It resolves the data
location, opens `LocalStorage` in an explicit access mode, loads the static asset catalog once, and
creates one provider-context resolver. Domain clients and pipelines still receive ordinary typed
configuration; they do not depend on the runtime, workspace, catalog file, or DuckDB.

`InvestmentAnalystApplication` builds on this runtime and is the stable operation-level facade for
Apple bootstrap and diagnostic queries. CLI commands and future runner or interface adapters call
the facade instead of assembling provider and analytical pipelines themselves. See
`docs/application_facade.md`.

## Workspace and legacy storage

Every migrated command accepts a mutually exclusive location pair:

- `--workspace PATH` selects an initialized persistent workspace.
- `--root PATH` retains the historical direct-storage layout as a compatibility interface.
- With neither argument, `WorkspaceService` resolves the default workspace using
  `INVESTMENT_ANALYST_WORKSPACE`, XDG data home, or the user data directory.

The default never uses `/tmp` and does not depend on the current working directory. `--root` does
not require a workspace manifest and preserves existing persisted identities. No data is copied or
migrated between layouts.

## Access modes

Commands declare their access explicitly:

- Downloads, normalization, metric computation, diagnostic computation, simulations, and the Apple
  snapshot use `read_write`.
- Market-history queries, SEC fundamental queries, and the consolidated Apple diagnostic query use
  `read_only`.

Workspace storage is opened through `WorkspaceService.open_storage`. Legacy storage is constructed
only inside `ApplicationRuntime`; read-only legacy access uses DuckDB's real read-only mode and will
not create a database or initialize tables. A workspace must already have a valid compatible manifest, normally created with
`scripts/init_workspace.py`. Resolution validates that manifest through `WorkspaceService`, and the
resolved location carries the manifest workspace UUID without rewriting any workspace files.

## Catalog composition

`ApplicationRuntime.create_default()` loads `AssetCatalogService` once and creates one
`ProviderAssetContextResolver`. Provider composition roots reuse that resolver to obtain Alpaca,
Coinbase, or SEC identifiers. HTTP clients remain independent from application composition and do
not load the catalog themselves.

The runtime is an ordinary injected object, not a singleton. It has no mutable global state, reads
no `.env` file, and stores no credentials.

## Examples

Use the default workspace:

```bash
python scripts/query_aapl_diagnostics.py \
  --known-at 2026-07-14T06:46:00Z \
  --fundamental-frequency quarterly
```

Use another initialized workspace:

```bash
python scripts/query_aapl_diagnostics.py \
  --workspace "$HOME/investment-data/personal" \
  --known-at 2026-07-14T06:46:00Z \
  --fundamental-frequency quarterly
```

Use the legacy storage layout temporarily:

```bash
python scripts/query_aapl_diagnostics.py \
  --root /tmp/legacy-analysis \
  --known-at 2026-07-14T06:46:00Z \
  --fundamental-frequency quarterly
```

`/tmp` in the final example is explicit and is appropriate for tests or disposable runs; it is not
a default. Existing data is not migrated automatically. The runtime changes composition only and
does not alter financial algorithms, source IDs, deterministic IDs, recommendations, or trading
behavior. The standalone simulated demonstration remains on its legacy explicit-root interface;
financial provider, normalization, metric, diagnostic, and consolidated commands use the runtime.
