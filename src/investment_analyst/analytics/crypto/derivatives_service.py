"""Read-only point-in-time replay and multidimensional derivatives diagnostic."""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from investment_analyst.analytics.crypto.derivatives_engine import (
    DVOL_CHANGE_KEY,
    FUNDING_SUM_KEY,
    SPREAD_BPS_KEY,
    CryptoDerivativesMetricEngine,
    select_latest_revisions,
)
from investment_analyst.analytics.crypto.derivatives_identity import diagnostic_id
from investment_analyst.analytics.crypto.derivatives_models import (
    CryptoDerivativeMetricValue,
    CryptoDerivativeObservationValue,
    CryptoDerivativesCoverage,
    CryptoDerivativesDiagnostic,
    CryptoDerivativesDiagnosticStatus,
    CryptoDerivativesQueryResult,
    DvolDirection,
    FundingDirection,
    observation_time,
)
from investment_analyst.core.models import MetricResult, NormalizedObservation
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.errors import RecordNotFoundError, StorageError


class CryptoDerivativesService:
    """Reconstruct one local information set without providers or storage writes."""

    def __init__(
        self,
        storage: LocalStorage,
        engine: CryptoDerivativesMetricEngine,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._storage = storage
        self._engine = engine
        self._clock = clock

    def query(
        self,
        *,
        asset_id: str,
        funding_source_id: str,
        dvol_source_id: str,
        summary_source_id: str,
        diagnostic_source_ids: tuple[str, ...],
        start: datetime,
        end: datetime,
        known_at: datetime,
    ) -> CryptoDerivativesQueryResult:
        self._storage.require_open()
        start_utc = _utc(start)
        end_utc = _utc(end)
        known = _utc(known_at)
        if start_utc >= end_utc:
            raise ValueError("derivatives query start must be earlier than end")
        if diagnostic_source_ids != tuple(sorted(set(diagnostic_source_ids))):
            raise ValueError("diagnostic source IDs must be unique and sorted")
        observations = tuple(
            self._storage.observations.list(
                asset_id=asset_id,
                available_to=known,
            )
        )
        computation = self._engine.compute(
            observations,
            asset_id=asset_id,
            funding_source_id=funding_source_id,
            dvol_source_id=dvol_source_id,
            summary_source_id=summary_source_id,
            known_at=known,
            computed_at=max(_utc(self._clock()), known),
            as_of_from=start_utc,
            as_of_before=end_utc,
        )
        metrics = _latest_metric_slots(
            tuple(self._reuse_persisted_metric(item) for item in computation.results)
        )
        expected_sources = {
            "funding_interest_1h": funding_source_id,
            "dvol_close": dvol_source_id,
            "open_interest": summary_source_id,
            "current_funding": summary_source_id,
            "funding_8h": summary_source_id,
            "bid_price": summary_source_id,
            "ask_price": summary_source_id,
            "mid_price": summary_source_id,
        }
        eligible = tuple(
            item
            for item in observations
            if item.field_name in expected_sources
            and item.source.source_id == expected_sources[item.field_name]
            and item.available_at <= known
            and item.observed_at is not None
            and item.observed_at <= known
            and (item.period_end is None or item.period_end <= known)
        )
        selected = select_latest_revisions(eligible, known_at=known)
        in_range = tuple(item for item in selected if start_utc <= observation_time(item) < end_utc)
        latest_open_interest = _latest_observation(in_range, "open_interest")
        latest_current_funding = _latest_observation(in_range, "current_funding")
        latest_funding_8h = _latest_observation(in_range, "funding_8h")
        funding_sum = _latest_metric(metrics, FUNDING_SUM_KEY, window=168)
        dvol_change = _latest_metric(metrics, DVOL_CHANGE_KEY, window=7)
        spread = _latest_metric(metrics, SPREAD_BPS_KEY, window=1)

        observation_values = tuple(
            _observation_value(item, known)
            for item in (
                latest_open_interest,
                latest_current_funding,
                latest_funding_8h,
            )
            if item is not None
        )
        funding_value = _metric_value(funding_sum)
        dvol_value = _metric_value(dvol_change)
        spread_value = _metric_value(spread)
        required_present = (
            funding_value,
            dvol_value,
            *observation_values,
            spread_value,
        )
        present_count = sum(item is not None for item in required_present)
        status = (
            CryptoDerivativesDiagnosticStatus.COMPLETE
            if present_count == 6
            else CryptoDerivativesDiagnosticStatus.PARTIAL
            if present_count
            else CryptoDerivativesDiagnosticStatus.INSUFFICIENT_DATA
        )
        missing = set(computation.missing_requirements)
        for label, item in (
            ("diagnostic:funding_sum_168h", funding_value),
            ("diagnostic:dvol_change_7d", dvol_value),
            ("diagnostic:latest_open_interest", latest_open_interest),
            ("diagnostic:latest_current_funding", latest_current_funding),
            ("diagnostic:latest_funding_8h", latest_funding_8h),
            ("diagnostic:latest_spread_bps", spread_value),
        ):
            if item is None:
                missing.add(label)
        metric_ids = tuple(sorted({item.result_id for item in metrics}, key=str))
        diagnostic_observation_ids = {
            item.observation_id for item in in_range if item.field_name in expected_sources
        }
        for metric in metrics:
            diagnostic_observation_ids.update(metric.input_observation_ids)
        observation_ids = tuple(sorted(diagnostic_observation_ids, key=str))
        diagnostic = CryptoDerivativesDiagnostic(
            diagnostic_id=diagnostic_id(
                asset_id=asset_id,
                source_ids=diagnostic_source_ids,
                known_at=known,
                observation_ids=observation_ids,
                metric_result_ids=metric_ids,
                dimensional_states={
                    "dvol_direction": _dvol_direction(dvol_change).value,
                    "funding_direction": _funding_direction(funding_sum).value,
                    "status": status.value,
                },
                algorithm_version="crypto-derivatives-diagnostic-v1",
            ),
            asset_id=asset_id,
            source_ids=diagnostic_source_ids,
            known_at=known,
            status=status,
            funding_direction=_funding_direction(funding_sum),
            dvol_direction=_dvol_direction(dvol_change),
            funding_sum_168h=funding_value,
            dvol_change_7d=dvol_value,
            latest_open_interest=(
                _observation_value(latest_open_interest, known)
                if latest_open_interest is not None
                else None
            ),
            latest_current_funding=(
                _observation_value(latest_current_funding, known)
                if latest_current_funding is not None
                else None
            ),
            latest_funding_8h=(
                _observation_value(latest_funding_8h, known)
                if latest_funding_8h is not None
                else None
            ),
            latest_spread_bps=spread_value,
            observation_ids=observation_ids,
            metric_result_ids=metric_ids,
            missing_requirements=tuple(sorted(missing)),
            limitations=(
                "Historical backfill is visible only from its first local retrieval.",
                "DVOL, funding history, and prospective summary snapshots have distinct cadences.",
                "Deribit retention, fields, limits, and access may change without an SLA.",
                "This descriptive diagnostic is not a signal, ranking, recommendation, or advice.",
            ),
        )
        raw_ids = self._verify_traceability(observation_ids, metrics)
        coverage = CryptoDerivativesCoverage(
            requested_start=start_utc,
            requested_end=end_utc,
            known_at=known,
            funding_observation_count=sum(
                item.source.source_id == funding_source_id for item in in_range
            ),
            dvol_observation_count=sum(
                item.source.source_id == dvol_source_id for item in in_range
            ),
            summary_snapshot_count=len(
                {
                    item.raw_record_id
                    for item in in_range
                    if item.source.source_id == summary_source_id
                }
            ),
            metric_count=len(metrics),
        )
        return CryptoDerivativesQueryResult(
            asset_id=asset_id,
            source_ids=diagnostic_source_ids,
            known_at=known,
            metrics=metrics,
            diagnostic=diagnostic,
            coverage=coverage,
            raw_record_ids=raw_ids,
            traceability_verified=True,
        )

    def _reuse_persisted_metric(self, candidate: MetricResult) -> MetricResult:
        try:
            existing = self._storage.metric_results.get(candidate.result_id)
        except RecordNotFoundError:
            return candidate
        if existing.model_dump(mode="python", exclude={"computed_at"}) != candidate.model_dump(
            mode="python",
            exclude={"computed_at"},
        ):
            raise StorageError("derivatives query metric identity is semantically inconsistent")
        return existing

    def _verify_traceability(
        self,
        observation_ids: tuple[UUID, ...],
        metrics: tuple[MetricResult, ...],
    ) -> tuple[UUID, ...]:
        observations = {
            identifier: self._storage.observations.get(identifier) for identifier in observation_ids
        }
        raw_ids = tuple(sorted({item.raw_record_id for item in observations.values()}, key=str))
        raw_records = self._storage.raw_records.get_many(raw_ids)
        for observation in observations.values():
            raw = raw_records[observation.raw_record_id]
            if observation.source != raw.source or observation.asset_id != raw.asset_id:
                raise StorageError("derivatives query observation traceability failed")
        for metric in metrics:
            if any(identifier not in observations for identifier in metric.input_observation_ids):
                raise StorageError("derivatives query metric input is not traceable")
        return raw_ids


def _latest_metric_slots(results: tuple[MetricResult, ...]) -> tuple[MetricResult, ...]:
    slots: dict[tuple[str, int], MetricResult] = {}
    for result in results:
        window = result.parameters.get("window")
        if isinstance(window, bool) or not isinstance(window, int):
            raise StorageError("derivatives metric window parameter is invalid")
        slot = (result.metric_key, window)
        existing = slots.get(slot)
        if existing is None or (result.as_of, str(result.result_id)) > (
            existing.as_of,
            str(existing.result_id),
        ):
            slots[slot] = result
    return tuple(
        sorted(slots.values(), key=lambda item: (item.metric_key, item.parameters["window"]))
    )


def _latest_metric(
    metrics: tuple[MetricResult, ...],
    metric_key: str,
    *,
    window: int,
) -> MetricResult | None:
    return next(
        (
            item
            for item in metrics
            if item.metric_key == metric_key and item.parameters.get("window") == window
        ),
        None,
    )


def _latest_observation(
    observations: tuple[NormalizedObservation, ...],
    field_name: str,
) -> NormalizedObservation | None:
    matches = tuple(item for item in observations if item.field_name == field_name)
    return max(
        matches,
        key=lambda item: (observation_time(item), item.available_at, str(item.observation_id)),
        default=None,
    )


def _observation_value(
    observation: NormalizedObservation,
    known_at: datetime,
) -> CryptoDerivativeObservationValue:
    delta = known_at - observation_time(observation)
    age_seconds = delta.days * 86_400 + delta.seconds
    return CryptoDerivativeObservationValue(
        field_name=observation.field_name,
        value=observation.value,
        unit=observation.unit,
        observed_at=observation_time(observation),
        available_at=observation.available_at,
        observation_id=observation.observation_id,
        raw_record_id=observation.raw_record_id,
        source_id=observation.source.source_id,
        age_seconds=age_seconds,
    )


def _metric_value(metric: MetricResult | None) -> CryptoDerivativeMetricValue | None:
    if metric is None:
        return None
    return CryptoDerivativeMetricValue(
        metric_key=metric.metric_key,
        value=metric.value,
        unit=metric.unit,
        as_of=metric.as_of,
        available_at=metric.available_at,
        result_id=metric.result_id,
        input_observation_ids=tuple(metric.input_observation_ids),
    )


def _funding_direction(metric: MetricResult | None) -> FundingDirection:
    if metric is None:
        return FundingDirection.UNAVAILABLE
    if metric.value > Decimal(0):
        return FundingDirection.POSITIVE
    if metric.value < Decimal(0):
        return FundingDirection.NEGATIVE
    return FundingDirection.ZERO


def _dvol_direction(metric: MetricResult | None) -> DvolDirection:
    if metric is None:
        return DvolDirection.UNAVAILABLE
    if metric.value > Decimal(0):
        return DvolDirection.RISING
    if metric.value < Decimal(0):
        return DvolDirection.FALLING
    return DvolDirection.UNCHANGED


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("derivatives query datetimes must include timezone information")
    return value.astimezone(UTC)


__all__ = ["CryptoDerivativesService"]
