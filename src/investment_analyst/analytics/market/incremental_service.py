"""Paged persistent daily market statistics over raw v2 staging."""

from __future__ import annotations

import heapq
import json
from collections import defaultdict, deque
from collections.abc import Collection, Iterator, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from time import perf_counter_ns
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.analytics.market.bar_models import (
    HistoricalBarQuery,
    MarketBar,
    MarketBarCoverage,
    MarketBarSeries,
)
from investment_analyst.analytics.market.daily_evidence import (
    DailyEvidenceFieldGroup,
    DailyEvidencePrefix,
)
from investment_analyst.analytics.market.history_v2 import (
    DailyMarketBarProjection,
    HistoricalMarketDataV2Service,
    MarketHistoryPage,
)
from investment_analyst.analytics.market.incremental_ema import (
    ALGORITHM_VERSION as EMA_V2_ALGORITHM,
)
from investment_analyst.analytics.market.incremental_recursive import (
    ATR_ALGORITHM_VERSION as ATR_V2_ALGORITHM,
)
from investment_analyst.analytics.market.incremental_recursive import (
    MACD_ALGORITHM_VERSION as MACD_V2_ALGORITHM,
)
from investment_analyst.analytics.market.incremental_recursive import (
    RSI_ALGORITHM_VERSION as RSI_V2_ALGORITHM,
)
from investment_analyst.analytics.market.incremental_state import (
    AtrParameters,
    AtrState,
    CheckpointMetricReference,
    EmaParameters,
    EmaState,
    IncrementalStateError,
    MacdParameters,
    MacdState,
    MarketRecursiveCheckpoint,
    RecursiveParameters,
    RsiParameters,
    RsiState,
    advance_checkpoint,
)
from investment_analyst.analytics.market.statistics_definitions import (
    ATR_KEY,
    BOLLINGER_BANDWIDTH_KEY,
    BOLLINGER_LOWER_KEY,
    BOLLINGER_PERCENT_B_KEY,
    BOLLINGER_UPPER_KEY,
    EMA_KEY,
    MACD_HISTOGRAM_KEY,
    MACD_LINE_KEY,
    MACD_SIGNAL_KEY,
    RELATIVE_VOLUME_KEY,
    RSI_AVERAGE_GAIN_KEY,
    RSI_AVERAGE_LOSS_KEY,
    RSI_KEY,
    SIMPLE_RETURN_KEY,
    SMA_KEY,
    TRUE_RANGE_KEY,
    VOLATILITY_KEY,
)
from investment_analyst.analytics.market.statistics_engine import (
    _BOLLINGER_ALGORITHM,
    _RELATIVE_VOLUME_ALGORITHM,
    _RETURN_ALGORITHM,
    _SMA_ALGORITHM,
    _VOLATILITY_ALGORITHM,
    MarketStatisticsEngine,
)
from investment_analyst.analytics.market.statistics_identity import semantic_metric_result_id
from investment_analyst.analytics.market.statistics_models import (
    MarketStatisticsRequest,
    MetricCalculation,
)
from investment_analyst.core.models import MetricResult
from investment_analyst.core.models.base import ContractModel, UTCDateTime
from investment_analyst.storage.raw_v2 import RawV2Staging

type _FinitePresenceKey = tuple[
    str,
    datetime,
    str,
    tuple[UUID, ...],
    str,
    str,
]


class IncrementalMarketRequest(ContractModel):
    """One explicit PIT calculation and computation instant for daily metrics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    statistics: MarketStatisticsRequest
    history_start: UTCDateTime
    history_end: UTCDateTime
    computed_at: UTCDateTime

    @model_validator(mode="after")
    def validate_request(self) -> IncrementalMarketRequest:
        query = self.statistics.query
        if self.history_start > query.start:
            raise ValueError("history_start must not follow the requested metric start")
        if self.history_end < query.end:
            raise ValueError("history_end must cover the requested metric range")
        if self.history_start >= self.history_end:
            raise ValueError("history range must be non-empty")
        if self.computed_at < query.known_at:
            raise ValueError("computed_at must not precede known_at")
        return self


class IncrementalMarketReceipt(ContractModel):
    """Auditable counts from one incremental daily market calculation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    source_id: str
    requested_start: UTCDateTime
    requested_end: UTCDateTime
    known_at: UTCDateTime
    computed_at: UTCDateTime
    candidate_versions: int = Field(ge=0)
    selected_bars: int = Field(ge=0)
    discarded_revisions: int = Field(ge=0)
    history_pages: int = Field(ge=0)
    close_prefixes_created: int = Field(ge=0)
    close_prefixes_reused: int = Field(ge=0)
    hlc_prefixes_created: int = Field(ge=0)
    hlc_prefixes_reused: int = Field(ge=0)
    close_divergence_index: int = Field(ge=0)
    hlc_divergence_index: int = Field(ge=0)
    checkpoints_created: int = Field(ge=0)
    checkpoints_reused: int = Field(ge=0)
    recurrence_steps: int = Field(ge=0)
    metrics_created: int = Field(ge=0)
    metrics_reused: int = Field(ge=0)
    metric_batches: int = Field(ge=0)
    maximum_metric_batch: int = Field(ge=0, le=256)
    bars_recalculated: int = Field(ge=0)
    bar_models_hydrated: int = Field(ge=0)
    finite_bar_models_hydrated: int = Field(ge=0)
    finite_window_calculations: int = Field(ge=0)
    finite_window_halo: int = Field(ge=0)
    history_selection_elapsed_microseconds: int = Field(ge=0)
    daily_prefix_build_elapsed_microseconds: int = Field(ge=0)
    daily_prefix_persist_verify_elapsed_microseconds: int = Field(ge=0)
    checkpoint_lookup_elapsed_microseconds: int = Field(ge=0)
    finite_presence_elapsed_microseconds: int = Field(ge=0)
    bar_materialization_elapsed_microseconds: int = Field(ge=0)
    recurrence_transition_elapsed_microseconds: int = Field(ge=0)
    checkpoint_persist_verify_elapsed_microseconds: int = Field(ge=0)
    finite_calculation_elapsed_microseconds: int = Field(ge=0)
    metric_generation_elapsed_microseconds: int = Field(ge=0)
    metric_dag_persist_elapsed_microseconds: int = Field(ge=0)
    checkpoint_metric_link_elapsed_microseconds: int = Field(ge=0)
    traceability_verified: bool


class IncrementalMarketService:
    """Advance append-only daily evidence and Decimal34 checkpoints in bounded pages."""

    def __init__(self, staging: RawV2Staging) -> None:
        if not staging.is_open:
            raise ValueError("raw v2 staging must be open")
        self._staging = staging
        self._history = HistoricalMarketDataV2Service(staging)
        self._finite_engine = MarketStatisticsEngine()

    def run(self, request: IncrementalMarketRequest) -> IncrementalMarketReceipt:
        """Reconcile PIT history, resume states and emit only the requested range."""
        output_query = request.statistics.query
        earliest = self._staging.earliest_market_observation_timestamp(
            asset_id=output_query.asset_id,
            source_id=output_query.source_id,
            end=request.history_end,
            known_at=output_query.known_at,
        )
        history_start = (
            min(request.history_start, earliest) if earliest is not None else request.history_start
        )
        history_query = HistoricalBarQuery(
            asset_id=output_query.asset_id,
            source_id=output_query.source_id,
            start=history_start,
            end=request.history_end,
            known_at=output_query.known_at,
        )
        self._staging.ensure_market_incremental_tables(create=True)

        parameters = _requested_parameters(request.statistics)
        close_parent: DailyEvidencePrefix | None = None
        hlc_parent: DailyEvidencePrefix | None = None
        previous_projection: DailyMarketBarProjection | None = None
        previous_checkpoint_by_key: dict[str, MarketRecursiveCheckpoint | None] = dict.fromkeys(
            recurrence_key(item) for item in parameters
        )
        seed_capacity = max(
            (
                item.window + 1
                if isinstance(item, RsiParameters)
                else item.window
                if isinstance(item, (EmaParameters, AtrParameters))
                else item.slow_window
                for item in parameters
            ),
            default=1,
        )
        seed_projections: list[DailyMarketBarProjection] = []
        finite_halo = _finite_window_halo(request.statistics)
        finite_projection_ring: deque[tuple[int, DailyMarketBarProjection]] = deque(
            maxlen=finite_halo
        )

        totals = defaultdict(int)
        first_close_divergence: int | None = None
        first_hlc_divergence: int | None = None
        finite_keys = _finite_metric_keys(request.statistics)
        finite_request = request.statistics.model_copy(update={"query": history_query})

        for history_page, selection_elapsed in _timed_pages(
            self._history.iter_pages(history_query)
        ):
            totals["history_selection_elapsed_microseconds"] += selection_elapsed
            totals["history_pages"] += 1
            totals["candidate_versions"] += history_page.candidate_versions
            totals["discarded_revisions"] += history_page.discarded_revisions
            page_bars = history_page.bars
            totals["selected_bars"] += len(page_bars)
            if len(page_bars) > 256:
                raise IncrementalStateError("history service returned a page larger than 256 bars")

            started = perf_counter_ns()
            close_prefixes, close_parent = _build_prefix_page(
                page_bars,
                DailyEvidenceFieldGroup.CLOSE,
                close_parent,
            )
            hlc_prefixes, hlc_parent = _build_prefix_page(
                page_bars,
                DailyEvidenceFieldGroup.HIGH_LOW_CLOSE,
                hlc_parent,
            )
            totals["daily_prefix_build_elapsed_microseconds"] += _elapsed_microseconds(started)
            started = perf_counter_ns()
            close_receipt = self._staging.save_daily_evidence_prefixes(close_prefixes)
            hlc_receipt = self._staging.save_daily_evidence_prefixes(hlc_prefixes)
            totals["daily_prefix_persist_verify_elapsed_microseconds"] += _elapsed_microseconds(
                started
            )
            totals["close_prefixes_created"] += len(close_receipt.created_ids)
            totals["close_prefixes_reused"] += len(close_receipt.reused_ids)
            totals["hlc_prefixes_created"] += len(hlc_receipt.created_ids)
            totals["hlc_prefixes_reused"] += len(hlc_receipt.reused_ids)
            if first_close_divergence is None and close_receipt.created_ids:
                created = set(close_receipt.created_ids)
                first_close_divergence = next(
                    item.length - 1 for item in close_prefixes if item.prefix_id in created
                )
            if first_hlc_divergence is None and hlc_receipt.created_ids:
                created = set(hlc_receipt.created_ids)
                first_hlc_divergence = next(
                    item.length - 1 for item in hlc_prefixes if item.prefix_id in created
                )

            verified_prefixes = {item.prefix_id: item for item in (*close_prefixes, *hlc_prefixes)}
            prefixes_by_family = {
                "ema": close_prefixes,
                "rsi": close_prefixes,
                "macd": close_prefixes,
                "atr": hlc_prefixes,
            }
            existing_by_key: dict[str, dict[UUID, MarketRecursiveCheckpoint]] = {}
            missing_indexes: set[int] = set()
            started = perf_counter_ns()
            for recurrence in parameters:
                key = recurrence_key(recurrence)
                family_prefixes = prefixes_by_family[recurrence.family]
                existing = self._staging.find_market_checkpoints_for_prefixes(
                    tuple(item.prefix_id for item in family_prefixes),
                    recurrence,
                    known_at=output_query.known_at,
                    verified_prefixes=verified_prefixes,
                )
                existing_by_key[key] = existing
                totals["checkpoints_reused"] += len(existing)
                missing_indexes.update(
                    index
                    for index, prefix in enumerate(family_prefixes)
                    if prefix.prefix_id not in existing
                )
            totals["checkpoint_lookup_elapsed_microseconds"] += _elapsed_microseconds(started)

            output_indexes = tuple(
                index
                for index, item in enumerate(page_bars)
                if output_query.start <= item.timestamp < output_query.end
            )
            finite_missing_times: set[datetime] = set()
            started = perf_counter_ns()
            if output_indexes:
                candidates = self._staging.find_market_metric_candidates(
                    asset_id=output_query.asset_id,
                    metric_keys=finite_keys,
                    timestamps=tuple(page_bars[index].timestamp for index in output_indexes),
                )
                existing_finite = {_finite_presence_key(item) for item in candidates.values()}
                prior_projections = [item for _, item in finite_projection_ring]
                for index in output_indexes:
                    context = [*prior_projections, *page_bars[: index + 1]]
                    for expected in _finite_expected_keys(
                        page_bars[index],
                        context,
                        request.statistics,
                    ):
                        if expected not in existing_finite:
                            finite_missing_times.add(page_bars[index].timestamp)
                            break
            totals["finite_presence_elapsed_microseconds"] += _elapsed_microseconds(started)

            finite_groups = _group_finite_outputs(
                page_bars,
                close_prefixes,
                finite_projection_ring,
                finite_missing_times,
                finite_halo,
            )
            projections_to_materialize: dict[UUID, DailyMarketBarProjection] = {
                page_bars[index].raw_record_id: page_bars[index] for index in missing_indexes
            }
            finite_projection_count = 0
            for group_projections, _ in finite_groups:
                finite_projection_count += len(group_projections)
                projections_to_materialize.update(
                    (item.raw_record_id, item) for item in group_projections
                )

            started = perf_counter_ns()
            materialized = self._materialize_projections(
                tuple(projections_to_materialize.values()),
                history_query,
            )
            totals["bar_materialization_elapsed_microseconds"] += _elapsed_microseconds(started)
            totals["bar_models_hydrated"] += len(materialized)
            totals["finite_bar_models_hydrated"] += finite_projection_count
            totals["finite_window_halo"] = max(totals["finite_window_halo"], finite_halo)

            states_by_key: dict[str, tuple[MarketRecursiveCheckpoint, ...]] = {}
            checkpoints_by_id: dict[UUID, MarketRecursiveCheckpoint] = {}
            for recurrence in parameters:
                key = recurrence_key(recurrence)
                family_prefixes = prefixes_by_family[recurrence.family]
                existing = existing_by_key[key]
                previous = previous_checkpoint_by_key[key]
                created_states: list[MarketRecursiveCheckpoint] = []
                states: list[MarketRecursiveCheckpoint] = []
                for projection, prefix in zip(page_bars, family_prefixes, strict=True):
                    checkpoint = existing.get(prefix.prefix_id)
                    if checkpoint is None:
                        bar = materialized.get(projection.raw_record_id)
                        if bar is None:
                            raise IncrementalStateError(
                                "a missing checkpoint requires one verified bar model"
                            )
                        started = perf_counter_ns()
                        checkpoint = advance_checkpoint(previous, bar, prefix, recurrence)
                        totals["recurrence_transition_elapsed_microseconds"] += (
                            _elapsed_microseconds(started)
                        )
                        created_states.append(checkpoint)
                        totals["recurrence_steps"] += 1
                    states.append(checkpoint)
                    checkpoints_by_id[checkpoint.checkpoint_id] = checkpoint
                    previous = checkpoint
                started = perf_counter_ns()
                for batch in _batches(created_states):
                    receipt = self._staging.save_market_recursive_checkpoints(
                        batch,
                        verified_prefixes=verified_prefixes,
                    )
                    totals["checkpoints_created"] += len(receipt.created_ids)
                totals["checkpoint_persist_verify_elapsed_microseconds"] += _elapsed_microseconds(
                    started
                )
                states_by_key[key] = tuple(states)
                previous_checkpoint_by_key[key] = previous

            finite_calculations: list[MetricCalculation] = []
            started = perf_counter_ns()
            for group_projections, output_timestamps in finite_groups:
                bars_by_id = {
                    item.raw_record_id: materialized[item.raw_record_id]
                    for item in group_projections
                }
                ordered_bars = tuple(sorted(bars_by_id.values(), key=lambda item: item.timestamp))
                if not ordered_bars:
                    continue
                series = _finite_series(history_query, ordered_bars)
                finite_calculations.extend(
                    self._finite_engine.compute_finite_windows(
                        series,
                        finite_request,
                        output_timestamps=output_timestamps,
                    )
                )
            totals["finite_calculation_elapsed_microseconds"] += _elapsed_microseconds(started)
            totals["finite_window_calculations"] += len(finite_calculations)

            all_recurrent: list[MetricCalculation] = []
            calculation_checkpoints: dict[UUID, MarketRecursiveCheckpoint] = {}
            started = perf_counter_ns()
            page_previous_projection = previous_projection
            for index, projection in enumerate(page_bars):
                if len(seed_projections) < seed_capacity:
                    seed_projections.append(projection)
                if output_query.start <= projection.timestamp < output_query.end:
                    checkpoint_map = {key: states[index] for key, states in states_by_key.items()}
                    calculations, associations = _calculations_for_bar(
                        projection=projection,
                        previous_projection=page_previous_projection,
                        checkpoints=checkpoint_map,
                        seed_projections=seed_projections,
                        query=output_query,
                    )
                    all_recurrent.extend(calculations)
                    calculation_checkpoints.update(associations)
                page_previous_projection = projection
            previous_projection = page_previous_projection

            page_calculations = [*finite_calculations, *all_recurrent]
            page_results = tuple(
                _metric_result(item, request.computed_at) for item in page_calculations
            )
            totals["metric_generation_elapsed_microseconds"] += _elapsed_microseconds(started)
            if page_results:
                started = perf_counter_ns()
                for batch in _topological_metric_batches(page_results):
                    receipt = self._staging.save_metrics(batch)
                    totals["metrics_created"] += len(receipt.created_ids)
                    totals["metrics_reused"] += len(receipt.reused_ids)
                    totals["metric_batches"] += 1
                    totals["maximum_metric_batch"] = max(
                        totals["maximum_metric_batch"],
                        len(batch),
                    )
                totals["metric_dag_persist_elapsed_microseconds"] += _elapsed_microseconds(started)
                started = perf_counter_ns()
                self._append_checkpoint_metric_references(
                    page_calculations,
                    calculation_checkpoints,
                    checkpoints_by_id,
                    verified_prefixes,
                )
                totals["checkpoint_metric_link_elapsed_microseconds"] += _elapsed_microseconds(
                    started
                )

            totals["bars_recalculated"] += len(missing_indexes)
            for index, projection in enumerate(page_bars):
                finite_projection_ring.append((close_prefixes[index].length - 1, projection))

        return IncrementalMarketReceipt(
            asset_id=output_query.asset_id,
            source_id=output_query.source_id,
            requested_start=output_query.start,
            requested_end=output_query.end,
            known_at=output_query.known_at,
            computed_at=request.computed_at,
            candidate_versions=totals["candidate_versions"],
            selected_bars=totals["selected_bars"],
            discarded_revisions=totals["discarded_revisions"],
            history_pages=totals["history_pages"],
            close_prefixes_created=totals["close_prefixes_created"],
            close_prefixes_reused=totals["close_prefixes_reused"],
            hlc_prefixes_created=totals["hlc_prefixes_created"],
            hlc_prefixes_reused=totals["hlc_prefixes_reused"],
            close_divergence_index=(
                first_close_divergence
                if first_close_divergence is not None
                else totals["selected_bars"]
            ),
            hlc_divergence_index=(
                first_hlc_divergence
                if first_hlc_divergence is not None
                else totals["selected_bars"]
            ),
            checkpoints_created=totals["checkpoints_created"],
            checkpoints_reused=totals["checkpoints_reused"],
            recurrence_steps=totals["recurrence_steps"],
            metrics_created=totals["metrics_created"],
            metrics_reused=totals["metrics_reused"],
            metric_batches=totals["metric_batches"],
            maximum_metric_batch=totals["maximum_metric_batch"],
            bars_recalculated=totals["bars_recalculated"],
            bar_models_hydrated=totals["bar_models_hydrated"],
            finite_bar_models_hydrated=totals["finite_bar_models_hydrated"],
            finite_window_calculations=totals["finite_window_calculations"],
            finite_window_halo=finite_halo,
            history_selection_elapsed_microseconds=totals["history_selection_elapsed_microseconds"],
            daily_prefix_build_elapsed_microseconds=totals[
                "daily_prefix_build_elapsed_microseconds"
            ],
            daily_prefix_persist_verify_elapsed_microseconds=totals[
                "daily_prefix_persist_verify_elapsed_microseconds"
            ],
            checkpoint_lookup_elapsed_microseconds=totals["checkpoint_lookup_elapsed_microseconds"],
            finite_presence_elapsed_microseconds=totals["finite_presence_elapsed_microseconds"],
            bar_materialization_elapsed_microseconds=totals[
                "bar_materialization_elapsed_microseconds"
            ],
            recurrence_transition_elapsed_microseconds=totals[
                "recurrence_transition_elapsed_microseconds"
            ],
            checkpoint_persist_verify_elapsed_microseconds=totals[
                "checkpoint_persist_verify_elapsed_microseconds"
            ],
            finite_calculation_elapsed_microseconds=totals[
                "finite_calculation_elapsed_microseconds"
            ],
            metric_generation_elapsed_microseconds=totals["metric_generation_elapsed_microseconds"],
            metric_dag_persist_elapsed_microseconds=totals[
                "metric_dag_persist_elapsed_microseconds"
            ],
            checkpoint_metric_link_elapsed_microseconds=totals[
                "checkpoint_metric_link_elapsed_microseconds"
            ],
            traceability_verified=True,
        )

    def _materialize_projections(
        self,
        projections: Collection[DailyMarketBarProjection],
        query: HistoricalBarQuery,
    ) -> dict[UUID, MarketBar]:
        typed = tuple({item.raw_record_id: item for item in projections}.values())
        output: dict[UUID, MarketBar] = {}
        for batch in _batches(typed):
            output.update(self._history.materialize_many(batch, query))
        return output

    def _append_checkpoint_metric_references(
        self,
        calculations: Sequence[MetricCalculation],
        calculation_checkpoints: Mapping[UUID, MarketRecursiveCheckpoint],
        checkpoints_by_id: Mapping[UUID, MarketRecursiveCheckpoint],
        verified_prefixes: Mapping[UUID, DailyEvidencePrefix],
    ) -> None:
        references_by_checkpoint: dict[UUID, dict[str, UUID]] = defaultdict(dict)
        for calculation in calculations:
            result_id = semantic_metric_result_id(calculation)
            checkpoint = calculation_checkpoints.get(result_id)
            if checkpoint is None:
                continue
            references_by_checkpoint[checkpoint.checkpoint_id][calculation.metric_key] = result_id
        updates: list[MarketRecursiveCheckpoint] = []
        for checkpoint_id, additions in references_by_checkpoint.items():
            checkpoint = checkpoints_by_id[checkpoint_id]
            references = {item.metric_key: item.result_id for item in checkpoint.metric_references}
            for metric_key, result_id in additions.items():
                existing_id = references.get(metric_key)
                if existing_id is not None and existing_id != result_id:
                    raise IncrementalStateError(
                        "checkpoint metric key already refers to a different result"
                    )
                references[metric_key] = result_id
            updates.append(
                checkpoint.model_copy(
                    update={
                        "metric_references": tuple(
                            CheckpointMetricReference(metric_key=key, result_id=value)
                            for key, value in sorted(references.items())
                        )
                    }
                )
            )
        for batch in _batches(updates):
            self._staging.save_market_recursive_checkpoints(
                batch,
                verified_prefixes=verified_prefixes,
            )


def _requested_parameters(request: MarketStatisticsRequest) -> tuple[RecursiveParameters, ...]:
    ema_windows = set(request.ema_windows)
    ema_windows.update((request.macd_fast_window, request.macd_slow_window))
    parameters: list[RecursiveParameters] = [
        *(EmaParameters(window=window) for window in sorted(ema_windows)),
        RsiParameters(window=request.rsi_window),
        AtrParameters(window=request.atr_window),
        MacdParameters(
            fast_window=request.macd_fast_window,
            slow_window=request.macd_slow_window,
            signal_window=request.macd_signal_window,
        ),
    ]
    return tuple(parameters)


def recurrence_key(parameters: RecursiveParameters) -> str:
    """Return a stable internal key that distinguishes family parameters."""
    return f"{parameters.family}:{parameters.model_dump_json()}"


def _finite_window_halo(request: MarketStatisticsRequest) -> int:
    return max(
        1,
        request.volatility_window,
        request.relative_volume_window,
        request.bollinger_window - 1,
        *(window - 1 for window in request.sma_windows),
    )


def _finite_metric_keys(request: MarketStatisticsRequest) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                SIMPLE_RETURN_KEY,
                SMA_KEY,
                VOLATILITY_KEY,
                RELATIVE_VOLUME_KEY,
                BOLLINGER_UPPER_KEY,
                BOLLINGER_LOWER_KEY,
                BOLLINGER_BANDWIDTH_KEY,
                BOLLINGER_PERCENT_B_KEY,
            }
        )
    )


def _finite_expected_keys(
    projection: DailyMarketBarProjection,
    context: Sequence[DailyMarketBarProjection],
    request: MarketStatisticsRequest,
) -> tuple[_FinitePresenceKey, ...]:
    """Describe exact existing finite outputs from projection-only input IDs."""
    output: list[_FinitePresenceKey] = []
    source_id = request.query.source_id

    def add(
        metric_key: str,
        parameters: dict[str, object],
        bars: Sequence[DailyMarketBarProjection],
        field: str,
        algorithm: str,
        unit: str,
    ) -> None:
        identifiers = tuple(item.observation_ids[field] for item in bars)
        output.append(
            (
                metric_key,
                projection.timestamp,
                _parameters_json(parameters),
                identifiers,
                algorithm,
                unit,
            )
        )

    if len(context) >= 2:
        add(
            SIMPLE_RETURN_KEY,
            {
                "periods": 1,
                "price_field": "close",
                "previous_bar_semantics": "previous_available_bar",
                "source_id": source_id,
            },
            context[-2:],
            "close",
            _RETURN_ALGORITHM,
            "ratio",
        )
    for window in request.sma_windows:
        if len(context) >= window:
            add(
                SMA_KEY,
                {
                    "window": window,
                    "price_field": "close",
                    "includes_current_bar": True,
                    "source_id": source_id,
                },
                context[-window:],
                "close",
                _SMA_ALGORITHM,
                "USD",
            )
    if len(context) >= request.volatility_window + 1:
        add(
            VOLATILITY_KEY,
            {
                "window": request.volatility_window,
                "return_type": "simple",
                "degrees_of_freedom": 1,
                "annualized": False,
                "source_id": source_id,
            },
            context[-(request.volatility_window + 1) :],
            "close",
            _VOLATILITY_ALGORITHM,
            "ratio",
        )
    if len(context) >= request.relative_volume_window + 1:
        volume_bars = context[-(request.relative_volume_window + 1) :]
        if any(item.values["volume"] != 0 for item in volume_bars[:-1]):
            add(
                RELATIVE_VOLUME_KEY,
                {
                    "window": request.relative_volume_window,
                    "comparison": "previous_available_bars",
                    "excludes_current_bar_from_baseline": True,
                    "source_id": source_id,
                },
                volume_bars,
                "volume",
                _RELATIVE_VOLUME_ALGORITHM,
                "ratio",
            )
    if len(context) >= request.bollinger_window:
        bollinger_bars = context[-request.bollinger_window :]
        parameters = {
            "window": request.bollinger_window,
            "multiplier": str(request.bollinger_multiplier),
            "price_field": "close",
            "degrees_of_freedom": 0,
            "includes_current_bar": True,
            "source_id": source_id,
        }
        for metric_key, unit in (
            (BOLLINGER_UPPER_KEY, "USD"),
            (BOLLINGER_LOWER_KEY, "USD"),
            (BOLLINGER_BANDWIDTH_KEY, "ratio"),
        ):
            add(
                metric_key,
                parameters,
                bollinger_bars,
                "close",
                _BOLLINGER_ALGORITHM,
                unit,
            )
        if len({item.values["close"] for item in bollinger_bars}) > 1:
            add(
                BOLLINGER_PERCENT_B_KEY,
                parameters,
                bollinger_bars,
                "close",
                _BOLLINGER_ALGORITHM,
                "ratio",
            )
    return tuple(output)


def _finite_presence_key(result: MetricResult) -> _FinitePresenceKey:
    return (
        result.metric_key,
        result.as_of,
        _parameters_json(result.parameters),
        tuple(result.input_observation_ids),
        result.algorithm_version,
        result.unit,
    )


def _parameters_json(parameters: Mapping[str, object]) -> str:
    return json.dumps(
        parameters,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _group_finite_outputs(
    page_bars: Sequence[DailyMarketBarProjection],
    close_prefixes: Sequence[DailyEvidencePrefix],
    previous_ring: deque[tuple[int, DailyMarketBarProjection]],
    output_timestamps: set[datetime],
    halo: int,
) -> tuple[tuple[tuple[DailyMarketBarProjection, ...], set[datetime]], ...]:
    """Build finite input slices only for output timestamps whose rows are absent."""
    if not output_timestamps:
        return ()
    context = list(previous_ring)
    context.extend(
        (prefix.length - 1, projection)
        for prefix, projection in zip(close_prefixes, page_bars, strict=True)
    )
    targets = sorted(
        (prefix.length - 1, projection.timestamp)
        for prefix, projection in zip(close_prefixes, page_bars, strict=True)
        if projection.timestamp in output_timestamps
    )
    groups: list[list[tuple[int, datetime]]] = []
    for target in targets:
        if groups and target[0] - groups[-1][-1][0] <= halo + 1:
            groups[-1].append(target)
        else:
            groups.append([target])
    output: list[tuple[tuple[DailyMarketBarProjection, ...], set[datetime]]] = []
    for group in groups:
        first_index = group[0][0]
        last_index = group[-1][0]
        start_index = max(0, first_index - halo)
        selected = tuple(
            projection for index, projection in context if start_index <= index <= last_index
        )
        if not selected or selected[-1].timestamp != group[-1][1]:
            raise IncrementalStateError("finite metric input window is incomplete")
        output.append((selected, {timestamp for _, timestamp in group}))
    return tuple(output)


def _build_prefix_page(
    bars: Sequence[DailyMarketBarProjection],
    field_group: DailyEvidenceFieldGroup,
    parent: DailyEvidencePrefix | None,
) -> tuple[tuple[DailyEvidencePrefix, ...], DailyEvidencePrefix | None]:
    output: list[DailyEvidencePrefix] = []
    for bar in bars:
        parent = bar.prefix(field_group, parent)
        output.append(parent)
    return tuple(output), parent


def _timed_pages(
    pages: Iterator[MarketHistoryPage],
) -> Iterator[tuple[MarketHistoryPage, int]]:
    """Measure page selection and projection separately from downstream work."""
    while True:
        started = perf_counter_ns()
        try:
            page = next(pages)
        except StopIteration:
            return
        yield page, _elapsed_microseconds(started)


def _elapsed_microseconds(started_at: int) -> int:
    return max(0, (perf_counter_ns() - started_at) // 1_000)


def _finite_series(
    query: HistoricalBarQuery,
    bars: Sequence[MarketBar],
) -> MarketBarSeries:
    ordered = tuple(sorted(bars, key=lambda item: item.timestamp))
    return MarketBarSeries(
        query=query,
        bars=ordered,
        coverage=MarketBarCoverage(
            candidate_versions=len(ordered),
            selected_versions=len(ordered),
            discarded_revisions=0,
            bar_count=len(ordered),
            earliest_timestamp=ordered[0].timestamp,
            latest_timestamp=ordered[-1].timestamp,
        ),
        traceability_verified=True,
    )


def _calculations_for_bar(
    *,
    projection: DailyMarketBarProjection,
    previous_projection: DailyMarketBarProjection | None,
    checkpoints: Mapping[str, MarketRecursiveCheckpoint],
    seed_projections: Sequence[DailyMarketBarProjection],
    query: HistoricalBarQuery,
) -> tuple[tuple[MetricCalculation, ...], dict[UUID, MarketRecursiveCheckpoint]]:
    calculations: list[MetricCalculation] = []
    checkpoint_by_result: dict[UUID, MarketRecursiveCheckpoint] = {}
    ema_calculations: dict[int, MetricCalculation] = {}
    ema_checkpoints: dict[int, MarketRecursiveCheckpoint] = {}

    for recurrence in checkpoints.values():
        if not isinstance(recurrence.parameters, EmaParameters):
            continue
        state = recurrence.state
        if not isinstance(state, EmaState) or state.value is None:
            continue
        calculation = _ema_calculation(recurrence, projection, seed_projections, query)
        ema_calculations[recurrence.parameters.window] = calculation
        ema_checkpoints[recurrence.parameters.window] = recurrence
        calculations.append(calculation)
        checkpoint_by_result[semantic_metric_result_id(calculation)] = recurrence

    rsi_checkpoint = _checkpoint_for_family(checkpoints, RsiParameters)
    if rsi_checkpoint is not None and isinstance(rsi_checkpoint.state, RsiState):
        state = rsi_checkpoint.state
        parameters = rsi_checkpoint.parameters
        if isinstance(parameters, RsiParameters) and state.value is not None:
            seed = state.change_count == parameters.window
            close_inputs = (
                tuple(
                    identifier
                    for item in seed_projections[: parameters.window + 1]
                    for identifier in (item.observation_ids["close"],)
                )
                if seed
                else (projection.observation_ids["close"],)
            )
            common = _checkpoint_parameters(rsi_checkpoint)
            average_parameters = {
                "window": parameters.window,
                "seed_method": "wilder_first_n_changes",
                "seed_start": rsi_checkpoint.seed_start.isoformat(),
                "price_field": "close",
                **common,
            }
            gain = MetricCalculation(
                asset_id=query.asset_id,
                source_id=query.source_id,
                metric_key=RSI_AVERAGE_GAIN_KEY,
                value=state.average_gain or Decimal("0"),
                unit="USD",
                as_of=projection.timestamp,
                available_at=rsi_checkpoint.available_at,
                parameters=average_parameters,
                input_observation_ids=close_inputs,
                algorithm_version=RSI_V2_ALGORITHM,
                quality=state.quality,
            )
            loss = MetricCalculation(
                asset_id=query.asset_id,
                source_id=query.source_id,
                metric_key=RSI_AVERAGE_LOSS_KEY,
                value=state.average_loss or Decimal("0"),
                unit="USD",
                as_of=projection.timestamp,
                available_at=rsi_checkpoint.available_at,
                parameters=average_parameters,
                input_observation_ids=close_inputs,
                algorithm_version=RSI_V2_ALGORITHM,
                quality=state.quality,
            )
            gain_id = semantic_metric_result_id(gain)
            loss_id = semantic_metric_result_id(loss)
            rsi = MetricCalculation(
                asset_id=query.asset_id,
                source_id=query.source_id,
                metric_key=RSI_KEY,
                value=state.value,
                unit="index",
                as_of=projection.timestamp,
                available_at=rsi_checkpoint.available_at,
                parameters=average_parameters,
                input_observation_ids=(projection.observation_ids["close"],),
                input_metric_result_ids=(gain_id, loss_id),
                algorithm_version=RSI_V2_ALGORITHM,
                quality=state.quality,
            )
            calculations.extend((gain, loss, rsi))
            for item in (gain, loss, rsi):
                checkpoint_by_result[semantic_metric_result_id(item)] = rsi_checkpoint

    atr_checkpoint = _checkpoint_for_family(checkpoints, AtrParameters)
    if atr_checkpoint is not None and isinstance(atr_checkpoint.state, AtrState):
        state = atr_checkpoint.state
        parameters = atr_checkpoint.parameters
        if isinstance(parameters, AtrParameters):
            common = _checkpoint_parameters(atr_checkpoint)
            base_parameters = {
                "window": parameters.window,
                "seed_method": "mean_first_n_true_ranges",
                "seed_start": atr_checkpoint.seed_start.isoformat(),
                **common,
            }
            true_range = MetricCalculation(
                asset_id=query.asset_id,
                source_id=query.source_id,
                metric_key=TRUE_RANGE_KEY,
                value=state.true_range,
                unit="USD",
                as_of=projection.timestamp,
                available_at=atr_checkpoint.available_at,
                parameters={
                    "first_bar_method": "high_low_only",
                    **base_parameters,
                },
                input_observation_ids=_true_range_input_ids(previous_projection, projection),
                algorithm_version=ATR_V2_ALGORITHM,
                quality=state.quality,
            )
            calculations.append(true_range)
            checkpoint_by_result[semantic_metric_result_id(true_range)] = atr_checkpoint
            if state.value is not None:
                seed = state.bars_seen == parameters.window
                atr_inputs = (
                    tuple(
                        identifier
                        for item in seed_projections[: parameters.window]
                        for identifier in (
                            item.observation_ids["high"],
                            item.observation_ids["low"],
                            item.observation_ids["close"],
                        )
                    )
                    if seed
                    else (
                        projection.observation_ids["high"],
                        projection.observation_ids["low"],
                        projection.observation_ids["close"],
                    )
                )
                atr = MetricCalculation(
                    asset_id=query.asset_id,
                    source_id=query.source_id,
                    metric_key=ATR_KEY,
                    value=state.value,
                    unit="USD",
                    as_of=projection.timestamp,
                    available_at=atr_checkpoint.available_at,
                    parameters=base_parameters,
                    input_observation_ids=atr_inputs,
                    input_metric_result_ids=(semantic_metric_result_id(true_range),),
                    algorithm_version=ATR_V2_ALGORITHM,
                    quality=state.quality,
                )
                calculations.append(atr)
                checkpoint_by_result[semantic_metric_result_id(atr)] = atr_checkpoint

    macd_checkpoint = _checkpoint_for_family(checkpoints, MacdParameters)
    if macd_checkpoint is not None and isinstance(macd_checkpoint.state, MacdState):
        state = macd_checkpoint.state
        parameters = macd_checkpoint.parameters
        if isinstance(parameters, MacdParameters) and state.line is not None:
            fast_calculation = ema_calculations.get(parameters.fast_window)
            slow_calculation = ema_calculations.get(parameters.slow_window)
            fast_checkpoint = ema_checkpoints.get(parameters.fast_window)
            slow_checkpoint = ema_checkpoints.get(parameters.slow_window)
            if (
                fast_calculation is None
                or slow_calculation is None
                or fast_checkpoint is None
                or slow_checkpoint is None
                or state.fast_ema != fast_calculation.value
                or state.slow_ema != slow_calculation.value
            ):
                raise IncrementalStateError("MACD EMA state does not match its EMA checkpoints")
            common = _checkpoint_parameters(macd_checkpoint)
            base_parameters = {
                "fast_window": parameters.fast_window,
                "slow_window": parameters.slow_window,
                "signal_window": parameters.signal_window,
                "seed_method": "sma_first_signal_lines",
                "seed_start": macd_checkpoint.seed_start.isoformat(),
                **common,
            }
            line = MetricCalculation(
                asset_id=query.asset_id,
                source_id=query.source_id,
                metric_key=MACD_LINE_KEY,
                value=state.line,
                unit="USD",
                as_of=projection.timestamp,
                available_at=macd_checkpoint.available_at,
                parameters=base_parameters,
                input_observation_ids=(projection.observation_ids["close"],),
                input_metric_result_ids=(
                    semantic_metric_result_id(fast_calculation),
                    semantic_metric_result_id(slow_calculation),
                ),
                algorithm_version=MACD_V2_ALGORITHM,
                quality=state.quality,
            )
            calculations.append(line)
            checkpoint_by_result[semantic_metric_result_id(line)] = macd_checkpoint
            if state.signal is not None:
                alpha = Decimal("2") / Decimal(parameters.signal_window + 1)
                signal = MetricCalculation(
                    asset_id=query.asset_id,
                    source_id=query.source_id,
                    metric_key=MACD_SIGNAL_KEY,
                    value=state.signal,
                    unit="USD",
                    as_of=projection.timestamp,
                    available_at=macd_checkpoint.available_at,
                    parameters={**base_parameters, "alpha": str(alpha)},
                    input_observation_ids=(projection.observation_ids["close"],),
                    input_metric_result_ids=(semantic_metric_result_id(line),),
                    algorithm_version=MACD_V2_ALGORITHM,
                    quality=state.quality,
                )
                calculations.append(signal)
                checkpoint_by_result[semantic_metric_result_id(signal)] = macd_checkpoint
                if state.histogram is not None:
                    histogram = MetricCalculation(
                        asset_id=query.asset_id,
                        source_id=query.source_id,
                        metric_key=MACD_HISTOGRAM_KEY,
                        value=state.histogram,
                        unit="USD",
                        as_of=projection.timestamp,
                        available_at=macd_checkpoint.available_at,
                        parameters=base_parameters,
                        input_observation_ids=(projection.observation_ids["close"],),
                        input_metric_result_ids=(
                            semantic_metric_result_id(line),
                            semantic_metric_result_id(signal),
                        ),
                        algorithm_version=MACD_V2_ALGORITHM,
                        quality=state.quality,
                    )
                    calculations.append(histogram)
                    checkpoint_by_result[semantic_metric_result_id(histogram)] = macd_checkpoint
    return tuple(calculations), checkpoint_by_result


def _ema_calculation(
    checkpoint: MarketRecursiveCheckpoint,
    projection: DailyMarketBarProjection,
    seed_projections: Sequence[DailyMarketBarProjection],
    query: HistoricalBarQuery,
) -> MetricCalculation:
    if not isinstance(checkpoint.parameters, EmaParameters) or not isinstance(
        checkpoint.state, EmaState
    ):
        raise IncrementalStateError("EMA calculation requires an EMA checkpoint")
    window = checkpoint.parameters.window
    seed = checkpoint.state.bars_seen == window
    if seed and len(seed_projections) < window:
        raise IncrementalStateError("EMA seed observations are not available")
    input_ids = (
        tuple(item.observation_ids["close"] for item in seed_projections[:window])
        if seed
        else (projection.observation_ids["close"],)
    )
    return MetricCalculation(
        asset_id=query.asset_id,
        source_id=query.source_id,
        metric_key=EMA_KEY,
        value=checkpoint.state.value or Decimal("0"),
        unit="USD",
        as_of=projection.timestamp,
        available_at=checkpoint.available_at,
        parameters={
            "window": window,
            "alpha": str(Decimal("2") / Decimal(window + 1)),
            "seed_method": "sma_first_window",
            "seed_start": checkpoint.seed_start.isoformat(),
            "price_field": "close",
            "includes_current_bar": True,
            **_checkpoint_parameters(checkpoint),
        },
        input_observation_ids=input_ids,
        algorithm_version=EMA_V2_ALGORITHM,
        quality=checkpoint.state.quality,
    )


def _checkpoint_parameters(checkpoint: MarketRecursiveCheckpoint) -> dict[str, str]:
    return {
        "source_id": checkpoint.source_id,
        "daily_evidence_prefix_id": str(checkpoint.daily_prefix_id),
        "market_checkpoint_id": str(checkpoint.checkpoint_id),
    }


def _checkpoint_for_family(
    checkpoints: Mapping[str, MarketRecursiveCheckpoint],
    parameter_type: type,
) -> MarketRecursiveCheckpoint | None:
    return next(
        (item for item in checkpoints.values() if isinstance(item.parameters, parameter_type)),
        None,
    )


def _true_range_input_ids(
    previous: DailyMarketBarProjection | None,
    current: DailyMarketBarProjection,
) -> tuple[UUID, ...]:
    if previous is None:
        return (current.observation_ids["high"], current.observation_ids["low"])
    return (
        previous.observation_ids["high"],
        current.observation_ids["high"],
        previous.observation_ids["low"],
        current.observation_ids["low"],
        previous.observation_ids["close"],
    )


def _metric_result(
    calculation: MetricCalculation,
    computed_at: datetime,
) -> MetricResult:
    return MetricResult(
        result_id=semantic_metric_result_id(calculation),
        asset_id=calculation.asset_id,
        metric_key=calculation.metric_key,
        value=calculation.value,
        unit=calculation.unit,
        as_of=calculation.as_of,
        available_at=calculation.available_at,
        computed_at=computed_at,
        parameters=dict(calculation.parameters),
        input_observation_ids=list(calculation.input_observation_ids),
        input_metric_result_ids=list(calculation.input_metric_result_ids),
        algorithm_version=calculation.algorithm_version,
        quality=calculation.quality,
    )


def _topological_metric_batches(
    results: Sequence[MetricResult],
) -> tuple[tuple[MetricResult, ...], ...]:
    """Sort one page's analytical DAG and split persistence into <=256 rows."""
    by_id = {item.result_id: item for item in results}
    in_degree = {
        identifier: sum(dependency in by_id for dependency in item.input_metric_result_ids)
        for identifier, item in by_id.items()
    }
    children: dict[UUID, list[UUID]] = defaultdict(list)
    for identifier, item in by_id.items():
        for dependency in item.input_metric_result_ids:
            if dependency in by_id:
                children[dependency].append(identifier)
    ready = [str(identifier) for identifier, degree in in_degree.items() if degree == 0]
    heapq.heapify(ready)
    ordered: list[MetricResult] = []
    while ready:
        identifier = UUID(heapq.heappop(ready))
        ordered.append(by_id[identifier])
        for child in children.get(identifier, ()):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                heapq.heappush(ready, str(child))
    if len(ordered) != len(by_id):
        raise IncrementalStateError("metric calculation graph contains a cycle")
    return _batches(ordered)


def _batches[T](items: Sequence[T]) -> tuple[tuple[T, ...], ...]:
    return tuple(tuple(items[start : start + 256]) for start in range(0, len(items), 256))


__all__ = [
    "IncrementalMarketReceipt",
    "IncrementalMarketRequest",
    "IncrementalMarketService",
]
