# Alpaca AAPL history import

This step uses **Alpaca Market Data only**. It calls the read-only endpoint
`https://data.alpaca.markets/v2/stocks/AAPL/bars`; it contains no account, position, order,
paper-trading, or execution functionality.

Authentication is sent only through the `APCA-API-KEY-ID` and `APCA-API-SECRET-KEY` headers. Keys
are never placed in URLs, stored in DuckDB, included in summaries, or printed by the script.

The importer always requests daily AAPL bars with `timeframe=1Day`, `feed=iex`,
`adjustment=all`, ascending order, and a page size of 10,000. It follows every
`next_page_token`, detects token cycles, filters locally with `start <= timestamp < end`, orders
bars deterministically, and rejects contradictory duplicates.

## Coverage and precision

The free IEX feed is partial and does not represent all United States exchanges. Its volume and
trade-count fields are therefore partial. Prices are requested with `adjustment=all`. This source
must not yet be used to infer institutional activity or drive Cazatiburones.

Provider numbers are parsed directly into `Decimal`; financial data never passes through binary
floating point. The fixture used by tests is fictitious and does not represent real AAPL prices.

## Traceability and storage

Each provider bar becomes one immutable `RawRecord` containing the provider strings for timestamp,
OHLC, volume, trade count, and VWAP. Seven `NormalizedObservation` objects reference that record.
IEX observations use `PARTIAL` quality and no reporting period is invented.

The raw-bar checksum participates in a deterministic UUID5. Re-importing identical content reuses
the same raw record and observations. A historical revision receives a new raw-record identifier,
so earlier versions remain available. `available_at` conservatively uses retrieval time because this
step does not infer when a historical daily bar first became available.

## Running the importer

Set credentials without placing them on the command line:

```bash
export ALPACA_API_KEY="..."
export ALPACA_API_SECRET="..."
```

Then run:

```bash
python scripts/fetch_alpaca_history.py \
  --root /tmp/investment-analyst-alpaca \
  --start 2026-06-01 \
  --end 2026-07-01
```

`--end` is exclusive. The command does not delete existing data. It prints a JSON summary and a
notice that the import is data analysis only, uses partial IEX coverage, executes no orders, and is
not financial advice.

This step still provides no indicators, metrics, real diagnostics, WebSocket feed, or trading.
