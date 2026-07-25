# Unified stored market history

The unified history layer reconstructs one common `MarketBar` shape from data already stored by the
Coinbase, Alpaca, or simulated pipelines. It never contacts a provider. Future indicators can depend
on this analytical shape instead of depending directly on provider payloads.

A provider response is retained as a canonical `RawRecord`. Each normalized observation extracts one
field and points back to exactly one raw record. The history service groups observations by
`raw_record_id`, verifies the corresponding raw file, and only then creates a `MarketBar`. It never
combines fields from different raw records.

Every query requires both `asset_id` and `source_id`. Source identity is mandatory because Coinbase,
Alpaca IEX, and simulated data have different coverage, units, fields, and quality. Sources are never
mixed silently. Coinbase bars contain OHLCV and are marked `VALID`; Alpaca IEX bars also contain trade
count and VWAP but are marked `PARTIAL`; simulated bars contain OHLCV and trade count and are marked
`VALID`.

## Point-in-time behavior and revisions

`known_at` defines what information the query is allowed to know. Only versions with
`available_at <= known_at` participate. When multiple raw-record revisions represent the same bar
timestamp, the service selects the revision with the latest eligible `available_at`. Equal
availability times across different revisions are ambiguous and produce an error instead of using a
UUID as a hidden tiebreaker. Coverage reports candidate, selected, and discarded revision counts.

The time filter is half-open: `start <= timestamp < end`. Missing bars are left missing. The layer does
not infer exchange calendars, weekends, holidays, or absent crypto intervals.

## Bounded reads and evidence verification

The observation repository applies asset, daily-frequency, availability, and half-open observed-time
filters in DuckDB before deserializing documents. The service repeats the source, timestamp, frequency,
and point-in-time checks on the typed observations so storage filtering cannot weaken domain
validation.

Raw-record index rows are retrieved in one bulk operation for each history query. Every selected file
is still read and checked against its indexed SHA-256 checksum and canonical JSON before its typed
record participates in reconstruction. This removes per-bar database round trips without caching,
skipping, or weakening traceability.

## Command-line query

```bash
python scripts/query_market_history.py \
  --root /tmp/investment-analyst-data \
  --asset-id crypto:btc-usd \
  --source-id coinbase-exchange:btc-usd:daily-candles \
  --start 2026-06-01 \
  --end 2026-07-01 \
  --known-at 2026-07-02T00:00:00Z \
  --limit 50
```

The script prints query metadata, coverage, bounded bars, truncation status, and traceability status.
It does not print raw documents, read API keys, call providers, calculate indicators or diagnostics,
execute orders, or provide financial advice.
