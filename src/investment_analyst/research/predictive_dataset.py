"""Build one deterministic point-in-time predictive research dataset."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

from investment_analyst.alerts.analytical_engine import AmbiguousAnalyticalMetricError
from investment_analyst.alerts.analytical_models import (
    AnalyticalConditionOperator,
    AnalyticalRuleState,
    AnalyticalScreeningCondition,
    AnalyticalScreeningDomain,
    AnalyticalScreeningRule,
)
from investment_analyst.alerts.analytical_monitor import AnalyticalMetricSnapshotSelector
from investment_analyst.analytics.analytical_access_models import (
    MetricIndexEntry,
    MetricPageCursor,
    MetricSeriesQuery,
)
from investment_analyst.analytics.market.history_service import (
    AmbiguousRevisionError,
    LatestRevisionState,
    consider_revision,
    require_unambiguous_revision,
)
from investment_analyst.analytics.metric_identity_cut import MetricCutContractError
from investment_analyst.catalog.provider_configuration import resolve_coinbase_configuration
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import MetricResult, NormalizedObservation
from investment_analyst.core.models.enums import AssetClass, DataFrequency, DataQuality
from investment_analyst.research.predictive_models import (
    CLOSE_FIELD_NAME,
    CLOSE_RETURN_ALGORITHM_VERSION,
    CLOSE_RETURN_COLUMN,
    DECIMAL34,
    FEATURE_COLUMNS,
    FEATURE_DAY_OFFSETS,
    FEATURE_RATIO_UNIT,
    LABEL_BAR_COUNT,
    LABEL_EXIT_OFFSET_DAYS,
    MACD_HISTOGRAM_COLUMN,
    MACD_HISTOGRAM_METRIC_KEY,
    MAX_PAGE_SIZE,
    PREREGISTERED_EXPERIMENT,
    PREREGISTERED_SPLIT_POLICY,
    RSI_COLUMN,
    RSI_METRIC_KEY,
    SUPPORTED_ASSET_CLASS,
    SUPPORTED_FREQUENCY,
    SUPPORTED_QUOTE_CURRENCY,
    EvidenceReason,
    FeatureState,
    LabelState,
    PredictiveArtifactSchema,
    PredictiveAssetIdentity,
    PredictiveDataset,
    PredictiveDatasetCounts,
    PredictiveDatasetManifest,
    PredictiveDatasetRequest,
    PredictiveDatasetSummary,
    PredictiveFeatureColumnSpec,
    PredictiveFeatureEvidence,
    PredictiveLabelBarEvidence,
    PredictiveLabelEvidence,
    PredictiveReadiness,
    PredictiveSample,
    PredictiveSplitSummary,
    PurgeReason,
    ReasonCode,
    SampleSplit,
    aggregate_feature_state,
    bar_start_instant,
    decision_instant,
    label_exit_bar_end,
    predictive_dataset_content_hash,
    predictive_dataset_id,
    predictive_feature_hash,
    predictive_row_id,
    predictive_sample_key,
    split_boundaries,
)
from investment_analyst.storage.local import LocalStorage

_REASON_MISSING = ReasonCode.CLOSE_MISSING
_REASON_NOT_YET = ReasonCode.CLOSE_NOT_YET_AVAILABLE
_REASON_AMBIGUOUS = ReasonCode.CLOSE_AMBIGUOUS
_REASON_INVALID = ReasonCode.CLOSE_INVALID
_REASON_PERIOD = ReasonCode.CLOSE_PERIOD_MISMATCH

_LABEL_REASON_MISSING = ReasonCode.LABEL_CLOSE_MISSING
_LABEL_REASON_AMBIGUOUS = ReasonCode.LABEL_CLOSE_AMBIGUOUS
_LABEL_REASON_INVALID = ReasonCode.LABEL_CLOSE_INVALID
_LABEL_REASON_PERIOD = ReasonCode.LABEL_PERIOD_MISMATCH


class PredictiveDatasetError(RuntimeError):
    """Base error for predictive research dataset construction."""


class PredictiveDatasetRequestError(PredictiveDatasetError, ValueError):
    """A request cannot be satisfied by the preregistered experiment."""


class UnsupportedPredictiveAssetError(PredictiveDatasetError):
    """The requested asset or source is outside the supported crypto profile."""


class PredictiveDatasetIntegrityError(PredictiveDatasetError):
    """Persisted evidence contradicts the requested scope or selector contract."""


@dataclass(frozen=True, slots=True)
class _CloseSelection:
    state: FeatureState
    observation: NormalizedObservation | None = None
    reasons: tuple[EvidenceReason, ...] = ()


@dataclass(frozen=True, slots=True)
class _MetricSelection:
    state: FeatureState
    metric: MetricResult | None = None
    reasons: tuple[EvidenceReason, ...] = ()


@dataclass(frozen=True, slots=True)
class _DraftSample:
    sample_key: UUID
    feature_hash: UUID
    row_id: UUID
    decision_date: date
    decision_at: datetime
    features: tuple[PredictiveFeatureEvidence, ...]
    label: PredictiveLabelEvidence
    feature_state: FeatureState
    label_state: LabelState


def _combine_states(*states: FeatureState) -> FeatureState:
    for candidate in (
        FeatureState.AMBIGUOUS,
        FeatureState.INCOMPATIBLE,
        FeatureState.NOT_YET_AVAILABLE,
        FeatureState.MISSING,
    ):
        if candidate in states:
            return candidate
    return FeatureState.AVAILABLE


def _reason(code: ReasonCode, detail: str) -> EvidenceReason:
    return EvidenceReason(code=code, detail=detail)


class PredictiveDatasetBuilder:
    """Read persisted evidence once and assemble one auditable research dataset."""

    def __init__(
        self,
        storage: LocalStorage,
        *,
        provider_resolver: ProviderAssetContextResolver,
        catalog: AssetCatalogService,
    ) -> None:
        storage.require_open()
        if not storage.read_only:
            raise PredictiveDatasetError("predictive builder requires read-only storage")
        workspace_id = storage.paths.workspace_id
        if not isinstance(workspace_id, UUID):
            raise PredictiveDatasetError("predictive builder requires an identified workspace")
        self._storage = storage
        self._provider_resolver = provider_resolver
        self._catalog = catalog
        self._workspace_uuid = workspace_id
        self._selector = AnalyticalMetricSnapshotSelector()
        self._rules = {
            spec.column_name: self._metric_rule(spec)
            for spec in PREREGISTERED_EXPERIMENT.feature_columns
            if spec.metric_key is not None
        }

    @staticmethod
    def _metric_rule(spec: PredictiveFeatureColumnSpec) -> AnalyticalScreeningRule:
        if spec.metric_key is None or spec.algorithm_version is None or spec.unit is None:
            raise PredictiveDatasetError("metric feature columns require an exact contract")
        condition = AnalyticalScreeningCondition(
            condition_id=spec.column_name,
            label_es=spec.column_name,
            domain=AnalyticalScreeningDomain.MARKET,
            metric_key=spec.metric_key,
            algorithm_version=spec.algorithm_version,
            unit=spec.unit,
            operator=AnalyticalConditionOperator.GREATER_THAN,
            threshold=Decimal("0"),
            parameter_filters=dict(spec.parameter_filters),
            accepted_qualities=(DataQuality.VALID,),
        )
        return AnalyticalScreeningRule(
            rule_id=f"predictive-{spec.column_name}-v1",
            rule_version=PREREGISTERED_EXPERIMENT.experiment_spec_id,
            name_es=spec.column_name,
            description_es="Predictive research feature extraction without thresholds.",
            state=AnalyticalRuleState.ACTIVE,
            domain=AnalyticalScreeningDomain.MARKET,
            asset_classes=(AssetClass.CRYPTO,),
            conditions=(condition,),
        )

    def build(self, request: PredictiveDatasetRequest) -> PredictiveDataset:
        """Return the deterministic dataset for one explicit request and cut."""
        asset = self._catalog.get(request.asset_id)
        if asset.asset_class is not SUPPORTED_ASSET_CLASS:
            raise UnsupportedPredictiveAssetError(
                f"asset {asset.asset_id} is not a supported crypto spot asset"
            )
        if asset.quote_currency != SUPPORTED_QUOTE_CURRENCY:
            raise UnsupportedPredictiveAssetError(
                f"asset {asset.asset_id} does not quote in {SUPPORTED_QUOTE_CURRENCY}"
            )
        if asset.crypto_profile is None:
            raise UnsupportedPredictiveAssetError(
                f"asset {asset.asset_id} lacks an explicit crypto profile"
            )
        configuration = resolve_coinbase_configuration(
            self._provider_resolver,
            asset_id=request.asset_id,
        )
        if (
            configuration.asset_class is not SUPPORTED_ASSET_CLASS
            or configuration.quote_unit != SUPPORTED_QUOTE_CURRENCY
        ):
            raise UnsupportedPredictiveAssetError(
                "resolved Coinbase configuration is outside crypto spot USD"
            )
        source_id = configuration.source_id

        decision_dates = request.decision_dates()
        feature_read_start = bar_start_instant(
            request.start_date + timedelta(days=FEATURE_DAY_OFFSETS[1])
        )
        feature_read_end = bar_start_instant(request.end_date)
        label_read_start = bar_start_instant(request.start_date)
        label_read_end = bar_start_instant(
            request.end_date + timedelta(days=LABEL_EXIT_OFFSET_DAYS + 1)
        )

        closes = self._read_closes(
            request,
            source_id=source_id,
            start=min(feature_read_start, label_read_start),
            end=max(feature_read_end, bar_start_instant(request.end_date + timedelta(days=6))),
        )
        metrics = self._read_metrics(
            request,
            source_id=source_id,
            as_of_from=bar_start_instant(request.start_date + timedelta(days=-1)),
            as_of_before=bar_start_instant(request.end_date),
        )

        drafts: list[_DraftSample] = []
        for decision_date in decision_dates:
            features = self._features(
                closes,
                metrics,
                decision_date=decision_date,
                dataset_known_at=request.dataset_known_at,
                source_id=source_id,
            )
            label = self._label(
                closes,
                decision_date=decision_date,
                dataset_known_at=request.dataset_known_at,
            )
            decision_at = decision_instant(decision_date)
            sample_key = predictive_sample_key(
                experiment_spec_id=PREREGISTERED_EXPERIMENT.experiment_spec_id,
                asset_id=request.asset_id,
                source_id=source_id,
                decision_at=decision_at,
            )
            feature_hash = predictive_feature_hash(
                sample_key=sample_key,
                asset_id=request.asset_id,
                source_id=source_id,
                decision_at=decision_at,
                features=features,
            )
            row_id = predictive_row_id(
                sample_key=sample_key,
                feature_hash=feature_hash,
                features=features,
                label=label,
            )
            drafts.append(
                _DraftSample(
                    sample_key=sample_key,
                    feature_hash=feature_hash,
                    row_id=row_id,
                    decision_date=decision_date,
                    decision_at=decision_at,
                    features=features,
                    label=label,
                    feature_state=aggregate_feature_state(features),
                    label_state=label.state,
                )
            )

        samples = self._assign_splits(tuple(drafts))
        counts = self._counts(samples)
        splits = self._split_summaries(samples)
        readiness = PredictiveReadiness.from_counts(
            eligible_before_split=counts.eligible_before_split,
            train_eligible=splits[0].eligible_after_purge,
            validation_eligible=splits[1].eligible_after_purge,
            holdout_eligible=splits[2].eligible_after_purge,
            train_positive=splits[0].positive or 0,
            train_negative=splits[0].negative or 0,
        )
        asset_identity = PredictiveAssetIdentity(
            asset_id=asset.asset_id,
            source_id=source_id,
            asset_class=asset.asset_class,
            quote_currency=asset.quote_currency,
            crypto_profile=asset.crypto_profile.value,
            exchange=asset.exchange or "UNKNOWN",
        )
        dataset_id = predictive_dataset_id(
            experiment_spec_id=PREREGISTERED_EXPERIMENT.experiment_spec_id,
            workspace_uuid=self._workspace_uuid,
            asset_id=request.asset_id,
            source_id=source_id,
            request=request,
            split_policy=PREREGISTERED_SPLIT_POLICY,
            rows=samples,
        )
        content_hash = predictive_dataset_content_hash(
            dataset_id=dataset_id,
            rows=samples,
        )
        experiment_limitations = PREREGISTERED_EXPERIMENT.limitations
        limitations = experiment_limitations + (
            "La partición temporal es base del holdout; no sustituye purged walk-forward.",
            "Un dataset exportado no implica muestra suficiente ni modelo evaluado.",
        )
        manifest = PredictiveDatasetManifest(
            dataset_id=dataset_id,
            dataset_content_sha256=content_hash,
            experiment=PREREGISTERED_EXPERIMENT,
            request=request,
            asset=asset_identity,
            workspace_uuid=self._workspace_uuid,
            frequency=SUPPORTED_FREQUENCY,
            close_field_name=CLOSE_FIELD_NAME,
            feature_read_start=feature_read_start,
            feature_read_end=feature_read_end,
            label_read_start=label_read_start,
            label_read_end=label_read_end,
            artifact_schema=PredictiveArtifactSchema(),
            counts=counts,
            split_policy=PREREGISTERED_SPLIT_POLICY,
            splits=splits,
            readiness=readiness,
            rows=samples,
            limitations=limitations,
        )
        summary = PredictiveDatasetSummary(
            dataset_id=dataset_id,
            dataset_content_sha256=content_hash,
            experiment_spec_id=PREREGISTERED_EXPERIMENT.experiment_spec_id,
            target_version=PREREGISTERED_EXPERIMENT.target_version,
            feature_set_version=PREREGISTERED_EXPERIMENT.feature_set_version,
            asset=asset_identity,
            request=request,
            counts=counts,
            split_policy=PREREGISTERED_SPLIT_POLICY,
            splits=splits,
            readiness=readiness,
            limitations=limitations,
        )
        return PredictiveDataset(
            dataset_id=dataset_id,
            dataset_content_sha256=content_hash,
            manifest=manifest,
            summary=summary,
            samples=samples,
        )

    def _read_closes(
        self,
        request: PredictiveDatasetRequest,
        *,
        source_id: str,
        start: datetime,
        end: datetime,
    ) -> dict[datetime, tuple[NormalizedObservation, ...]]:
        observations = self._storage.observations.list(
            asset_id=request.asset_id,
            source_id=source_id,
            frequency=DataFrequency.DAY_1,
            field_name=CLOSE_FIELD_NAME,
            observed_from=start,
            observed_before=end,
            available_to=request.dataset_known_at,
        )
        grouped: dict[datetime, list[NormalizedObservation]] = {}
        for observation in observations:
            self._validate_close_scope(
                observation,
                request=request,
                source_id=source_id,
                start=start,
                end=end,
            )
            observed_at = observation.observed_at
            if observed_at is None:
                raise PredictiveDatasetIntegrityError("close observation lacks observed_at")
            grouped.setdefault(observed_at, []).append(observation)
        return {timestamp: tuple(rows) for timestamp, rows in grouped.items()}

    @staticmethod
    def _validate_close_scope(
        observation: NormalizedObservation,
        *,
        request: PredictiveDatasetRequest,
        source_id: str,
        start: datetime,
        end: datetime,
    ) -> None:
        if observation.asset_id != request.asset_id:
            raise PredictiveDatasetIntegrityError("close observation belongs to another asset")
        if observation.source.source_id != source_id:
            raise PredictiveDatasetIntegrityError("close observation belongs to another source")
        if observation.frequency is not DataFrequency.DAY_1:
            raise PredictiveDatasetIntegrityError("close observation is not daily")
        if observation.field_name != CLOSE_FIELD_NAME:
            raise PredictiveDatasetIntegrityError("close observation does not carry close")
        if observation.observed_at is None:
            raise PredictiveDatasetIntegrityError("close observation lacks observed_at")
        if not start <= observation.observed_at < end:
            raise PredictiveDatasetIntegrityError("close observation is outside the read window")
        if observation.available_at > request.dataset_known_at:
            raise PredictiveDatasetIntegrityError(
                "close observation is not available at dataset_known_at"
            )

    def _read_metrics(
        self,
        request: PredictiveDatasetRequest,
        *,
        source_id: str,
        as_of_from: datetime,
        as_of_before: datetime,
    ) -> dict[datetime, tuple[MetricResult, ...]]:
        query = MetricSeriesQuery(
            asset_id=request.asset_id,
            domain="market",
            known_at=request.dataset_known_at,
            metric_keys=(RSI_METRIC_KEY, MACD_HISTOGRAM_METRIC_KEY),
            source_id=source_id,
            as_of_from=as_of_from,
            as_of_before=as_of_before,
            limit=MAX_PAGE_SIZE,
        )
        grouped: dict[datetime, list[MetricResult]] = {}
        seen: set[UUID] = set()
        cursor: MetricPageCursor | None = None
        while True:
            page_query = query if cursor is None else query.model_copy(update={"after": cursor})
            entries = self._storage.metric_results.list_metric_index_page(page_query)
            if len(entries) > MAX_PAGE_SIZE:
                raise PredictiveDatasetIntegrityError("metric index exceeded the bounded page")
            previous_key: tuple[datetime, str] | None = None
            visible: list[MetricIndexEntry] = []
            for entry in entries:
                self._validate_metric_index_entry(entry, query)
                key = (entry.available_at, str(entry.result_id))
                if previous_key is not None and key <= previous_key:
                    raise PredictiveDatasetIntegrityError("metric index is not in stable order")
                previous_key = key
                if entry.result_id in seen:
                    raise PredictiveDatasetIntegrityError("metric index repeated a result")
                seen.add(entry.result_id)
                if self._legacy_visible(entry, request.dataset_known_at):
                    visible.append(entry)
            identifiers = tuple(entry.result_id for entry in visible)
            results = self._storage.metric_results.get_many(identifiers) if identifiers else {}
            if set(results) != set(identifiers):
                raise PredictiveDatasetIntegrityError("metric index references a missing result")
            for entry in visible:
                result = results[entry.result_id]
                self._validate_hydrated_metric(entry, result, query)
                grouped.setdefault(result.as_of, []).append(result)
            if len(entries) < MAX_PAGE_SIZE:
                break
            next_cursor = MetricPageCursor(
                available_at=entries[-1].available_at,
                result_id=entries[-1].result_id,
            )
            if cursor == next_cursor:
                raise PredictiveDatasetIntegrityError("metric index did not advance its cursor")
            cursor = next_cursor
        return {period: tuple(items) for period, items in grouped.items()}

    @staticmethod
    def _validate_metric_index_entry(
        entry: MetricIndexEntry,
        query: MetricSeriesQuery,
    ) -> None:
        if (
            entry.asset_id != query.asset_id
            or entry.metric_key not in query.metric_keys
            or entry.source_id != query.source_id
        ):
            raise PredictiveDatasetIntegrityError("metric index returned out-of-scope evidence")
        if entry.available_at > query.known_at:
            raise PredictiveDatasetIntegrityError("metric index returned future evidence")
        if query.as_of_from is not None and entry.as_of < query.as_of_from:
            raise PredictiveDatasetIntegrityError("metric index returned an early period")
        if query.as_of_before is not None and entry.as_of >= query.as_of_before:
            raise PredictiveDatasetIntegrityError("metric index returned a late period")
        if query.after is not None and (
            entry.available_at,
            str(entry.result_id),
        ) <= (
            query.after.available_at,
            str(query.after.result_id),
        ):
            raise PredictiveDatasetIntegrityError("metric index page did not advance")

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
            or result.parameters.get("frequency") != entry.frequency
            or result.parameters.get("known_at") != entry.legacy_known_at
        ):
            raise PredictiveDatasetIntegrityError(
                "metric index projection changed during hydration"
            )
        if result.available_at > query.known_at:
            raise PredictiveDatasetIntegrityError(
                "metric result is not available at dataset_known_at"
            )

    @staticmethod
    def _legacy_visible(entry: MetricIndexEntry, known_at: datetime) -> bool:
        if entry.legacy_known_at is None:
            return True
        text = entry.legacy_known_at
        normalized = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
        try:
            legacy = datetime.fromisoformat(normalized)
        except ValueError as error:
            raise PredictiveDatasetIntegrityError(
                "metric index contains a malformed legacy cut"
            ) from error
        if legacy.tzinfo is None or legacy.utcoffset() is None:
            raise PredictiveDatasetIntegrityError(
                "metric index contains a timezone-naive legacy cut"
            )
        return legacy <= known_at

    def _features(
        self,
        closes: dict[datetime, tuple[NormalizedObservation, ...]],
        metrics: dict[datetime, tuple[MetricResult, ...]],
        *,
        decision_date: date,
        dataset_known_at: datetime,
        source_id: str,
    ) -> tuple[PredictiveFeatureEvidence, ...]:
        decision_at = decision_instant(decision_date)
        previous_day = decision_date + timedelta(days=FEATURE_DAY_OFFSETS[0])
        prior_day = decision_date + timedelta(days=FEATURE_DAY_OFFSETS[1])
        previous = self._select_close(
            closes.get(bar_start_instant(previous_day)),
            cut=decision_at,
            expected_start=bar_start_instant(previous_day),
        )
        prior = self._select_close(
            closes.get(bar_start_instant(prior_day)),
            cut=decision_at,
            expected_start=bar_start_instant(prior_day),
        )
        return (
            self._close_return_evidence(previous, prior),
            self._metric_evidence(
                self._rule_for(RSI_COLUMN),
                metrics.get(bar_start_instant(previous_day), ()),
                decision_at=decision_at,
                source_id=source_id,
                denominator=None,
            ),
            self._metric_evidence(
                self._rule_for(MACD_HISTOGRAM_COLUMN),
                metrics.get(bar_start_instant(previous_day), ()),
                decision_at=decision_at,
                source_id=source_id,
                denominator=previous,
            ),
        )

    def _rule_for(self, column: str) -> AnalyticalScreeningRule:
        rule = self._rules.get(column)
        if rule is None:
            raise PredictiveDatasetError(f"missing selector rule for {column}")
        return rule

    def _select_close(
        self,
        rows: tuple[NormalizedObservation, ...] | None,
        *,
        cut: datetime,
        expected_start: datetime,
        label: bool = False,
    ) -> _CloseSelection:
        missing = _LABEL_REASON_MISSING if label else _REASON_MISSING
        not_yet = _LABEL_REASON_MISSING if label else _REASON_NOT_YET
        ambiguous = _LABEL_REASON_AMBIGUOUS if label else _REASON_AMBIGUOUS
        invalid = _LABEL_REASON_INVALID if label else _REASON_INVALID
        period = _LABEL_REASON_PERIOD if label else _REASON_PERIOD
        if not rows:
            return _CloseSelection(
                FeatureState.MISSING,
                reasons=(_reason(missing, f"no close evidence for {expected_start.date()}"),),
            )
        eligible = tuple(row for row in rows if row.available_at <= cut)
        if not eligible:
            return _CloseSelection(
                FeatureState.NOT_YET_AVAILABLE,
                reasons=(
                    _reason(
                        not_yet,
                        f"close evidence for {expected_start.date()} is not available at cut",
                    ),
                ),
            )
        state: LatestRevisionState[NormalizedObservation] = LatestRevisionState()
        for row in eligible:
            state = consider_revision(state, row, available_at=row.available_at)
        try:
            selected = require_unambiguous_revision(state, timestamp=expected_start)
        except AmbiguousRevisionError as error:
            return _CloseSelection(
                FeatureState.AMBIGUOUS,
                reasons=(_reason(ambiguous, str(error)),),
            )
        if (
            selected.period_start != expected_start
            or selected.period_end != expected_start + timedelta(days=1)
        ):
            return _CloseSelection(
                FeatureState.INCOMPATIBLE,
                reasons=(
                    _reason(
                        period,
                        f"close evidence for {expected_start.date()} has an incompatible period",
                    ),
                ),
            )
        if selected.quality is not DataQuality.VALID:
            return _CloseSelection(
                FeatureState.INCOMPATIBLE,
                reasons=(
                    _reason(
                        invalid,
                        f"close evidence for {expected_start.date()} is {selected.quality.value}",
                    ),
                ),
            )
        if selected.unit != SUPPORTED_QUOTE_CURRENCY:
            return _CloseSelection(
                FeatureState.INCOMPATIBLE,
                reasons=(
                    _reason(
                        invalid,
                        f"close evidence for {expected_start.date()} quotes in {selected.unit}",
                    ),
                ),
            )
        if not selected.value.is_finite() or selected.value <= 0:
            return _CloseSelection(
                FeatureState.INCOMPATIBLE,
                reasons=(
                    _reason(
                        invalid,
                        f"close evidence for {expected_start.date()} is not a positive decimal",
                    ),
                ),
            )
        return _CloseSelection(FeatureState.AVAILABLE, observation=selected)

    @staticmethod
    def _close_return_evidence(
        previous: _CloseSelection,
        prior: _CloseSelection,
    ) -> PredictiveFeatureEvidence:
        state = _combine_states(previous.state, prior.state)
        reasons = previous.reasons + prior.reasons
        if state is not FeatureState.AVAILABLE:
            return PredictiveFeatureEvidence(
                column_name=CLOSE_RETURN_COLUMN,
                state=state,
                algorithm_version=CLOSE_RETURN_ALGORITHM_VERSION,
                unit=FEATURE_RATIO_UNIT,
                reasons=reasons,
            )
        if previous.observation is None or prior.observation is None:
            raise PredictiveDatasetIntegrityError(
                "available close return lacks selected observations"
            )
        with localcontext(DECIMAL34):
            value = previous.observation.value / prior.observation.value - 1
        return PredictiveFeatureEvidence(
            column_name=CLOSE_RETURN_COLUMN,
            state=FeatureState.AVAILABLE,
            value=value,
            as_of=previous.observation.observed_at,
            available_at=max(previous.observation.available_at, prior.observation.available_at),
            algorithm_version=CLOSE_RETURN_ALGORITHM_VERSION,
            unit=FEATURE_RATIO_UNIT,
            quality=DataQuality.VALID,
        )

    def _metric_evidence(
        self,
        rule: AnalyticalScreeningRule,
        period_metrics: tuple[MetricResult, ...],
        *,
        decision_at: datetime,
        source_id: str,
        denominator: _CloseSelection | None,
    ) -> PredictiveFeatureEvidence:
        spec = next(
            item
            for item in PREREGISTERED_EXPERIMENT.feature_columns
            if item.column_name == rule.conditions[0].condition_id
        )
        selection = self._select_metric(
            rule,
            spec,
            period_metrics,
            decision_at=decision_at,
            source_id=source_id,
        )
        if denominator is not None:
            state = _combine_states(selection.state, denominator.state)
            reasons = selection.reasons + denominator.reasons
        else:
            state = selection.state
            reasons = selection.reasons
        if state is not FeatureState.AVAILABLE:
            return PredictiveFeatureEvidence(
                column_name=spec.column_name,
                state=state,
                algorithm_version=spec.algorithm_version,
                unit=FEATURE_RATIO_UNIT,
                reasons=reasons,
            )
        metric = selection.metric
        if metric is None:
            raise PredictiveDatasetIntegrityError("available metric evidence lacks a result")
        if spec.transform == "rsi_fraction":
            with localcontext(DECIMAL34):
                value = metric.value / Decimal(100)
        else:
            if denominator is None or denominator.observation is None:
                raise PredictiveDatasetIntegrityError(
                    "histogram ratio lacks an available close denominator"
                )
            with localcontext(DECIMAL34):
                value = metric.value / denominator.observation.value
        return PredictiveFeatureEvidence(
            column_name=spec.column_name,
            state=FeatureState.AVAILABLE,
            value=value,
            metric_result_id=metric.result_id,
            as_of=metric.as_of,
            available_at=metric.available_at,
            computed_at=metric.computed_at,
            algorithm_version=spec.algorithm_version,
            unit=FEATURE_RATIO_UNIT,
            quality=metric.quality,
        )

    def _select_metric(
        self,
        rule: AnalyticalScreeningRule,
        spec: PredictiveFeatureColumnSpec,
        period_metrics: tuple[MetricResult, ...],
        *,
        decision_at: datetime,
        source_id: str,
    ) -> _MetricSelection:
        try:
            selected = self._selector.select(
                rule=rule,
                metrics=period_metrics,
                source_id=source_id,
                known_at=decision_at,
            )
        except (AmbiguousAnalyticalMetricError, MetricCutContractError) as error:
            return _MetricSelection(
                FeatureState.AMBIGUOUS,
                reasons=(_reason(ReasonCode.METRIC_AMBIGUOUS, str(error)),),
            )
        if len(selected) > 1:
            raise PredictiveDatasetIntegrityError("metric selector returned multiple results")
        if selected:
            metric = selected[0]
            if metric.as_of != bar_start_instant(decision_at.date() + timedelta(days=-1)):
                raise PredictiveDatasetIntegrityError(
                    "metric selector returned evidence from another period"
                )
            return _MetricSelection(FeatureState.AVAILABLE, metric=metric)
        matching = tuple(
            metric
            for metric in period_metrics
            if self._matches_metric_contract(metric, spec, source_id)
        )
        if matching and all(metric.available_at > decision_at for metric in matching):
            return _MetricSelection(
                FeatureState.NOT_YET_AVAILABLE,
                reasons=(
                    _reason(
                        ReasonCode.METRIC_NOT_YET_AVAILABLE,
                        f"{spec.metric_key} for {decision_at.date()} is not available at decision",
                    ),
                ),
            )
        if matching:
            raise PredictiveDatasetIntegrityError("metric selector omitted an eligible metric")
        if any(metric.metric_key == spec.metric_key for metric in period_metrics):
            return _MetricSelection(
                FeatureState.INCOMPATIBLE,
                reasons=(
                    _reason(
                        ReasonCode.METRIC_INCOMPATIBLE,
                        f"{spec.metric_key} has no exact version, unit, parameters or quality",
                    ),
                ),
            )
        return _MetricSelection(
            FeatureState.MISSING,
            reasons=(
                _reason(
                    ReasonCode.METRIC_MISSING,
                    f"no {spec.metric_key} evidence for {decision_at.date()}",
                ),
            ),
        )

    @staticmethod
    def _matches_metric_contract(
        metric: MetricResult,
        spec: PredictiveFeatureColumnSpec,
        source_id: str,
    ) -> bool:
        return (
            metric.metric_key == spec.metric_key
            and metric.algorithm_version == spec.algorithm_version
            and metric.unit == spec.unit
            and metric.quality is DataQuality.VALID
            and metric.parameters.get("source_id") == source_id
            and all(
                metric.parameters.get(key) == value for key, value in spec.parameter_filters.items()
            )
        )

    def _label(
        self,
        closes: dict[datetime, tuple[NormalizedObservation, ...]],
        *,
        decision_date: date,
        dataset_known_at: datetime,
    ) -> PredictiveLabelEvidence:
        bars: list[PredictiveLabelBarEvidence] = []
        selected: list[NormalizedObservation | None] = []
        reasons: list[EvidenceReason] = []
        states: list[FeatureState] = []
        for offset in range(LABEL_BAR_COUNT):
            day = decision_date + timedelta(days=offset)
            start = bar_start_instant(day)
            selection = self._select_close(
                closes.get(start),
                cut=dataset_known_at,
                expected_start=start,
                label=True,
            )
            states.append(selection.state)
            reasons.extend(selection.reasons)
            observation = selection.observation
            selected.append(observation)
            bars.append(
                PredictiveLabelBarEvidence(
                    bar_start=start,
                    bar_end=start + timedelta(days=1),
                    state=selection.state,
                    observation_id=observation.observation_id if observation else None,
                    available_at=observation.available_at if observation else None,
                    value=observation.value if observation else None,
                    reasons=selection.reasons,
                )
            )
        exit_end = label_exit_bar_end(decision_date)
        if FeatureState.AMBIGUOUS in states:
            state = LabelState.AMBIGUOUS
        elif exit_end > dataset_known_at:
            state = LabelState.NOT_YET_AVAILABLE
            reasons.append(
                _reason(
                    ReasonCode.LABEL_NOT_MATURED,
                    f"exit bar ends at {exit_end.isoformat()} after dataset_known_at",
                )
            )
        elif FeatureState.INCOMPATIBLE in states:
            state = LabelState.INCOMPATIBLE
        elif FeatureState.MISSING in states or FeatureState.NOT_YET_AVAILABLE in states:
            state = LabelState.MISSING
        else:
            state = LabelState.AVAILABLE
        if state is not LabelState.AVAILABLE:
            return PredictiveLabelEvidence(
                state=state,
                decision_at=decision_instant(decision_date),
                entry_bar_start=bar_start_instant(decision_date),
                exit_bar_end=exit_end,
                bars=tuple(bars),
                reasons=tuple(reasons),
            )
        entry = selected[0]
        exit_observation = selected[LABEL_BAR_COUNT - 1]
        if entry is None or exit_observation is None:
            raise PredictiveDatasetIntegrityError("available label lacks entry or exit close")
        with localcontext(DECIMAL34):
            return_5d = exit_observation.value / entry.value - 1
        label = 1 if return_5d > 0 else 0
        available_at = max(
            [bar.available_at for bar in bars if bar.available_at is not None] + [exit_end]
        )
        return PredictiveLabelEvidence(
            state=LabelState.AVAILABLE,
            decision_at=decision_instant(decision_date),
            entry_bar_start=bar_start_instant(decision_date),
            exit_bar_end=exit_end,
            entry_close=entry.value,
            exit_close=exit_observation.value,
            return_5d=return_5d,
            label=label,
            label_available_at=available_at,
            bars=tuple(bars),
        )

    @staticmethod
    def _assign_splits(drafts: tuple[_DraftSample, ...]) -> tuple[PredictiveSample, ...]:
        decision_dates = tuple(item.decision_date for item in drafts)
        train_end, validation_end = split_boundaries(len(decision_dates))
        samples: list[PredictiveSample] = []
        for index, draft in enumerate(drafts):
            if index < train_end:
                split = SampleSplit.TRAIN
                boundary_date = (
                    decision_dates[train_end] if train_end < len(decision_dates) else None
                )
            elif index < validation_end:
                split = SampleSplit.VALIDATION
                boundary_date = (
                    decision_dates[validation_end] if validation_end < len(decision_dates) else None
                )
            else:
                split = SampleSplit.HOLDOUT
                boundary_date = None
            purge_reason = PredictiveDatasetBuilder._purge_reason(
                draft.decision_date,
                split=split,
                boundary_date=boundary_date,
            )
            feature_available = draft.feature_state is FeatureState.AVAILABLE
            label_available = draft.label_state is LabelState.AVAILABLE
            samples.append(
                PredictiveSample(
                    sample_key=draft.sample_key,
                    feature_hash=draft.feature_hash,
                    row_id=draft.row_id,
                    decision_at=draft.decision_at,
                    features=draft.features,
                    label=draft.label,
                    feature_state=draft.feature_state,
                    label_state=draft.label_state,
                    split=split,
                    purged=purge_reason is not None,
                    purge_reason=purge_reason,
                    eligible=feature_available and label_available and purge_reason is None,
                )
            )
        return tuple(samples)

    @staticmethod
    def _purge_reason(
        decision_date: date,
        *,
        split: SampleSplit,
        boundary_date: date | None,
    ) -> PurgeReason | None:
        if boundary_date is None or split is SampleSplit.HOLDOUT:
            return None
        boundary = bar_start_instant(boundary_date)
        exit_end = label_exit_bar_end(decision_date)
        if exit_end >= boundary:
            return PurgeReason.LABEL_INTERVAL_OVERLAP
        if exit_end >= boundary - timedelta(days=PREREGISTERED_SPLIT_POLICY.embargo_days):
            return PurgeReason.EMBARGO
        return None

    @staticmethod
    def _counts(samples: tuple[PredictiveSample, ...]) -> PredictiveDatasetCounts:
        def feature_count(state: FeatureState) -> int:
            return sum(1 for item in samples if item.feature_state is state)

        def label_count(state: LabelState) -> int:
            return sum(1 for item in samples if item.label_state is state)

        return PredictiveDatasetCounts(
            requested=len(samples),
            feature_available=feature_count(FeatureState.AVAILABLE),
            feature_missing=feature_count(FeatureState.MISSING),
            feature_not_yet_available=feature_count(FeatureState.NOT_YET_AVAILABLE),
            feature_incompatible=feature_count(FeatureState.INCOMPATIBLE),
            feature_ambiguous=feature_count(FeatureState.AMBIGUOUS),
            label_available=label_count(LabelState.AVAILABLE),
            label_missing=label_count(LabelState.MISSING),
            label_not_yet_available=label_count(LabelState.NOT_YET_AVAILABLE),
            label_incompatible=label_count(LabelState.INCOMPATIBLE),
            label_ambiguous=label_count(LabelState.AMBIGUOUS),
            eligible_before_split=sum(
                1
                for item in samples
                if item.feature_state is FeatureState.AVAILABLE
                and item.label_state is LabelState.AVAILABLE
            ),
            purged=sum(1 for item in samples if item.purged),
        )

    @staticmethod
    def _split_summaries(
        samples: tuple[PredictiveSample, ...],
    ) -> tuple[PredictiveSplitSummary, ...]:
        summaries: list[PredictiveSplitSummary] = []
        for split in (SampleSplit.TRAIN, SampleSplit.VALIDATION, SampleSplit.HOLDOUT):
            assigned = tuple(item for item in samples if item.split is split)
            purged = sum(1 for item in assigned if item.purged)
            remaining = len(assigned) - purged
            eligible = sum(1 for item in assigned if item.eligible)
            if split is SampleSplit.TRAIN:
                positive = sum(1 for item in assigned if item.eligible and item.label.label == 1)
                negative = sum(1 for item in assigned if item.eligible and item.label.label == 0)
            else:
                positive = None
                negative = None
            summaries.append(
                PredictiveSplitSummary(
                    split=split,
                    proposed=len(assigned),
                    purged=purged,
                    remaining=remaining,
                    eligible_after_purge=eligible,
                    positive=positive,
                    negative=negative,
                )
            )
        return tuple(summaries)


__all__ = [
    "FEATURE_COLUMNS",
    "PredictiveDatasetBuilder",
    "PredictiveDatasetError",
    "PredictiveDatasetIntegrityError",
    "PredictiveDatasetRequestError",
    "UnsupportedPredictiveAssetError",
]
