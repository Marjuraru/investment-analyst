# Versioned asset catalog

The asset catalog separates a stable canonical asset ID from identifiers assigned by external
providers. A ticker is convenient for display, but it is not a safe global identity: the same
text can be reused across markets, providers, or asset classes. The canonical IDs already used
by this repository remain unchanged.

The packaged `default_assets.v1.json` document is static, versioned, deterministic, and contains
only the assets currently supported by working pipelines: Apple and the Coinbase BTC-USD asset.
It contains no credentials, URLs, market observations, algorithm parameters, recommendations, or
time-varying values.

## Model

A `CatalogAsset` extends the core `Asset` contract with explicit aliases and provider bindings.
Aliases are case-insensitive lookup names and do not replace the canonical ID. Each binding has:

- `provider`: a lower-case provider slug such as `alpaca`, `sec`, or `coinbase`;
- `namespace`: the provider's identifier kind, such as `symbol`, `cik`, or `product_id`;
- `identifier`: the exact external value;
- `capabilities`: sorted namespaced operations currently supported through that binding.

A binding identity is `(provider, namespace, identifier)`. The catalog rejects duplicate
canonical IDs and duplicate external identities. Reverse lookup therefore cannot silently choose
between two assets. Alias ambiguity is also reported explicitly.

SEC CIK values are strings. This preserves Apple's ten-digit CIK, including leading zeroes.
Coinbase product IDs and provider symbols are likewise preserved exactly instead of being
converted or normalized destructively.

## Queries

List the complete packaged catalog:

```bash
python scripts/list_assets.py
```

Filter by asset class or capability:

```bash
python scripts/list_assets.py --asset-type equity
python scripts/list_assets.py --capability market.daily_bars
```

Resolve an alias or canonical ID:

```bash
python scripts/resolve_asset.py --alias aapl
python scripts/resolve_asset.py --asset-id crypto:btc-usd
```

Return a specific provider binding:

```bash
python scripts/resolve_asset.py \
  --asset-id equity:us:aapl \
  --provider sec \
  --namespace cik
```

## Extending the catalog

To add an already implemented asset, edit the next versioned JSON resource and provide its core
`Asset` fields, unique aliases, and sorted provider bindings. Every capability must be a
lower-case namespaced name. Asset IDs and external binding identities must remain globally
unique. New provider identifiers must match the constants and behavior of the implementation,
and compatibility tests should import those constants so catalog drift is detected.

The service loads the packaged resource with `importlib.resources`, reads it once per instance,
and builds immutable in-memory indexes once. It does not access the network, DuckDB, a workspace,
or environment variables. Pipelines do not consume the catalog yet; migration will be gradual so
existing persisted IDs and working integrations remain unchanged.
