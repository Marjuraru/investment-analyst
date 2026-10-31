"""Read-only reconstruction of daily MACD states and authorized crossings."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from decimal import Decimal
from typing import Protocol
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
from investment_analyst.analytics.market.incremental_recursive import (
    MACD_ALGORITHM_VERSION,
    RSI_ALGORITHM_VERSION,
)
from investment_analyst.analytics.market.pattern_models import (
    DailyMarketMomentumSegment,
    DailyMarketPatternEvent,
    DailyMarketPatternMetricEvidence,
    DailyMarketPatternPoint,
    DailyMarketPatternsCoverage,
    DailyMarketPatternsRequest,
    DailyMarketPatternsResult,
    DailyMarketPatternType,
    MacdMomentumState,
    daily_market_momentum_segment_id,
    daily_market_pattern_event_id,
    daily_market_patterns_result_id,
)
from investment_analyst.analytics.market.statistics_definitions import (
    MACD_HISTOGRAM_KEY,
    RSI_KEY,
)
from investment_analyst.analytics.metric_identity_cut import MetricCutContractError
from investment_analyst.core.interfaces.repositories import ObservationRepository
from investment_analyst.core.models import (
    AssetClass,
    DataFrequency,
    DataQuality,
    MetricResult,
    NormalizedObservation,
)

_PATTERN_METRIC_KEYS = (MACD_HISTOGRAM_KEY, RSI_KEY)
_MAX_METRIC_PAGE = 256
_ZERO = Decimal("0")
_RSI_THRESHOLD = Decimal("30")
_SUPPORTED_CURRENCY = "USD"

_PATTERN_EVIDENCE_RULE = AnalyticalScreeningRule(
    rule_id="market.technical.daily-market-patterns-evidence",
    rule_version="1.0",
    name_es="Evidencia diaria para estados y patrones descriptivos",
    description_es=(
        "Selecciona la evidencia RSI(14) e histograma MACD(12,26,9) incremental v2 "
        "de cada período observado; no evalúa umbrales ni produce señales."
    ),
    state=AnalyticalRuleState.SILENT,
    domain=AnalyticalScreeningDomain.MARKET,
    asset_classes=(
        AssetClass.CRYPTO,
        AssetClass.EQUITY,
        AssetClass.ETF,
    ),
    conditions=(
        AnalyticalScreeningCondition(
            condition_id="macd_histogram",
            label_es="Histograma MACD",
            domain=AnalyticalScreeningDomain.MARKET,
            metric_key=MACD_HISTOGRAM_KEY,
            algorithm_version=MACD_ALGORITHM_VERSION,
            unit="USD",
            operator=AnalyticalConditionOperator.GREATER_THAN,
            threshold=_ZERO,
            parameter_filters={
                "fast_window": 12,
                "slow_window": 26,
                "signal_window": 9,
            },
            accepted_qualities=(DataQuality.PARTIAL, DataQuality.VALID),
        ),
        AnalyticalScreeningCondition(
            condition_id="rsi",
            label_es="RSI Wilder",
            domain=AnalyticalScreeningDomain.MARKET,
            metric_key=RSI_KEY,
            algorithm_version=RSI_ALGORITHM_VERSION,
            unit="index",
            operator=AnalyticalConditionOperator.LESS_THAN_OR_EQUAL,
            threshold=_RSI_THRESHOLD,
            parameter_filters={"window": 14},
            accepted_qualities=(DataQuality.PARTIAL, DataQuality.VALID),
        ),
    ),
)


class PatternMetricPort(Protocol):
    """Bounded metric index and batch hydration used before any calculation."""

    def list_metric_index_page(self, query: MetricSeriesQuery) -> tuple[MetricIndexEntry, ...]: ...

    def get_metric_results(self, result_ids: Collection[UUID]) -> dict[UUID, MetricResult]: ...


class DailyMarketPatternsError(ValueError):
    """Base error for an invalid or ambiguous daily pattern query."""


class UnsupportedDailyPatternsCurrencyError(DailyMarketPatternsError):
    """Raised when the resolved asset quotes in a currency this contract lacks."""


class DuplicateDailyCloseDateError(DailyMarketPatternsError):
    """Raised when one UTC date carries more than one distinct close period."""


class AmbiguousDailyCloseRevisionError(DailyMarketPatternsError):
    """Raised when one close period has non-equivalent revisions at the same cut."""


class AmbiguousDailyPatternEvidenceError(DailyMarketPatternsError):
    """Raised when one period carries ambiguous eligible metric revisions."""


class DailyMarketPatternsService:
    """Describe states and crossings from persisted daily evidence without writes."""

    def __init__(
        self,
        observations: ObservationRepository,
        metrics: PatternMetricPort,
        selector: AnalyticalMetricSnapshotSelector | None = None,
    ) -> None:
        self._observations = observations
        self._metrics = metrics
        self._selector = selector or AnalyticalMetricSnapshotSelector()

    def query(
        self,
        request: DailyMarketPatternsRequest,
        *,
        asset_id: str,
        source_id: str,
        currency: str,
    ) -> DailyMarketPatternsResult:
        """Return one deterministic descriptive reconstruction at the requested cut."""
        if request.asset_id != asset_id:
            raise DailyMarketPatternsError("request asset_id differs from the resolved asset")
        if currency != _SUPPORTED_CURRENCY:
            raise UnsupportedDailyPatternsCurrencyError(
                "daily market patterns currently require a USD quote currency"
            )
        close_rows, closes = self._read_closes(
            request,
            asset_id=asset_id,
            source_id=source_id,
        )
        metrics_by_period = self._read_metrics(
            request,
            asset_id=asset_id,
            source_id=source_id,
        )
        points = tuple(
            self._point(
                request,
                close,
                metrics_by_period.get(close.observed_at, ()),
                source_id=source_id,
            )
            for close in closes
        )
        segments = self._segments(points, asset_id=asset_id, source_id=source_id)
        events = self._events(points, asset_id=asset_id, source_id=source_id)
        close_periods = {close.observed_at for close in closes}
        coverage = DailyMarketPatternsCoverage(
            close_observations_read=close_rows,
            close_periods=len(closes),
            discarded_close_revisions=close_rows - len(closes),
            point_count=len(points),
            rsi_available_points=sum(point.rsi.status == "available" for point in points),
            rsi_missing_points=sum(point.rsi.status == "missing" for point in points),
            macd_available_points=sum(
                point.macd_histogram.status == "available" for point in points
            ),
            macd_missing_points=sum(point.macd_histogram.status == "missing" for point in points),
            metric_periods_without_close=len(set(metrics_by_period) - close_periods),
            segment_count=len(segments),
            event_count=len(events),
            earliest_as_of=points[0].as_of if points else None,
            latest_as_of=points[-1].as_of if points else None,
            maximum_evidence_available_at=(
                max(point.evidence_available_at for point in points) if points else None
            ),
        )
        limitations = (
            "Reconstrucción descriptiva al corte solicitado: no afirma que una transición "
            "se notificara en su fecha histórica.",
            "Sólo usa cierres diarios observados, RSI(14) y el histograma MACD(12,26,9) "
            "incremental v2 ya persistidos; no reconstruye OHLCV ni infiere un calendario.",
            "Una métrica ausente o incompatible rompe la continuidad; no se rellena con un "
            "período anterior.",
            "Los tramos que tocan el borde del rango están censurados y su duración total "
            "no se conoce.",
            "Las acciones y ETF usan Alpaca IEX de una sola bolsa; no es cobertura SIP "
            "consolidada.",
            "Esta versión admite únicamente cotizaciones en USD; no aplica conversión FX.",
            "No calcula retorno posterior ni evalúa utilidad predictiva; no es señal, "
            "predicción, recomendación ni instrucción de broker.",
        )
        return DailyMarketPatternsResult(
            result_id=daily_market_patterns_result_id(
                request=request,
                asset_id=asset_id,
                source_id=source_id,
                currency=currency,
                points=points,
                segments=segments,
                events=events,
            ),
            request=request,
            asset_id=asset_id,
            source_id=source_id,
            currency=currency,
            points=points,
            segments=segments,
            events=events,
            coverage=coverage,
            limitations=limitations,
        )

    def _read_closes(
        self,
        request: DailyMarketPatternsRequest,
        *,
        asset_id: str,
        source_id: str,
    ) -> tuple[int, tuple[NormalizedObservation, ...]]:
        rows = self._observations.list(
            asset_id=asset_id,
            source_id=source_id,
            frequency=DataFrequency.DAY_1,
            observed_from=request.start_utc,
            observed_before=request.end_before_utc,
            available_to=request.known_at,
            field_name="close",
        )
        states: dict[datetime, LatestRevisionState[NormalizedObservation]] = {}
        for row in rows:
            self._validate_close(row, request, asset_id=asset_id, source_id=source_id)
            states[row.observed_at] = consider_revision(
                states.get(row.observed_at, LatestRevisionState()),
                row,
                available_at=row.available_at,
            )
        selected: list[NormalizedObservation] = []
        for timestamp in sorted(states):
            try:
                selected.append(
                    require_unambiguous_revision(states[timestamp], timestamp=timestamp)
                )
            except AmbiguousRevisionError as error:
                raise AmbiguousDailyCloseRevisionError(str(error)) from error
        by_date: dict[object, list[NormalizedObservation]] = {}
        for row in selected:
            by_date.setdefault(row.observed_at.date(), []).append(row)
        if any(len({row.observed_at for row in rows}) > 1 for rows in by_date.values()):
            raise DuplicateDailyCloseDateError(
                "daily patterns reject more than one close period on the same UTC date"
            )
        return len(rows), tuple(selected)

    @staticmethod
    def _validate_close(
        row: NormalizedObservation,
        request: DailyMarketPatternsRequest,
        *,
        asset_id: str,
        source_id: str,
    ) -> None:
        if (
            row.asset_id != asset_id
            or row.source.source_id != source_id
            or row.frequency is not DataFrequency.DAY_1
            or row.field_name != "close"
        ):
            raise DailyMarketPatternsError("close evidence is outside the requested scope")
        if row.observed_at is None:
            raise DailyMarketPatternsError("close evidence lacks an observed timestamp")
        if not request.start_utc <= row.observed_at < request.end_before_utc:
            raise DailyMarketPatternsError("close evidence is outside the requested range")
        if row.available_at > request.known_at:
            raise DailyMarketPatternsError("close evidence is not available at known_at")
        if row.unit != _SUPPORTED_CURRENCY:
            raise UnsupportedDailyPatternsCurrencyError(
                "close evidence does not quote in the authorized currency"
            )
        if not row.value.is_finite() or row.value <= 0:
            raise DailyMarketPatternsError("close evidence must be a positive finite decimal")

    def _read_metrics(
        self,
        request: DailyMarketPatternsRequest,
        *,
        asset_id: str,
        source_id: str,
    ) -> dict[datetime, tuple[MetricResult, ...]]:
        query = MetricSeriesQuery(
            asset_id=asset_id,
            domain="market",
            known_at=request.known_at,
            metric_keys=_PATTERN_METRIC_KEYS,
            source_id=source_id,
            as_of_from=request.start_utc,
            as_of_before=request.end_before_utc,
            limit=_MAX_METRIC_PAGE,
        )
        grouped: dict[datetime, list[MetricResult]] = {}
        seen_ids: set[UUID] = set()
        cursor: MetricPageCursor | None = None
        while True:
            page_query = query if cursor is None else query.model_copy(update={"after": cursor})
            entries = self._metrics.list_metric_index_page(page_query)
            if len(entries) > _MAX_METRIC_PAGE:
                raise DailyMarketPatternsError("metric index exceeded the bounded page size")
            visible: list[MetricIndexEntry] = []
            previous_key: tuple[object, str] | None = None
            for entry in entries:
                self._validate_index_entry(entry, query)
                key = (entry.available_at, str(entry.result_id))
                if previous_key is not None and key <= previous_key:
                    raise DailyMarketPatternsError("metric index page is not in stable order")
                previous_key = key
                if entry.result_id in seen_ids:
                    raise DailyMarketPatternsError("metric index repeated a prior page result")
                seen_ids.add(entry.result_id)
                if _legacy_visible(entry, request.known_at):
                    visible.append(entry)
            identifiers = tuple(entry.result_id for entry in visible)
            results = self._metrics.get_metric_results(identifiers) if identifiers else {}
            if set(results) != set(identifiers):
                raise DailyMarketPatternsError("metric index references a missing result")
            for entry in visible:
                result = results[entry.result_id]
                self._validate_hydrated_result(entry, result, query)
                grouped.setdefault(result.as_of, []).append(result)
            if len(entries) < _MAX_METRIC_PAGE:
                break
            next_cursor = MetricPageCursor(
                available_at=entries[-1].available_at,
                result_id=entries[-1].result_id,
            )
            if cursor == next_cursor:
                raise DailyMarketPatternsError("metric index did not advance its page cursor")
            cursor = next_cursor
        return {period: tuple(items) for period, items in grouped.items()}

    @staticmethod
    def _validate_index_entry(entry: MetricIndexEntry, query: MetricSeriesQuery) -> None:
        if (
            entry.asset_id != query.asset_id
            or entry.metric_key not in query.metric_keys
            or entry.source_id != query.source_id
        ):
            raise DailyMarketPatternsError("metric index returned evidence outside the scope")
        if entry.available_at > query.known_at:
            raise DailyMarketPatternsError("metric index returned future evidence")
        if query.as_of_from is not None and entry.as_of < query.as_of_from:
            raise DailyMarketPatternsError("metric index returned evidence before the range")
        if query.as_of_before is not None and entry.as_of >= query.as_of_before:
            raise DailyMarketPatternsError("metric index returned evidence after the range")
        if query.after is not None and (
            entry.available_at,
            str(entry.result_id),
        ) <= (
            query.after.available_at,
            str(query.after.result_id),
        ):
            raise DailyMarketPatternsError("metric index page did not advance its cursor")

    @staticmethod
    def _validate_hydrated_result(
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
            raise DailyMarketPatternsError("metric index projection changed during hydration")
        if result.available_at > query.known_at:
            raise DailyMarketPatternsError("metric result is not available at known_at")

    def _point(
        self,
        request: DailyMarketPatternsRequest,
        close: NormalizedObservation,
        period_metrics: tuple[MetricResult, ...],
        *,
        source_id: str,
    ) -> DailyMarketPatternPoint:
        try:
            selected = self._selector.select(
                rule=_PATTERN_EVIDENCE_RULE,
                metrics=period_metrics,
                source_id=source_id,
                known_at=request.known_at,
            )
        except (AmbiguousAnalyticalMetricError, MetricCutContractError) as error:
            raise AmbiguousDailyPatternEvidenceError(str(error)) from error
        by_key: dict[str, MetricResult] = {}
        for metric in selected:
            if metric.as_of != close.observed_at:
                raise DailyMarketPatternsError(
                    "the metric selector returned evidence from another period"
                )
            if metric.metric_key in by_key:
                raise AmbiguousDailyPatternEvidenceError(
                    "the metric selector returned duplicate evidence for one period"
                )
            by_key[metric.metric_key] = metric
        rsi = _evidence(RSI_KEY, by_key.get(RSI_KEY), period_metrics)
        histogram = _evidence(MACD_HISTOGRAM_KEY, by_key.get(MACD_HISTOGRAM_KEY), period_metrics)
        available = [close.available_at]
        for item in (rsi, histogram):
            if item.available_at is not None:
                available.append(item.available_at)
        return DailyMarketPatternPoint(
            as_of=close.observed_at,
            close_observation_id=close.observation_id,
            close=close.value,
            close_available_at=close.available_at,
            evidence_available_at=max(available),
            rsi=rsi,
            macd_histogram=histogram,
            macd_state=_momentum_state(histogram),
        )

    @staticmethod
    def _segments(
        points: tuple[DailyMarketPatternPoint, ...],
        *,
        asset_id: str,
        source_id: str,
    ) -> tuple[DailyMarketMomentumSegment, ...]:
        segments: list[DailyMarketMomentumSegment] = []
        start = 0
        while start < len(points):
            point = points[start]
            if point.macd_state is MacdMomentumState.UNAVAILABLE:
                start += 1
                continue
            end = start
            while end + 1 < len(points) and points[end + 1].macd_state is point.macd_state:
                end += 1
            window = points[start : end + 1]
            identifiers = tuple(
                item.macd_histogram.result_id
                for item in window
                if item.macd_histogram.result_id is not None
            )
            if len(identifiers) != len(window):
                raise DailyMarketPatternsError("a MACD segment lacks its evidence identity")
            left_censored = start == 0
            right_censored = end == len(points) - 1
            segments.append(
                DailyMarketMomentumSegment(
                    segment_id=daily_market_momentum_segment_id(
                        asset_id=asset_id,
                        source_id=source_id,
                        state=point.macd_state,
                        histogram_result_ids=identifiers,
                        left_censored=left_censored,
                        right_censored=right_censored,
                    ),
                    state=point.macd_state,
                    start_as_of=window[0].as_of,
                    end_as_of=window[-1].as_of,
                    observed_periods=len(window),
                    duration_seconds=int((window[-1].as_of - window[0].as_of).total_seconds()),
                    left_censored=left_censored,
                    right_censored=right_censored,
                    histogram_result_ids=identifiers,
                )
            )
            start = end + 1
        return tuple(segments)

    @staticmethod
    def _events(
        points: tuple[DailyMarketPatternPoint, ...],
        *,
        asset_id: str,
        source_id: str,
    ) -> tuple[DailyMarketPatternEvent, ...]:
        events: list[DailyMarketPatternEvent] = []
        for previous, current in zip(points, points[1:], strict=False):
            histogram_previous = previous.macd_histogram
            histogram_current = current.macd_histogram
            if histogram_previous.value is not None and histogram_current.value is not None:
                if histogram_previous.value <= _ZERO < histogram_current.value:
                    events.append(
                        _event(
                            DailyMarketPatternType.MACD_CROSS_POSITIVE,
                            "previous_histogram <= 0 < current_histogram",
                            previous,
                            current,
                            histogram_previous,
                            histogram_current,
                            asset_id=asset_id,
                            source_id=source_id,
                        )
                    )
                elif histogram_previous.value >= _ZERO > histogram_current.value:
                    events.append(
                        _event(
                            DailyMarketPatternType.MACD_CROSS_NEGATIVE,
                            "previous_histogram >= 0 > current_histogram",
                            previous,
                            current,
                            histogram_previous,
                            histogram_current,
                            asset_id=asset_id,
                            source_id=source_id,
                        )
                    )
            rsi_previous = previous.rsi
            rsi_current = current.rsi
            if (
                rsi_previous.value is not None
                and rsi_current.value is not None
                and rsi_previous.value <= _RSI_THRESHOLD < rsi_current.value
            ):
                events.append(
                    _event(
                        DailyMarketPatternType.RSI_CROSS_ABOVE_30,
                        "previous_rsi <= 30 < current_rsi",
                        previous,
                        current,
                        rsi_previous,
                        rsi_current,
                        asset_id=asset_id,
                        source_id=source_id,
                    )
                )
        return tuple(events)


def _legacy_visible(entry: MetricIndexEntry, known_at: datetime) -> bool:
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
        raise DailyMarketPatternsError("metric index contains a malformed legacy cut") from error
    if legacy.tzinfo is None or legacy.utcoffset() is None:
        raise DailyMarketPatternsError("metric index contains a timezone-naive legacy cut")
    return legacy <= known_at


def _evidence(
    metric_key: str,
    metric: MetricResult | None,
    period_metrics: tuple[MetricResult, ...],
) -> DailyMarketPatternMetricEvidence:
    if metric is None:
        present = any(item.metric_key == metric_key for item in period_metrics)
        return DailyMarketPatternMetricEvidence(
            metric_key=metric_key,
            status="missing",
            reason="metric_not_eligible" if present else "metric_not_persisted",
        )
    return DailyMarketPatternMetricEvidence(
        metric_key=metric_key,
        status="available",
        value=metric.value,
        result_id=metric.result_id,
        algorithm_version=metric.algorithm_version,
        unit=metric.unit,
        available_at=metric.available_at,
        quality=metric.quality,
    )


def _momentum_state(evidence: DailyMarketPatternMetricEvidence) -> MacdMomentumState:
    if evidence.status == "missing" or evidence.value is None:
        return MacdMomentumState.UNAVAILABLE
    if evidence.value > _ZERO:
        return MacdMomentumState.POSITIVE
    if evidence.value < _ZERO:
        return MacdMomentumState.NEGATIVE
    return MacdMomentumState.NEUTRAL


def _event(
    pattern_type: DailyMarketPatternType,
    comparison: str,
    previous: DailyMarketPatternPoint,
    current: DailyMarketPatternPoint,
    previous_evidence: DailyMarketPatternMetricEvidence,
    current_evidence: DailyMarketPatternMetricEvidence,
    *,
    asset_id: str,
    source_id: str,
) -> DailyMarketPatternEvent:
    previous_id = previous_evidence.result_id
    current_id = current_evidence.result_id
    previous_value = previous_evidence.value
    current_value = current_evidence.value
    if previous_id is None or current_id is None or previous_value is None or current_value is None:
        raise DailyMarketPatternsError("a crossing event requires both metric rows")
    return DailyMarketPatternEvent(
        event_id=daily_market_pattern_event_id(
            asset_id=asset_id,
            source_id=source_id,
            pattern_type=pattern_type,
            previous=previous,
            current=current,
        ),
        pattern_type=pattern_type,
        previous_as_of=previous.as_of,
        current_as_of=current.as_of,
        previous_close_observation_id=previous.close_observation_id,
        current_close_observation_id=current.close_observation_id,
        previous_metric_result_id=previous_id,
        current_metric_result_id=current_id,
        previous_value=previous_value,
        current_value=current_value,
        comparison=comparison,
        previous_evidence_available_at=previous.evidence_available_at,
        current_evidence_available_at=current.evidence_available_at,
        evidence_available_at=max(
            previous.evidence_available_at,
            current.evidence_available_at,
        ),
    )


__all__ = [
    "AmbiguousDailyCloseRevisionError",
    "AmbiguousDailyPatternEvidenceError",
    "DailyMarketPatternsError",
    "DailyMarketPatternsService",
    "DuplicateDailyCloseDateError",
    "PatternMetricPort",
    "UnsupportedDailyPatternsCurrencyError",
]
