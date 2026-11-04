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

Each provider bar becomes one immutable `RawRecord` containing the eight provider strings for
timestamp, OHLC, volume, trade count, and VWAP. Seven `NormalizedObservation` objects reference that
record. When the provider reports `vw=0` together with `v=0` and `n=0`, VWAP is not calculable: the
raw record still preserves `vw="0"` and exactly six observations are stored, without a zero-USD
VWAP observation, imputation, or deletion of the bar. IEX observations use `PARTIAL` quality and no
reporting period is invented.

Admission is data-driven: `vw=0` is accepted only with zero volume and zero trades. Other
non-positive VWAP values, non-finite values, invalid OHLC, and incoherent activity remain rejected;
a positive VWAP keeps its previous treatment even when volume is zero. The import integrity check
derives the exact expected field set from the raw values and rejects missing `close`, duplicate
fields, or an absent VWAP in a normal bar.

The import JSON summary adds `vwap_unavailable_bars`, counted only from bars inspected by that
execution (including reused bars already inspected). It does not scan the stored corpus, and an
`already_current` run reports zero for that execution rather than claiming historical absence.
The source receipt still certifies a complete provider fetch, not completeness of seven fields.

Legacy and v2 daily readers share the rule in `bar_schemas.missing_market_bar_fields`: an Alpaca
IEX daily bar may omit VWAP only when the projected volume and trade count are zero. The resulting
bar exposes `vwap=None` and no `vwap` key in its observation IDs; close, volume, timestamp, quality,
and availability are unchanged. Chart and intraday aggregations preserve the missing VWAP instead
of recomputing it from other days. A missing VWAP with positive volume or trades, or the absence of
any other required field, still fails as an incomplete bar.

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
