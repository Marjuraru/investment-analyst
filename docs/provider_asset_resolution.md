# Provider asset resolution

The canonical asset ID is the durable identity used by storage and analytics. Provider symbols are external identifiers used only at provider boundaries. A ticker such as `AAPL` is not a global identity because different providers may use different namespaces and because the same text can be ambiguous across asset classes or markets.

## Resolution flow

Each composition root now follows one explicit path:

```text
canonical asset_id
→ AssetCatalogService
→ ProviderAssetContextResolver
→ ProviderAssetContext
→ typed provider configuration
→ client and pipeline
```

The catalog is loaded once per command. Resolution occurs before HTTP or storage writes. Clients do not load the JSON catalog, access a workspace, or query DuckDB. They receive only identifiers that were already resolved and validated.

## Bindings and capabilities

A provider binding is identified by `provider + namespace + identifier`. Current namespaces are:

- Alpaca: `symbol`
- Coinbase: `product_id`
- SEC: `cik` and `ticker`

Capabilities are checked before provider execution. Alpaca and Coinbase require `market.daily_bars`. SEC requires `fundamentals.submissions` and `fundamentals.company_facts`. An unsupported provider, missing namespace, or missing capability fails without an HTTP request.

The SEC CIK remains a ten-character string so leading zeroes are preserved. It is never converted to an integer.

## Typed configurations

Provider-specific configuration models contain only the identifiers and fixed dataset settings required by the current flows. They are immutable and reject extra fields. The current source IDs, Alpaca IEX feed, adjustment mode, and Coinbase daily granularity remain unchanged.

Existing constants remain available for compatibility with older tests and callers. New composition code resolves external identifiers through the catalog and uses those constants only for stable dataset identities that must remain compatible with persisted records.

## Persisted identity compatibility

This migration does not rewrite data or change schemas. For the same provider payload and timestamps, the following remain unchanged:

- canonical asset IDs and source IDs;
- request parameters and headers;
- RawRecord checksums and UUID5 identities;
- observation IDs and record keys;
- metric and diagnostic identities;
- provider-derived timestamps and frequencies.

There is no data migration. Existing RawRecords, observations, metrics, and diagnostics remain valid.

## Adding assets or providers

To add an asset, update the versioned catalog with its canonical core asset definition, aliases, exact provider bindings, and supported capabilities. All external identities must remain globally unique.

To integrate a provider, add its bindings and capabilities, resolve one `ProviderAssetContext` at the composition boundary, build a typed provider configuration, and pass only resolved identifiers to the low-level client. Financial algorithms may remain asset-specific until a separate migration explicitly generalizes them.
