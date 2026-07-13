# Coinbase Exchange daily history

This step imports public BTC-USD daily candles from Coinbase Exchange Market Data. The endpoint is
read-only, public, and does not require credentials or API keys. It is used only to demonstrate a
real provider-to-storage path; the application still does not trade or issue recommendations.

## Candle format and requests

Coinbase returns each candle as six ordered values:

```text
[time, low, high, open, close, volume]
```

The importer requests a fixed granularity of 86,400 seconds. Coinbase permits at most 300 candle
intervals per request, so longer periods are divided into consecutive chunks. Responses are filtered
to the exact half-open interval `start <= candle_time < end`, sorted chronologically, and deduplicated.
Coinbase may return older candles than requested or omit intervals with no trades. Missing dates are
reported but never filled with invented data.

All financial values are parsed directly into `Decimal`; binary floating-point values are not used.
Tests use a local fixture and never contact the network.

## Storage and traceability

Each accepted candle becomes one immutable `RawRecord` containing the ordered raw values as strings.
Five `NormalizedObservation` objects are then created for open, high, low, close, and volume. No trade
count is invented.

The checksum covers the stored ordered `raw_candle` representation. A UUID5 includes the source,
product, candle timestamp, granularity, and checksum. Re-importing identical content therefore reuses
the same identifiers, while corrected historical content produces a new raw record and five new
observations without deleting the earlier version.

The pipeline verifies that every observation resolves to its raw record, belongs to BTC-USD, uses UTC,
has a one-day period, and survives a storage round-trip.

## Run the importer

```bash
python scripts/fetch_coinbase_history.py \
  --root data/coinbase-demo \
  --start 2026-06-01 \
  --end 2026-07-01
```

`--end` is exclusive and both dates are interpreted as midnight UTC. The destination root is always
explicit and existing data is not deleted.

These candles represent Coinbase Exchange only, not an aggregate of the entire cryptocurrency market.
WebSockets, live updates, indicators, metrics, and real diagnostics are not implemented. The importer
performs data analysis only, executes no orders, and does not constitute financial advice.
