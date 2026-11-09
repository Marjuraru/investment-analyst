"""Read-only descriptive outcomes for the two incremental screening conditions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from uuid import UUID

from investment_analyst.alerts.analytical_engine import (
    AmbiguousAnalyticalMetricError,
    AnalyticalScreeningEngine,
    AnalyticalScreeningError,
    IncompatibleAnalyticalMetricError,
)
from investment_analyst.alerts.analytical_models import (
    AnalyticalConditionState,
    AnalyticalScreeningRequest,
    AnalyticalScreeningRule,
)
from investment_analyst.alerts.descriptive_outcome_models import (
    MAX_DESCRIPTIVE_OUTCOME_BYTES,
    ConditionEvidenceReason,
    DescriptiveCloseObservation,
    DescriptiveConditionEvidence,
    DescriptiveConditionOutcomeReport,
    DescriptiveOutcomeEvidence,
    DescriptiveOutcomeGroup,
    DescriptiveOutcomeRequest,
    DescriptiveOutcomeStatus,
    DescriptivePeriodOutcome,
    descriptive_outcome_report_id,
    forward_close_count,
    forward_window_end,
    summarize_descriptive_rows,
)
from investment_analyst.analytics.analytical_access_models import (
    MetricIndexEntry,
    MetricPageCursor,
    MetricSeriesQuery,
    canonical_analytical_json,
)
from investment_analyst.analytics.market.bar_schemas import get_market_bar_schema
from investment_analyst.analytics.market.history_service import (
    LatestRevisionState,
    consider_revision,
)
from investment_analyst.analytics.metric_identity_cut import (
    CutIdentityVersion,
    LegacyKnownAtState,
    MetricCutContractError,
    metric_cut_eligibility,
    probe_legacy_known_at,
    resolve_cut_identity_version,
)
from investment_analyst.application.market_universe import (
    MarketAssetDescriptor,
    build_market_asset_universe,
)
from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    MetricResult,
    NormalizedObservation,
)
from investment_analyst.storage import LocalStorage
from investment_analyst.workspace.models import WorkspaceAccessMode

SUPPORTED_DESCRIPTIVE_OUTCOME_RULE_IDS: frozenset[str] = frozenset(
    {
        "market.technical.rsi-incremental-low-review",
        "market.technical.macd-incremental-positive-histogram-review",
    }
)

_MAX_METRIC_PAGE = 256
_MAX_METRIC_ENTRIES = 4096
_MAX_OBSERVATIONS = 4096
_MAX_METRIC_PAGES = 256
_QUALITY_INVALID = frozenset({DataQuality.SUSPECT})
_DAILY_FREQUENCY = "day_1"


class DescriptiveOutcomeError(RuntimeError):
    """Base error for unavailable or invalid descriptive outcome evidence."""


class DescriptiveOutcomeUnsupportedError(DescriptiveOutcomeError):
    """Raised when the requested rule, asset, or workspace is outside the contract."""


class DescriptiveOutcomeFingerprintChangedError(DescriptiveOutcomeError):
    """Raised when the saved rule differs from the requested fingerprint."""


class DescriptiveOutcomeBudgetExceededError(DescriptiveOutcomeError):
    """Raised when one bounded read would exceed its declared budget."""


class DescriptiveOutcomeIntegrityError(DescriptiveOutcomeError):
    """Raised when persisted index or hydrated evidence is internally inconsistent."""


@dataclass(frozen=True, slots=True)
class _MetricCandidate:
    result: MetricResult
    invalid_reason: str | None


@dataclass(frozen=True, slots=True)
class _ClosePeriod:
    start: datetime
    end: datetime
    observation: NormalizedObservation | None
    ambiguous: bool
    invalid_reason: str | None
    excluded: bool


class DescriptiveConditionOutcomeService:
    """Evaluate one saved incremental rule over persisted point-in-time evidence."""

    def __init__(
        self,
        runtime: ApplicationRuntime,
        workspace: Path,
        rule: AnalyticalScreeningRule,
        *,
        engine: AnalyticalScreeningEngine | None = None,
    ) -> None:
        if rule.rule_id not in SUPPORTED_DESCRIPTIVE_OUTCOME_RULE_IDS:
            raise DescriptiveOutcomeUnsupportedError(
                "the analytical rule is not supported by descriptive outcomes"
            )
        if len(rule.conditions) != 1:
            raise DescriptiveOutcomeUnsupportedError(
                "descriptive outcomes require exactly one daily condition"
            )
        self._runtime = runtime
        self._workspace = workspace.expanduser().resolve(strict=False)
        self._rule = rule
        self._engine = engine or AnalyticalScreeningEngine()

    def run(self, request: DescriptiveOutcomeRequest) -> DescriptiveConditionOutcomeReport:
        """Return one deterministic read-only report without provider or domain writes."""
        if request.rule_id != self._rule.rule_id:
            raise DescriptiveOutcomeUnsupportedError(
                "the requested rule is not the supplied immutable rule"
            )
        rule_fingerprint = self._rule.semantic_fingerprint()
        if request.expected_rule_fingerprint != rule_fingerprint:
            raise DescriptiveOutcomeFingerprintChangedError(
                "the saved rule fingerprint changed before the query"
            )
        descriptor = self._descriptor(request.asset_id)
        if descriptor.asset_class not in self._rule.asset_classes:
            raise DescriptiveOutcomeUnsupportedError(
                "the analytical rule is not compatible with this asset class"
            )
        self._require_daily_source(descriptor.source_id)
        location = StorageLocationRequest(workspace=self._workspace)
        resolved = self._runtime.resolve_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        )
        if resolved.workspace_id is None:
            raise DescriptiveOutcomeUnsupportedError(
                "descriptive outcomes require a workspace identity"
            )
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            metrics = self._read_metrics(storage, request, descriptor)
            closes = self._read_closes(storage, request, descriptor)
        rows = self._evaluate_rows(request, descriptor, metrics, closes)
        groups = summarize_descriptive_rows(rows)
        report = DescriptiveConditionOutcomeReport(
            report_id=_report_id(
                request=request,
                rule=self._rule,
                rule_fingerprint=rule_fingerprint,
                asset=descriptor,
                workspace_id=resolved.workspace_id,
                rows=rows,
                groups=groups,
            ),
            request=request,
            rule_id=self._rule.rule_id,
            rule_version=self._rule.rule_version,
            rule_fingerprint=rule_fingerprint,
            asset_id=descriptor.asset_id,
            asset_class=descriptor.asset_class,
            source_id=descriptor.source_id,
            currency=descriptor.quote_currency,
            workspace_id=resolved.workspace_id,
            requested_calendar_days=request.requested_calendar_days,
            observed_periods=len(rows),
            matched_count=sum(item.group is DescriptiveOutcomeGroup.MATCHED for item in rows),
            not_matched_count=sum(
                item.group is DescriptiveOutcomeGroup.NOT_MATCHED for item in rows
            ),
            not_evaluable_count=sum(
                item.group is DescriptiveOutcomeGroup.NOT_EVALUABLE for item in rows
            ),
            rows=rows,
            groups=groups,
            latest_evidence_available_at=_latest_evidence_available_at(rows),
            limitations=_limitations(descriptor),
        )
        if len(report.to_json_bytes()) > MAX_DESCRIPTIVE_OUTCOME_BYTES:
            raise DescriptiveOutcomeBudgetExceededError(
                "the descriptive outcome report exceeds 256 KiB"
            )
        return report

    def _descriptor(self, asset_id: str) -> MarketAssetDescriptor:
        universe = build_market_asset_universe(
            self._runtime.catalog,
            self._runtime.provider_resolver,
        )
        matches = tuple(item for item in universe.assets if item.asset_id == asset_id)
        if len(matches) != 1:
            raise DescriptiveOutcomeUnsupportedError(
                "descriptive outcomes asset_id is not supported"
            )
        return matches[0]

    @staticmethod
    def _require_daily_source(source_id: str) -> None:
        try:
            schema = get_market_bar_schema(source_id)
        except ValueError as error:
            raise DescriptiveOutcomeUnsupportedError(
                "descriptive outcomes require a registered market source"
            ) from error
        if schema.frequency is not DataFrequency.DAY_1:
            raise DescriptiveOutcomeUnsupportedError(
                "descriptive outcomes require a daily market source"
            )

    def _read_metrics(
        self,
        storage: LocalStorage,
        request: DescriptiveOutcomeRequest,
        descriptor: MarketAssetDescriptor,
    ) -> dict[datetime, tuple[_MetricCandidate, ...]]:
        metric_keys = tuple(sorted({item.metric_key for item in self._rule.conditions}))
        query = MetricSeriesQuery(
            asset_id=descriptor.asset_id,
            domain="market",
            known_at=request.known_at,
            metric_keys=metric_keys,
            source_id=descriptor.source_id,
            as_of_from=request.start_utc,
            as_of_before=request.end_before_utc,
            limit=_MAX_METRIC_PAGE,
        )
        grouped: dict[datetime, list[_MetricCandidate]] = {}
        seen_ids: set[UUID] = set()
        cursor: MetricPageCursor | None = None
        visible_count = 0
        pages = 0
        while True:
            pages += 1
            if pages > _MAX_METRIC_PAGES:
                raise DescriptiveOutcomeBudgetExceededError(
                    "the metric index exceeded the bounded page count"
                )
            page_query = query if cursor is None else query.model_copy(update={"after": cursor})
            entries = storage.metric_results.list_metric_index_page(page_query)
            if len(entries) > _MAX_METRIC_PAGE:
                raise DescriptiveOutcomeIntegrityError(
                    "the metric index exceeded the bounded page size"
                )
            self._validate_index_page(entries, page_query)
            visible: list[MetricIndexEntry] = []
            for entry in entries:
                if entry.result_id in seen_ids:
                    raise DescriptiveOutcomeIntegrityError(
                        "the metric index repeated a prior page result"
                    )
                seen_ids.add(entry.result_id)
                if _index_visible_at_cut(entry, request.known_at):
                    visible.append(entry)
            visible_count += len(visible)
            if visible_count > _MAX_METRIC_ENTRIES:
                raise DescriptiveOutcomeBudgetExceededError(
                    "the metric index exceeded 4096 relevant entries"
                )
            hydrated = self._hydrate_metrics(storage, visible)
            for entry in visible:
                result = hydrated[entry.result_id]
                self._validate_hydrated_metric(entry, result, query)
                grouped.setdefault(result.as_of, []).append(
                    _MetricCandidate(
                        result=result,
                        invalid_reason=_metric_invalid_reason(result, request.known_at),
                    )
                )
            if len(entries) < _MAX_METRIC_PAGE:
                break
            next_cursor = MetricPageCursor(
                available_at=entries[-1].available_at,
                result_id=entries[-1].result_id,
            )
            if cursor == next_cursor:
                raise DescriptiveOutcomeIntegrityError(
                    "the metric index did not advance its page cursor"
                )
            cursor = next_cursor
        if len(grouped) > 90:
            raise DescriptiveOutcomeBudgetExceededError(
                "the metric index contains more than 90 daily periods"
            )
        dates = [period.date() for period in grouped]
        if len(set(dates)) != len(dates):
            raise DescriptiveOutcomeIntegrityError(
                "the metric index contains more than one daily period per UTC date"
            )
        return {
            period: tuple(sorted(items, key=lambda item: str(item.result.result_id)))
            for period, items in sorted(grouped.items())
        }

    @staticmethod
    def _validate_index_page(
        entries: Sequence[MetricIndexEntry],
        query: MetricSeriesQuery,
    ) -> None:
        previous: tuple[datetime, str] | None = None
        for entry in entries:
            if (
                entry.asset_id != query.asset_id
                or entry.metric_key not in query.metric_keys
                or entry.source_id != query.source_id
                or (entry.frequency is not None and entry.frequency != _DAILY_FREQUENCY)
            ):
                raise DescriptiveOutcomeIntegrityError(
                    "the metric index returned evidence outside the requested scope"
                )
            if entry.available_at > query.known_at:
                raise DescriptiveOutcomeIntegrityError(
                    "the metric index returned evidence unavailable at known_at"
                )
            if query.as_of_from is not None and entry.as_of < query.as_of_from:
                raise DescriptiveOutcomeIntegrityError(
                    "the metric index returned evidence before the requested range"
                )
            if query.as_of_before is not None and entry.as_of >= query.as_of_before:
                raise DescriptiveOutcomeIntegrityError(
                    "the metric index returned evidence after the requested range"
                )
            key = (entry.available_at, str(entry.result_id))
            if previous is not None and key <= previous:
                raise DescriptiveOutcomeIntegrityError(
                    "the metric index page is not in stable keyset order"
                )
            if query.after is not None and key <= (
                query.after.available_at,
                str(query.after.result_id),
            ):
                raise DescriptiveOutcomeIntegrityError(
                    "the metric index page did not advance its cursor"
                )
            previous = key

    @staticmethod
    def _hydrate_metrics(
        storage: LocalStorage,
        entries: Sequence[MetricIndexEntry],
    ) -> dict[UUID, MetricResult]:
        output: dict[UUID, MetricResult] = {}
        ordered = tuple(item.result_id for item in entries)
        for start in range(0, len(ordered), _MAX_METRIC_PAGE):
            chunk = ordered[start : start + _MAX_METRIC_PAGE]
            output.update(storage.metric_results.get_many(chunk))
        if set(output) != set(ordered):
            raise DescriptiveOutcomeIntegrityError(
                "the metric index references a missing metric result"
            )
        return output

    @staticmethod
    def _validate_hydrated_metric(
        entry: MetricIndexEntry,
        result: MetricResult,
        query: MetricSeriesQuery,
    ) -> None:
        if (
            result.result_id != entry.result_id
            or result.asset_id != entry.asset_id
            or result.metric_key != entry.metric_key
            or result.as_of != entry.as_of
            or result.available_at != entry.available_at
            or result.parameters.get("source_id") != query.source_id
            or result.parameters.get("known_at") != entry.legacy_known_at
        ):
            raise DescriptiveOutcomeIntegrityError(
                "the metric index projection changed during hydration"
            )
        declared_frequency = result.parameters.get("frequency")
        if declared_frequency is not None and declared_frequency != _DAILY_FREQUENCY:
            raise DescriptiveOutcomeIntegrityError(
                "the hydrated metric is not a daily market metric"
            )
        if result.available_at > query.known_at:
            raise DescriptiveOutcomeIntegrityError("the hydrated metric is unavailable at known_at")

    def _read_closes(
        self,
        storage: LocalStorage,
        request: DescriptiveOutcomeRequest,
        descriptor: MarketAssetDescriptor,
    ) -> tuple[_ClosePeriod, ...]:
        observed_before = request.known_at
        count = storage.observations.count(
            asset_id=descriptor.asset_id,
            source_id=descriptor.source_id,
            frequency=DataFrequency.DAY_1,
            field_name="close",
            observed_from=request.start_utc,
            observed_before=observed_before,
            available_to=request.known_at,
        )
        if count > _MAX_OBSERVATIONS:
            raise DescriptiveOutcomeBudgetExceededError(
                "the close evidence exceeded 4096 observations"
            )
        rows = storage.observations.list(
            asset_id=descriptor.asset_id,
            source_id=descriptor.source_id,
            frequency=DataFrequency.DAY_1,
            observed_from=request.start_utc,
            observed_before=observed_before,
            available_to=request.known_at,
            field_name="close",
        )
        if len(rows) > _MAX_OBSERVATIONS:
            raise DescriptiveOutcomeBudgetExceededError(
                "the close evidence exceeded 4096 observations"
            )
        states: dict[datetime, LatestRevisionState[NormalizedObservation]] = {}
        ends: dict[datetime, set[datetime]] = {}
        for row in rows:
            self._validate_observation_scope(row, request, descriptor)
            start, end = _close_bounds(row)
            if start is None or end is None:
                raise DescriptiveOutcomeIntegrityError("close evidence lacks a usable daily period")
            if end > request.known_at or row.available_at > request.known_at:
                continue
            states[start] = consider_revision(
                states.get(start, LatestRevisionState()),
                row,
                available_at=row.available_at,
            )
            ends.setdefault(start, set()).add(end)
        periods: list[_ClosePeriod] = []
        for start in sorted(states):
            state = states[start]
            period_ends = ends[start]
            if len(period_ends) != 1:
                periods.append(
                    _ClosePeriod(
                        start=start,
                        end=min(period_ends),
                        observation=None,
                        ambiguous=True,
                        invalid_reason=None,
                        excluded=False,
                    )
                )
                continue
            end = next(iter(period_ends))
            if state.candidate is None or state.latest_ties != 1:
                periods.append(
                    _ClosePeriod(
                        start=start,
                        end=end,
                        observation=None,
                        ambiguous=True,
                        invalid_reason=None,
                        excluded=False,
                    )
                )
                continue
            observation = state.candidate
            excluded_reason = None
            if observation.unit != descriptor.quote_currency:
                excluded_reason = "currency_mismatch"
            elif observation.observed_at is None:
                excluded_reason = "missing_observed_at"
            if excluded_reason is not None:
                periods.append(
                    _ClosePeriod(
                        start=start,
                        end=end,
                        observation=observation,
                        ambiguous=False,
                        invalid_reason=excluded_reason,
                        excluded=True,
                    )
                )
                continue
            periods.append(
                _ClosePeriod(
                    start=start,
                    end=end,
                    observation=observation,
                    ambiguous=False,
                    invalid_reason=_close_invalid_reason(observation),
                    excluded=False,
                )
            )
        return tuple(periods)

    @staticmethod
    def _validate_observation_scope(
        row: NormalizedObservation,
        request: DescriptiveOutcomeRequest,
        descriptor: MarketAssetDescriptor,
    ) -> None:
        if (
            row.asset_id != descriptor.asset_id
            or row.source.source_id != descriptor.source_id
            or row.frequency is not DataFrequency.DAY_1
            or row.field_name != "close"
        ):
            raise DescriptiveOutcomeIntegrityError(
                "close evidence is outside the requested asset, source, or frequency"
            )
        if row.observed_at is None and row.period_start is None:
            raise DescriptiveOutcomeIntegrityError("close evidence lacks an observed period")
        if row.available_at > request.known_at:
            raise DescriptiveOutcomeIntegrityError("close evidence is unavailable at known_at")

    def _evaluate_rows(
        self,
        request: DescriptiveOutcomeRequest,
        descriptor: MarketAssetDescriptor,
        metrics: dict[datetime, tuple[_MetricCandidate, ...]],
        closes: tuple[_ClosePeriod, ...],
    ) -> tuple[DescriptivePeriodOutcome, ...]:
        rows: list[DescriptivePeriodOutcome] = []
        for period_as_of, candidates in sorted(metrics.items()):
            selected: list[MetricResult] = []
            reason: ConditionEvidenceReason | None = None
            invalid_reason: str | None = None
            for condition in self._rule.conditions:
                metric, selected_reason, selected_invalid_reason = self._select_metric(
                    condition,
                    candidates,
                    descriptor.source_id,
                    request.known_at,
                    period_as_of,
                )
                if selected_reason is not None:
                    reason = selected_reason
                    invalid_reason = selected_invalid_reason
                    break
                if metric is None:
                    raise DescriptiveOutcomeIntegrityError(
                        "metric selection returned neither evidence nor a reason"
                    )
                selected.append(metric)
            if reason is not None:
                condition = self._rule.conditions[0]
                rows.append(
                    DescriptivePeriodOutcome(
                        period_as_of=period_as_of,
                        group=DescriptiveOutcomeGroup.NOT_EVALUABLE,
                        condition=DescriptiveConditionEvidence(
                            state=AnalyticalConditionState.NOT_EVALUABLE,
                            reason=reason,
                            metric_key=condition.metric_key,
                            operator=condition.operator,
                            threshold=condition.threshold,
                            retention_threshold=condition.retention_threshold,
                            unit=condition.unit,
                        ),
                        outcome=DescriptiveOutcomeEvidence(
                            status=DescriptiveOutcomeStatus.CONDITION_NOT_EVALUABLE,
                            reason=invalid_reason or reason.value,
                            observed_intervals=0,
                        ),
                    )
                )
                continue
            eval_at = max(item.available_at for item in selected)
            try:
                screening = self._engine.evaluate(
                    AnalyticalScreeningRequest(
                        rule=self._rule,
                        asset_id=descriptor.asset_id,
                        asset_class=descriptor.asset_class,
                        source_id=descriptor.source_id,
                        known_at=eval_at,
                        computed_at=eval_at,
                        metrics=tuple(selected),
                    )
                )
            except AmbiguousAnalyticalMetricError as error:
                rows.append(
                    self._unevaluable_row(
                        period_as_of=period_as_of,
                        reason=ConditionEvidenceReason.AMBIGUOUS_METRIC,
                        detail=str(error),
                    )
                )
                continue
            except IncompatibleAnalyticalMetricError as error:
                rows.append(
                    self._unevaluable_row(
                        period_as_of=period_as_of,
                        reason=ConditionEvidenceReason.INVALID_METRIC,
                        detail=str(error),
                    )
                )
                continue
            except AnalyticalScreeningError as error:
                rows.append(
                    self._unevaluable_row(
                        period_as_of=period_as_of,
                        reason=ConditionEvidenceReason.INVALID_METRIC,
                        detail=str(error),
                    )
                )
                continue
            if any(
                item.state is AnalyticalConditionState.NOT_EVALUABLE
                for item in screening.conditions
            ):
                rows.append(
                    self._unevaluable_row(
                        period_as_of=period_as_of,
                        reason=ConditionEvidenceReason.NO_COMPATIBLE_METRIC,
                        detail=None,
                    )
                )
                continue
            condition_results = {item.condition_id: item for item in screening.conditions}
            evidence = tuple(
                self._condition_evidence(
                    condition,
                    condition_results[condition.condition_id],
                    metric,
                    screening.result_id,
                )
                for condition, metric in zip(self._rule.conditions, selected, strict=True)
            )
            group = (
                DescriptiveOutcomeGroup.MATCHED
                if screening.matched
                else DescriptiveOutcomeGroup.NOT_MATCHED
            )
            rows.append(
                DescriptivePeriodOutcome(
                    period_as_of=period_as_of,
                    group=group,
                    condition=evidence[0],
                    outcome=self._outcome_evidence(
                        eval_at,
                        request.known_at,
                        closes,
                    ),
                )
            )
        return tuple(rows)

    def _select_metric(
        self,
        condition,
        candidates: Sequence[_MetricCandidate],
        source_id: str,
        known_at: datetime,
        period_as_of: datetime,
    ) -> tuple[MetricResult | None, ConditionEvidenceReason | None, str | None]:
        exact = tuple(
            item
            for item in candidates
            if item.result.metric_key == condition.metric_key
            and item.result.algorithm_version == condition.algorithm_version
            and item.result.unit == condition.unit
            and item.result.quality in condition.accepted_qualities
            and item.result.parameters.get("source_id") == source_id
            and item.result.as_of == period_as_of
            and item.result.available_at <= known_at
            and item.result.available_at >= item.result.as_of
            and all(
                item.result.parameters.get(name) == expected
                for name, expected in condition.parameter_filters.items()
            )
        )
        if not exact:
            return None, ConditionEvidenceReason.NO_COMPATIBLE_METRIC, None
        invalid = next(
            (item for item in exact if item.invalid_reason is not None),
            None,
        )
        eligible = tuple(item for item in exact if item.invalid_reason is None)
        if not eligible:
            return (
                None,
                ConditionEvidenceReason.INVALID_METRIC,
                invalid.invalid_reason if invalid is not None else None,
            )
        minimum = min(item.result.available_at for item in eligible)
        at_minimum = tuple(item for item in eligible if item.result.available_at == minimum)
        distinct: dict[str, MetricResult] = {}
        for item in at_minimum:
            payload = {
                "value": item.result.value,
                "unit": item.result.unit,
                "as_of": item.result.as_of,
                "available_at": item.result.available_at,
                "algorithm_version": item.result.algorithm_version,
                "quality": item.result.quality,
                "parameters": item.result.parameters,
            }
            distinct.setdefault(
                canonical_analytical_json(payload).decode("utf-8"),
                item.result,
            )
        if len(distinct) > 1:
            return None, ConditionEvidenceReason.AMBIGUOUS_METRIC, None
        selected = min(distinct.values(), key=lambda item: str(item.result_id))
        if not selected.value.is_finite():
            return None, ConditionEvidenceReason.INVALID_METRIC, "non_finite_value"
        return selected, None, None

    def _unevaluable_row(
        self,
        *,
        period_as_of: datetime,
        reason: ConditionEvidenceReason,
        detail: str | None,
    ) -> DescriptivePeriodOutcome:
        condition = self._rule.conditions[0]
        return DescriptivePeriodOutcome(
            period_as_of=period_as_of,
            group=DescriptiveOutcomeGroup.NOT_EVALUABLE,
            condition=DescriptiveConditionEvidence(
                state=AnalyticalConditionState.NOT_EVALUABLE,
                reason=reason,
                metric_key=condition.metric_key,
                operator=condition.operator,
                threshold=condition.threshold,
                retention_threshold=condition.retention_threshold,
                unit=condition.unit,
            ),
            outcome=DescriptiveOutcomeEvidence(
                status=DescriptiveOutcomeStatus.CONDITION_NOT_EVALUABLE,
                reason=(detail or reason.value)[:240],
                observed_intervals=0,
            ),
        )

    @staticmethod
    def _condition_evidence(
        condition,
        result,
        metric: MetricResult,
        screening_result_id: UUID,
    ) -> DescriptiveConditionEvidence:
        return DescriptiveConditionEvidence(
            state=result.state,
            metric_key=condition.metric_key,
            operator=condition.operator,
            threshold=condition.threshold,
            retention_threshold=condition.retention_threshold,
            unit=condition.unit,
            metric_result_id=metric.result_id,
            as_of=metric.as_of,
            available_at=metric.available_at,
            computed_at=metric.computed_at,
            algorithm_version=metric.algorithm_version,
            parameters=dict(metric.parameters),
            quality=metric.quality,
            observed_value=result.observed_value,
            screening_result_id=screening_result_id,
            explanation_es=result.explanation_es,
        )

    @staticmethod
    def _outcome_evidence(
        eval_at: datetime,
        known_at: datetime,
        closes: Sequence[_ClosePeriod],
    ) -> DescriptiveOutcomeEvidence:
        window_end = forward_window_end(eval_at)
        selectable = tuple(
            item
            for item in closes
            if item.start >= eval_at
            and item.end <= window_end
            and not item.excluded
            and (item.observation is not None or item.ambiguous)
        )
        selected = tuple(selectable[: forward_close_count()])
        close_models = _close_models(selected)
        observed_intervals = max(len(close_models) - 1, 0)
        if any(item.ambiguous for item in selected):
            ambiguous = next(item for item in selected if item.ambiguous)
            return DescriptiveOutcomeEvidence(
                status=DescriptiveOutcomeStatus.AMBIGUOUS_CLOSE,
                reason=f"ambiguous_close_at_{ambiguous.start.isoformat()}",
                observed_intervals=observed_intervals,
                closes=close_models,
                elapsed_seconds=_elapsed_seconds(close_models),
                bar_separation_seconds=_separations(close_models),
                evidence_available_at=_evidence_available_at(close_models),
            )
        invalid = next(
            (item for item in selected if item.invalid_reason is not None),
            None,
        )
        if invalid is not None:
            return DescriptiveOutcomeEvidence(
                status=DescriptiveOutcomeStatus.INVALID_CLOSE,
                reason=invalid.invalid_reason,
                observed_intervals=observed_intervals,
                closes=close_models,
                elapsed_seconds=_elapsed_seconds(close_models),
                bar_separation_seconds=_separations(close_models),
                evidence_available_at=_evidence_available_at(close_models),
            )
        if len(selected) < forward_close_count():
            status = (
                DescriptiveOutcomeStatus.PENDING_WINDOW
                if known_at < window_end
                else DescriptiveOutcomeStatus.INSUFFICIENT_OBSERVED_BARS
            )
            return DescriptiveOutcomeEvidence(
                status=status,
                reason=(
                    "observation_window_open"
                    if status is DescriptiveOutcomeStatus.PENDING_WINDOW
                    else "observation_window_closed"
                ),
                observed_intervals=observed_intervals,
                closes=close_models,
                elapsed_seconds=_elapsed_seconds(close_models),
                bar_separation_seconds=_separations(close_models),
                evidence_available_at=_evidence_available_at(close_models),
            )
        first = close_models[0].close
        last = close_models[-1].close
        with localcontext() as context:
            context.prec = 34
            context.rounding = ROUND_HALF_EVEN
            return_value = (last / first) - Decimal("1")
        return DescriptiveOutcomeEvidence(
            status=DescriptiveOutcomeStatus.AVAILABLE,
            return_5_intervals=return_value,
            observed_intervals=forward_close_count() - 1,
            closes=close_models,
            elapsed_seconds=_elapsed_seconds(close_models),
            bar_separation_seconds=_separations(close_models),
            evidence_available_at=_evidence_available_at(close_models),
        )


def _report_id(
    *,
    request: DescriptiveOutcomeRequest,
    rule: AnalyticalScreeningRule,
    rule_fingerprint: str,
    asset: MarketAssetDescriptor,
    workspace_id: UUID,
    rows: tuple[DescriptivePeriodOutcome, ...],
    groups,
) -> UUID:
    return descriptive_outcome_report_id(
        request=request,
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        rule_fingerprint=rule_fingerprint,
        asset_id=asset.asset_id,
        asset_class=asset.asset_class,
        source_id=asset.source_id,
        currency=asset.quote_currency,
        workspace_id=workspace_id,
        requested_calendar_days=request.requested_calendar_days,
        observed_periods=len(rows),
        rows=rows,
        groups=groups,
        latest_evidence_available_at=_latest_evidence_available_at(rows),
        limitations=_limitations(asset),
    )


def _index_visible_at_cut(entry: MetricIndexEntry, known_at: datetime) -> bool:
    if entry.legacy_known_at is None:
        return True
    normalized = (
        f"{entry.legacy_known_at[:-1]}+00:00"
        if entry.legacy_known_at.endswith(("Z", "z"))
        else entry.legacy_known_at
    )
    try:
        legacy = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise DescriptiveOutcomeIntegrityError(
            "the metric index contains a malformed legacy cut"
        ) from error
    if legacy.tzinfo is None or legacy.utcoffset() is None:
        raise DescriptiveOutcomeIntegrityError(
            "the metric index contains a timezone-naive legacy cut"
        )
    return legacy.astimezone(UTC) <= known_at


def _metric_invalid_reason(result: MetricResult, known_at: datetime) -> str | None:
    try:
        eligibility = metric_cut_eligibility(result, known_at)
    except MetricCutContractError as error:
        return str(error)[:240]
    if eligibility.eligible:
        return None
    if resolve_cut_identity_version(result) is CutIdentityVersion.V1:
        legacy = probe_legacy_known_at(result)
        if legacy.state is LegacyKnownAtState.ABSENT:
            return "legacy_cut_absent"
        if legacy.state is LegacyKnownAtState.NOT_STRING:
            return "legacy_cut_not_string"
        if legacy.state is LegacyKnownAtState.UNPARSEABLE:
            return "legacy_cut_unparseable"
        if legacy.state is LegacyKnownAtState.NAIVE:
            return "legacy_cut_naive"
    return "metric_not_eligible_at_cut"


def _close_bounds(
    row: NormalizedObservation,
) -> tuple[datetime | None, datetime | None]:
    start = row.period_start if row.period_start is not None else row.observed_at
    if start is None:
        return None, None
    end = row.period_end if row.period_end is not None else start + timedelta(days=1)
    return start, end


def _close_invalid_reason(row: NormalizedObservation) -> str | None:
    if row.quality in _QUALITY_INVALID:
        return "suspect_close_quality"
    if not row.value.is_finite():
        return "non_finite_close"
    if row.value <= 0:
        return "non_positive_close"
    return None


def _close_models(
    periods: Sequence[_ClosePeriod],
) -> tuple[DescriptiveCloseObservation, ...]:
    models: list[DescriptiveCloseObservation] = []
    for period in periods:
        observation = period.observation
        if observation is None:
            continue
        if observation.observed_at is None:
            continue
        models.append(
            DescriptiveCloseObservation(
                observation_id=observation.observation_id,
                period_start=period.start,
                period_end=period.end,
                observed_at=observation.observed_at,
                available_at=observation.available_at,
                close=observation.value,
                quality=observation.quality,
            )
        )
    return tuple(models)


def _elapsed_seconds(closes: Sequence[DescriptiveCloseObservation]) -> int | None:
    if len(closes) < 2:
        return None
    return int((closes[-1].period_start - closes[0].period_start).total_seconds())


def _separations(closes: Sequence[DescriptiveCloseObservation]) -> tuple[int, ...]:
    return tuple(
        int((closes[index].period_start - closes[index - 1].period_start).total_seconds())
        for index in range(1, len(closes))
    )


def _evidence_available_at(
    closes: Sequence[DescriptiveCloseObservation],
) -> datetime | None:
    values = tuple(value for close in closes for value in (close.period_end, close.available_at))
    return max(values) if values else None


def _latest_evidence_available_at(
    rows: Sequence[DescriptivePeriodOutcome],
) -> datetime | None:
    values = tuple(
        value
        for item in rows
        for value in (item.condition.available_at, item.outcome.evidence_available_at)
        if value is not None
    )
    return max(values) if values else None


def _limitations(descriptor: MarketAssetDescriptor) -> tuple[str, ...]:
    limitations = [
        "La revisión describe condiciones y sus resultados observados; no mide "
        "rentabilidad ejecutable, no descuenta dividendos, costes ni impuestos y no "
        "valida una predicción.",
        "Los cinco intervalos usan seis cierres observados; no equivalen a cinco días "
        "naturales ni a cinco sesiones garantizadas.",
        "La ventana de observación termina 30 días después de la disponibilidad de la "
        "condición; la falta de barras no demuestra que el mercado no ocurrió.",
        "La configuración actual de la regla se evalúa retrospectivamente; no se afirma "
        "que esa política existiera en cada fecha histórica.",
        "Los precios adjustment-all no son precios ejecutables.",
    ]
    if descriptor.provider == "alpaca":
        limitations.append(
            "Alpaca IEX representa una sola bolsa y no es cobertura SIP consolidada."
        )
        limitations.append(
            "Las barras diarias Alpaca IEX no exponen period_start/period_end nativos; la "
            "proyección usa observed_at como inicio y un intervalo declarado de 24 h como fin."
        )
    elif descriptor.provider == "coinbase":
        limitations.append(
            "Coinbase representa únicamente la actividad observada en Coinbase Exchange."
        )
    return tuple(limitations)


__all__ = [
    "DescriptiveConditionOutcomeService",
    "DescriptiveOutcomeBudgetExceededError",
    "DescriptiveOutcomeError",
    "DescriptiveOutcomeFingerprintChangedError",
    "DescriptiveOutcomeIntegrityError",
    "DescriptiveOutcomeUnsupportedError",
    "SUPPORTED_DESCRIPTIVE_OUTCOME_RULE_IDS",
]
