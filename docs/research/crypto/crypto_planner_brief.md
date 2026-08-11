# Crypto Planner Brief

Status: **NON-NORMATIVE PLANNING EVIDENCE**<br>
Snapshot: **2026-08**<br>
Research verification: **2026-08-11 UTC**<br>
Repository snapshot used by research: `main@d12ae19a3dd2dd0751518df84e8e4c3896b3d77f`

## Authority

This brief is not roadmap authority and does not authorize implementation.

Before using it, PLAN must inspect live `main`, the active Work Block state, and normative product/architecture documentation.

Authority order:

1. live repository;
2. active Work Block;
3. normative product documentation;
4. `crypto_research_synthesis_2026-08.md`;
5. this brief and raw research material.

Provider pricing, rate limits, free tiers, endpoint coverage, licensing, and retention are volatile and must be revalidated when material to the selected Work Block.

## Strategic finding

Do **not** assume that the next market capability should be RSI, MACD, or another OHLCV-derived indicator merely because it is easy or previously listed.

External research and focused primary-source verification show that crypto currently offers useful public evidence in additional dimensions.

This does not prove predictive edge and does not make any candidate below mandatory.

MKT-2 remains useful because it added auditable derived lineage through `input_metric_result_ids`; it does not imply that EMA or other classical indicators deserve roadmap priority.

## Verified live-repo constraints

At the research snapshot:

- the analytical core is deterministic, PIT and append-only;
- `available_at`, deterministic identities and `Decimal` semantics are permanent constraints;
- runtime persistence uses DuckDB, not SQLite/CSV;
- CCXT is not a current runtime dependency;
- current market statistics include EMA and Bollinger; RSI/MACD remain pending;
- the product has screening/replay but no operational predictive-signal, recommendation, broker, execution or automated-trading layer;
- crypto daily coverage exists for BTC-USD and ETH-USD, while the richer intraday contract is currently BTC-USD-specific.

Live `main` overrides this snapshot if anything has changed.

## Candidate capability families — UNORDERED

The ordering below has **no priority meaning**.

### Crypto derivatives evidence

Candidate evidence:

- public Deribit funding history;
- volatility-index history;
- open-interest/current derivative snapshots;
- bounded option/IV snapshots where the public contract is sufficient.

Known caution:

- complete historical PIT options-surface coverage has not been established.

### DeFi / stablecoin evidence

Candidate evidence from currently documented DefiLlama Free endpoints:

- stablecoin supply/distribution;
- TVL;
- DEX/options-DEX volumes;
- selected DeFi open interest;
- fees/revenue;
- yields/pools.

Known caution:

- provider historical aggregates are not automatically vintage-correct PIT evidence;
- bridges, unlocks, ETFs and several richer datasets are currently documented outside the Free endpoint set.

### Historical crypto universe foundation

Candidate evidence:

- prospective asset-universe snapshots;
- identity and market metadata;
- first/last observed state;
- listings/removals when observable.

Purpose:

- enable future cross-sectional analysis without silently using today's survivors as the historical universe.

Known caution:

- research did not establish a complete survivorship-safe historical universe at USD 0/month;
- inactive/delisted CoinGecko history is currently documented as paid-plan access.

### Multi-venue spot evidence

Candidate evidence:

- one additional spot venue/source;
- explicit venue identity;
- normalized same-asset observations;
- bounded price/volume/liquidity comparison.

Known caution:

- prospective public data is easier than complete historical tick/order-book reconstruction;
- do not turn the first multi-venue block into a full microstructure warehouse without a concrete analytical need.

PLAN may reject all four if live repo inspection identifies a better cohesive next capability.

## Known research traps

- R1 did not inspect the live repository successfully.
- R2 contains known incorrect repo-state claims, including CCXT usage and SQLite/CSV persistence.
- Do not treat R1/R2 FIT-GAP claims as repository facts.
- Provider historical data is not automatically vintage PIT.
- Free data does not imply unrestricted history, stable methodology, commercial licensing, or zero engineering cost.
- Exchange-flow analytics depend on address-label provenance and possible label revision.
- Order-book history is materially harder than prospective capture.
- A current active-coin universe can create survivorship bias in historical cross-sectional work.
- CCXT, Cryptofeed, VectorBT and similar projects are prior art, not pre-approved dependencies.
- Do not infer ETH intraday support from the BTC-specific intraday contract.
- Any source/provider fact that matters to implementation must be revalidated.

## Anti-anchoring instruction

Do not interpret:

- candidate inclusion;
- candidate ordering;
- the number of sources available;
- ease of implementation;
- or the research authors' preferences

as roadmap priority.

Do not implement this brief's ideas during PLAN.

## Planner comparison dimensions

Compare viable candidates qualitatively across:

- analytical leverage;
- zero/low-cost data availability;
- PIT/reproducibility feasibility;
- historical/backfill semantics;
- architectural reuse;
- validation feasibility;
- engineering complexity;
- provider dependence;
- storage/operational burden;
- licensing risk.

Do **not** collapse these dimensions into an opaque aggregate score.

## Permanent invariants

Any selected capability must preserve:

- point-in-time / `available_at`;
- zero look-ahead;
- append-only evidence/history;
- deterministic and reproducible identities;
- `Decimal` for financial calculations where applicable;
- explicit asset/source/venue/chain/instrument identity;
- provider separation from analytics;
- explicit formula/parameters/version/limitations for derived analytics;
- market/fundamental/domain separation;
- no opaque combined score;
- no LLM as calculation truth source;
- no recommendation or execution authority implied by descriptive evidence.

## Expected PLAN behavior

PLAN should:

1. verify current `main`, worktree, active Issues/PRs and `workflow:active`;
2. inspect the current seams relevant to each serious candidate;
3. revalidate only the volatile provider facts that materially affect the decision;
4. compare the candidates above and any better candidate discovered from the live repo;
5. choose the **smallest cohesive next productive capability**;
6. justify why that capability is preferable now and what is explicitly deferred;
7. publish only the Work Block; do not implement.

The selected Work Block may be a bounded provider/data foundation before analytics if the evidence contract itself is the highest-leverage missing piece.

## Research handoff

For detailed provenance, corrections and primary-source links, read:

`docs/research/crypto/crypto_research_synthesis_2026-08.md`

Treat the synthesis as non-normative research evidence, not as a replacement for live inspection.
