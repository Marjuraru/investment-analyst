# Crypto Research Synthesis — 2026-08

Status: **NON-NORMATIVE RESEARCH SNAPSHOT**<br>
Research scope: crypto data, analytics, validation, open-source prior art, and near-zero-cost product opportunities<br>
Verified snapshot: **2026-08-11 UTC**<br>
Repository snapshot: `Marjuraru/investment-analyst@d12ae19a3dd2dd0751518df84e8e4c3896b3d77f`

## Authority and intended use

This document is research evidence for planning. It is **not** a product contract, roadmap authorization, implementation specification, provider contract, or source of truth for live repository state.

Authority order:

1. live repository and current `main`;
2. active Work Block;
3. normative product and architecture documentation;
4. this research synthesis;
5. raw research reports R1/R2.

Any planned capability still requires a separate Work Block and its normal PLAN/BUILD/AUDIT gates.

Provider pricing, free tiers, rate limits, endpoint coverage, licensing, retention, and exchange availability are **VOLATILE FACTS** and must be revalidated from primary sources before implementation.

## 1. Purpose and provenance

This synthesis combines four evidence layers:

- **R1** — `deep-research-report.md`: broad state-of-the-art exploration.
- **R2** — `deep-research-report_2.md`: adversarial/replication review of R1.
- **Focused verification** — primary-source checks of claims capable of changing roadmap priority.
- **Live repository inspection** — `investment-analyst` after MKT-2.

R1 and R2 are useful discovery material but contain known factual errors and overconfident inferences. They must not be used as repository-state authority.

Classification vocabulary used below:

- `VERIFIED`: directly checked against a primary source or live repository.
- `SUPPORTED`: multiple pieces of evidence support the claim, but it remains interpretive.
- `MIXED`: evidence is useful but incomplete, source-dependent, or methodologically constrained.
- `CONTRADICTED`: the reviewed claim is materially inconsistent with primary evidence.
- `VOLATILE`: correct only as a dated provider/product fact and subject to change.
- `UNKNOWN`: research did not establish the claim strongly enough.

Evidence type:

- `LIVE_REPO_FACT`
- `EXTERNAL_FACT`
- `VOLATILE_FACT`
- `INFERENCE`
- `ROADMAP_HYPOTHESIS`

## 2. Live repository snapshot

`LIVE_REPO_FACT / VERIFIED`

At the verified snapshot, `main` is:

`d12ae19a3dd2dd0751518df84e8e4c3896b3d77f` — `feat: add auditable market EMA lineage (#49)`.

Relevant existing capabilities include:

- local-first persistent workspace using DuckDB, append-only evidence and Parquet export;
- deterministic identities and point-in-time semantics centered on `available_at`;
- `Decimal` financial calculations;
- Alpaca IEX market data for supported US listed assets;
- Coinbase Exchange daily crypto market data for BTC-USD and ETH-USD, with a separate richer intraday contract currently limited to BTC-USD;
- SEC EDGAR fundamentals for eligible corporate issuers;
- SMV/BVL identity and registry work;
- FRED/ALFRED point-in-time macro evidence;
- deterministic screening, analytical candidates, historical replay, cooldown/hysteresis, and silent monitoring;
- market statistics including return, SMA, volatility, relative volume, Bollinger and EMA;
- MKT-2 derived lineage through `input_metric_result_ids`;
- no operational predictive signals, direct recommendations, broker integration, order execution, or automated trading.

Current runtime dependencies declared in `pyproject.toml` are `duckdb` and `pydantic`. **CCXT is not a current runtime dependency.**

Therefore any adoption of CCXT, Cryptofeed, VectorBT, pandas, NumPy, SciPy, statsmodels, or similar tooling is a new architectural/dependency decision and must not be inferred from R1/R2.

## 3. Findings that survived review

### 3.1 Crypto has useful public data dimensions beyond OHLCV

`EXTERNAL_FACT / VERIFIED`

Crypto offers useful public or no-auth data surfaces beyond ordinary spot candles, including exchange market data, derivatives market data, DeFi/stablecoin aggregates, and prospective order-book/trade feeds.

`ROADMAP_HYPOTHESIS / SUPPORTED`

Given the current product state, adding a **new evidence dimension** may have higher marginal product value than repeatedly adding low-cost transformations of the same OHLCV series.

This is not proof that any particular dataset creates predictive edge.

### 3.2 Classical technical indicators should not become an automatic sequence

`ROADMAP_HYPOTHESIS / SUPPORTED`

R1 and R2 both found weak evidence for treating RSI, MACD, and similar classical indicators as standalone predictive edge.

MKT-2 remains architecturally useful because it delivered deterministic recursive lineage, not because EMA itself is assumed to be predictive.

Implication: the next Work Block should **not automatically be RSI or MACD merely because they are next on an old checklist**.

This does not permanently prohibit those indicators; PLAN may still select one if live architecture and product leverage justify it.

### 3.3 Deribit public derivatives data is materially more accessible than R2 claimed

`EXTERNAL_FACT / VERIFIED`

Deribit documents that public API methods do not require authentication. Strategically relevant public market-data methods include:

- historical perpetual funding rates;
- volatility-index history;
- historical volatility;
- index data;
- current book summaries with open interest and, where applicable, current funding and option `mark_iv`;
- mark-price history for supported instruments, with documented coverage limitations.

`CONTRADICTED`

R2's broad characterization of Deribit implied-volatility/derivatives data as generally requiring a paid provider is too strong.

`UNKNOWN`

Research has **not** established that a complete historical point-in-time options surface — all strikes, expiries, instruments and historical availability — can be reconstructed for free.

A derivatives Work Block would need a bounded provider-discovery/probe contract before claiming that capability.

### 3.4 DefiLlama has a meaningful free API, but free and Pro coverage must be separated

`EXTERNAL_FACT / VERIFIED`

DefiLlama's current official API documentation separates Free and Pro APIs.

At verification time, the Free API:

- requires no authentication;
- documents 31 endpoints;
- includes historical TVL;
- includes stablecoin lists and historical stablecoin market-cap/distribution series;
- includes DEX and options-DEX volume endpoints;
- includes open-interest overview for DeFi perpetual venues;
- includes protocol fees/revenue endpoints;
- includes yield/pool endpoints.

`EXTERNAL_FACT / VERIFIED`

The same documentation classifies several other datasets as Pro-only, including bridge endpoints, token unlock endpoints, ETF endpoints, selected inflow/liquidity analytics, and additional derivatives endpoints.

`CONTRADICTED`

Claims in R1/R2 that treat DefiLlama bridge API data as generically free are not valid under the verified API split.

`VOLATILE_FACT`

DefiLlama's plans, endpoint split, rate limits, and prices can change. Revalidate them before implementation.

### 3.5 Historical vendor aggregates are not automatically vintage-correct PIT evidence

`INFERENCE / SUPPORTED`

A historical value returned today for a past date is not automatically proof of what the provider exposed on that past date.

Providers can change methodology, labels, entity mappings, backfills, or historical values.

For `investment-analyst`, a provider integration should distinguish, where applicable:

- event/measurement time;
- source period;
- provider-reported timestamp;
- retrieval time;
- `available_at` / `known_at`;
- provider methodology/version if exposed;
- raw response identity.

A provider's historical endpoint must not silently be treated as a vintage archive unless that behavior is explicitly demonstrated.

### 3.6 Cross-sectional crypto research is promising, but zero-cost survivorship-safe history is unresolved

`ROADMAP_HYPOTHESIS / MIXED`

Cross-sectional factors such as size, momentum/reversal, liquidity and related characteristics are worth future investigation because they require a broader information set than a single-asset indicator.

`EXTERNAL_FACT / VERIFIED`

CoinGecko currently documents access to inactive/delisted coin history as a paid-plan capability.

`INFERENCE`

Using today's active universe to backtest earlier periods risks survivorship bias.

`UNKNOWN`

The research did **not** establish a complete survivorship-safe historical crypto universe at approximately USD 0/month.

A viable zero-cost strategy may instead be **prospective universe capture**: snapshot identities, listings, market-cap/volume metadata and removals from now onward, creating the project's own append-only PIT universe.

### 3.7 Multi-venue data is cheap prospectively, but historical depth is a separate problem

`EXTERNAL_FACT / VERIFIED`

Coinbase currently exposes public/no-auth market-data WebSocket channels for trades and selected order-book/candle data. Other exchanges also expose public market-data surfaces, but each venue has its own contracts, limits and retention.

`ROADMAP_HYPOTHESIS / SUPPORTED`

A second spot venue could enable:

- venue dispersion;
- liquidity comparison;
- source robustness;
- price-discovery research;
- future cross-venue features.

`INFERENCE`

Prospective capture is much easier than reconstructing a complete historical tick/order-book archive.

Therefore "multi-venue" should not automatically imply a high-frequency streaming subsystem or massive tick warehouse in the first block.

### 3.8 Coin Metrics Community is useful but must be treated as constrained coverage

`EXTERNAL_FACT / VERIFIED`

Coin Metrics documents a Community HTTP API without an API key and a current limit of 10 requests per 6 seconds per IP.

Coverage differs by dataset. For example, current documentation states that Community funding-rate data is limited to the previous 24 hours, while full history belongs to the professional product.

`ROADMAP_HYPOTHESIS / MIXED`

Coin Metrics remains useful for discovery and selected network/reference data, but should not be assumed to solve all historical market/on-chain requirements for free.

## 4. Reliability notes and corrections to R1/R2

### R1 known limitations

`CONTRADICTED / LIVE_REPO_FACT`

R1 explicitly failed to inspect the repository and inferred its state. Its FIT-GAP section is therefore non-authoritative.

Examples of unsafe R1 assumptions:

- treating missing repo capabilities as probable without live verification;
- implying mature third-party libraries should be adopted rather than merely evaluated;
- underestimating the engineering complexity of a PIT-valid backtest/replay layer.

### R2 known limitations

`CONTRADICTED / LIVE_REPO_FACT`

R2 claimed or implied that the repository:

- already uses CCXT;
- uses SQLite/CSV as runtime persistence;
- already has local backtesting in the sense described by trading frameworks;
- may already include RSI/MACD.

These claims do not match the verified repository snapshot.

`CONTRADICTED`

R2 also attributed positions to R1 that R1 did not actually hold. R1 already warned against over-prioritizing classical technical indicators and deep-learning approaches without rigorous validation.

### Provider corrections

`CONTRADICTED`

- DefiLlama bridge API access must not be described as part of the verified Free API.
- Deribit public derivatives market data must not be described as generally paywalled.
- "On-chain history is PIT by nature" is too broad: immutable blockchain events do not make third-party entity labels, derived metrics or reconstructed aggregates automatically vintage-correct.
- "Free data" must not be equated with unrestricted historical depth, stable methodology, permissive commercial licensing, or zero engineering cost.

## 5. Verified external data landscape

The following table is qualitative. It is **not an aggregate score** and does not establish roadmap priority.

| Family | Near-zero-cost availability | Historical potential | PIT/repro challenge | Engineering | Main caution |
|---|---|---|---|---|---|
| Deribit funding / volatility index | Strong | Strong for documented endpoints | Moderate | Low–Medium | Exact retention and semantics must be probed |
| Deribit OI / option snapshots | Strong prospectively | Mixed | Moderate | Medium | Full historical options surface not established |
| DefiLlama TVL / stablecoins / DEX / fees | Strong | Strong for documented histories | Moderate–High | Low–Medium | Vendor backfills/methodology are not vintages |
| Crypto universe snapshots | Strong prospectively | Weak before capture start | High | Medium | Survivorship and delisting history |
| Multi-venue spot | Strong prospectively | Venue-dependent | Moderate | Medium | Symbol/venue normalization and history |
| Order-book microstructure | Strong prospectively | Often weak without own capture | High | High | Storage, synchronization, dropped messages |
| Raw on-chain metrics | Strong in principle | Strong | Moderate–High | High | Chain-specific semantics and processing |
| Exchange-wallet flows | Mixed | Potentially strong | High | High | Address-label provenance and revision |
| Coin Metrics Community | Useful but constrained | Dataset-dependent | Moderate | Low–Medium | Free coverage differs by dataset |
| Dune research queries | Useful | Strong queryable chain history | Moderate | Medium | Result limits, execution retention, query semantics |
| Classical OHLCV indicators | Strong | Strong | Low | Low | Limited new information dimension |
| ML / deep learning | Depends on prior data layer | Depends | Very High | High | Overfitting, labels, temporal validation |

## 6. PIT and reproducibility requirements for any new crypto source

Any selected provider/domain should answer these questions before production integration:

1. What exactly is the observed event or measurement?
2. What does the provider timestamp mean?
3. When did the information become available to our system?
4. Can the provider revise historical values or methodology?
5. Can the exact raw response be persisted append-only?
6. Can we reconstruct the information set available at a prior cutoff without look-ahead?
7. Is the asset/venue/chain/instrument identity stable and explicit?
8. Is historical backfill equivalent to live capture, or semantically different?
9. Are rate limits, pagination, missing intervals and retention explicit?
10. What licensing/terms constraints apply to storage, redistribution and future commercial use?

If these cannot be answered, the data may still be useful descriptively, but its PIT claims must be narrowed.

## 7. Candidate capability families — UNORDERED

These are planning hypotheses, not approved roadmap items.

### Crypto derivatives evidence

Possible scope:

- Deribit public funding history;
- volatility-index history;
- current/open-interest evidence;
- bounded option/IV snapshots if semantics are clear.

Why it is interesting:

- adds a genuinely new evidence dimension;
- public endpoints reduce data cost;
- BTC/ETH align with existing crypto coverage;
- some data volumes are modest compared with full order books.

Primary unknown:

- exact historical completeness and PIT semantics for richer option features.

### DeFi / stablecoin evidence

Possible scope:

- stablecoin supply and chain distribution;
- TVL;
- DEX volumes;
- fees/revenue;
- selected DeFi open-interest aggregates.

Why it is interesting:

- substantial current Free API coverage;
- low-to-moderate storage requirements;
- evidence is different from CEX price history.

Primary unknown:

- methodology revisions and vintage correctness of historical aggregates.

### Historical crypto universe foundation

Possible scope:

- prospective snapshots of active asset identities;
- market-cap/volume metadata;
- first/last observed states;
- explicit removals/delistings where observable;
- append-only universe reconstruction.

Why it is interesting:

- enables later cross-sectional research;
- attacks survivorship bias directly;
- creates a proprietary historical evidence asset over time.

Primary unknown:

- best zero-cost identity/metadata source and rules for token/chain identity changes.

### Multi-venue spot evidence

Possible scope:

- one additional spot venue or source;
- normalized venue identity;
- same-asset price/volume comparison;
- bounded dispersion/liquidity evidence.

Why it is interesting:

- reduces single-venue dependence;
- creates a foundation for price-discovery and fragmentation analysis.

Primary unknown:

- which second venue provides the best contract, historical depth and operational reliability without adding unnecessary dependency complexity.

PLAN may reject all four if live repository inspection identifies a smaller or higher-leverage cohesive capability.

## 8. Anti-roadmap for the next stage

The following are **not permanent prohibitions**. They are warnings against premature prioritization.

Do not prioritize solely because a capability is popular or easy:

- a long sequence of RSI/MACD/other classical indicators without a clear product/validation reason;
- full automated trading or exchange execution;
- ML/deep learning before feature provenance, labels and temporal validation are mature;
- full tick-level multi-exchange order-book warehousing before a bounded analytical use case exists;
- massive raw-chain graph analytics before a concrete question justifies the processing cost;
- paid institutional datasets before free/public evidence is proven to be the actual bottleneck;
- social/news sentiment ingestion merely because the data exists;
- a third-party framework solely to avoid writing a small direct adapter;
- opaque aggregate scores that collapse evidence quality, provider quality, analytical value and recommendation into one number.

## 9. Open questions that may justify future focused research

Only perform additional research when an unresolved question can materially change a Work Block.

High-value unresolved questions include:

- How much Deribit historical funding and volatility-index history is practically retrievable under the public contract?
- Which Deribit option instruments expose usable historical mark-price/IV-related history, and what is the retention boundary?
- Does DefiLlama expose enough methodology/version metadata to support stronger PIT claims, or should historical imports be explicitly labeled retrospective?
- Which zero-cost provider is best for prospective crypto-universe snapshots and stable asset identity?
- Which second spot venue offers the best combination of public access, symbol stability, historical depth and operational reliability?
- What licensing constraints matter if `investment-analyst` later becomes commercial?
- Which new evidence family produces useful descriptive/replay capabilities before any predictive-signal layer exists?

## 10. Planning guidance

The next PLAN should inspect live `main` first and compare candidate families qualitatively across:

- analytical leverage;
- zero/low-cost data availability;
- reproducibility and PIT feasibility;
- architectural reuse;
- validation feasibility;
- engineering complexity;
- provider dependence;
- storage/operational burden;
- licensing risk.

Do **not** collapse these dimensions into an opaque aggregate score.

A planned capability is not implementation authorization.

The canonical product separation remains:

`evidence → analytics → opportunity detection → validated signals/prediction → explicit recommendation → human/policy decision → future controlled execution`

This research concerns mainly the **evidence and analytics foundations**.

## 11. Primary sources and verification notes

Verified during the focused review. URLs and provider facts remain subject to change.

### Live repository

- Repository: https://github.com/Marjuraru/investment-analyst
- Verified main commit: https://github.com/Marjuraru/investment-analyst/commit/d12ae19a3dd2dd0751518df84e8e4c3896b3d77f
- README at snapshot: https://github.com/Marjuraru/investment-analyst/blob/d12ae19a3dd2dd0751518df84e8e4c3896b3d77f/README.md
- Runtime dependencies: https://github.com/Marjuraru/investment-analyst/blob/d12ae19a3dd2dd0751518df84e8e4c3896b3d77f/pyproject.toml

### Deribit

- Authentication/public methods: https://docs.deribit.com/articles/authentication
- Funding-rate history: https://docs.deribit.com/api-reference/market-data/public-get_funding_rate_history
- Volatility-index history: https://docs.deribit.com/api-reference/market-data/public-get_volatility_index_data
- Historical volatility: https://docs.deribit.com/api-reference/market-data/public-get_historical_volatility
- Book summary / OI / funding / option mark IV fields: https://docs.deribit.com/api-reference/market-data/public-get_book_summary_by_instrument
- Mark-price history and documented limitations: https://docs.deribit.com/api-reference/market-data/public-get_mark_price_history

### DefiLlama

- Free vs Pro API and endpoint map: https://api-docs.defillama.com/
- Subscription/API plan comparison: https://defillama.com/subscription

### CoinGecko

- Inactive/delisted historical-data availability: https://docs.coingecko.com/changelog/10122018
- Coin list / active-inactive identity map: https://docs.coingecko.com/reference/coins-list

### Coin Metrics

- API conventions and Community rate limits: https://docs.coinmetrics.io/api
- Community funding-rate availability: https://docs.coinmetrics.io/market-data/market-data-overview/funding-rates/futures-funding-rates

### Dune

- API rate limits and 32 GB result limit: https://docs.dune.com/api-reference/overview/rate-limits
- Execution-result retention/limits: https://docs.dune.com/api-reference/executions/endpoint/get-execution-result

### Coinbase

- Exchange WebSocket overview: https://docs.cdp.coinbase.com/exchange/websocket-feed/overview
- Exchange WebSocket channels: https://docs.cdp.coinbase.com/exchange/websocket-feed/channels
- Advanced Trade public market-data channels: https://docs.cdp.coinbase.com/coinbase-business/advanced-trade-apis/guides/websocket

## 12. Final research position

`ROADMAP_HYPOTHESIS / SUPPORTED`

The research does **not** establish that crypto should replace the project's multi-asset architecture.

It does establish a credible reason for a **temporary crypto-first product emphasis** while data budget is near zero: several crypto-specific evidence dimensions are accessible publicly and can deepen the analytical system without committing to paid institutional feeds.

The next capability should still be chosen independently by PLAN against live architecture.

Research phase status: **broad exploration closed; only focused verification should continue when a concrete planning decision depends on it.**
