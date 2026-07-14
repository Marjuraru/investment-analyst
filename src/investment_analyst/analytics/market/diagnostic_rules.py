"""Pure Decimal-only rules for auditable market-condition diagnostics."""

import json
from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext
from uuid import UUID, uuid5

from investment_analyst.analytics.market.diagnostic_models import (
    MarketDiagnosticRequest,
    MarketMetricSnapshot,
)
from investment_analyst.analytics.market.diagnostic_selection import (
    describe_missing_requirements,
)
from investment_analyst.core.models import (
    DataQuality,
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
    EvidenceDirection,
    MetricResult,
)

TREND_WEIGHT = Decimal("0.60")
MOMENTUM_WEIGHT = Decimal("0.40")
POSITIVE_THRESHOLD = Decimal("60")
NEGATIVE_THRESHOLD = Decimal("40")
TREND_SCALE = Decimal("1000")
MOMENTUM_SCALE = Decimal("1000")
VOLATILITY_REFERENCE = Decimal("0.10")
RELATIVE_VOLUME_REFERENCE = Decimal("2")
ALGORITHM_VERSION = "market-diagnostic-v1-decimal34"

_DIAGNOSTIC_NAMESPACE = UUID("5be46ad2-a8bf-5d2f-9910-dca6f7d357de")


class MarketDiagnosticRuleError(RuntimeError):
    """Base error for deterministic diagnostic rules."""


class InvalidDiagnosticMetricError(MarketDiagnosticRuleError):
    """Raised when selected metric values cannot support the published rules."""


def clamp(value: Decimal, minimum: Decimal, maximum: Decimal) -> Decimal:
    """Limit one Decimal value to an inclusive Decimal interval."""
    if any(
        isinstance(item, (bool, float)) or not isinstance(item, Decimal)
        for item in (
            value,
            minimum,
            maximum,
        )
    ):
        raise TypeError("clamp accepts Decimal values only")
    if not value.is_finite() or not minimum.is_finite() or not maximum.is_finite():
        raise ValueError("clamp values must be finite")
    if minimum > maximum:
        raise ValueError("minimum must not be greater than maximum")
    return min(max(value, minimum), maximum)


def _quality(results: tuple[MetricResult, ...]) -> DataQuality:
    precedence = (
        DataQuality.SUSPECT,
        DataQuality.PARTIAL,
        DataQuality.DELAYED,
        DataQuality.VALID,
    )
    qualities = {result.quality for result in results}
    for candidate in precedence:
        if candidate in qualities:
            return candidate
    return DataQuality.PARTIAL


def _quality_cap(quality: DataQuality) -> Decimal:
    return {
        DataQuality.VALID: Decimal("1.00"),
        DataQuality.DELAYED: Decimal("0.90"),
        DataQuality.PARTIAL: Decimal("0.75"),
        DataQuality.SUSPECT: Decimal("0.50"),
    }[quality]


def _direction(value: Decimal) -> EvidenceDirection:
    if value > 0:
        return EvidenceDirection.SUPPORTS
    if value < 0:
        return EvidenceDirection.OPPOSES
    return EvidenceDirection.NEUTRAL


def _utc(value: datetime, *, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketDiagnosticRuleError(f"{name} must include timezone information")
    return value.astimezone(UTC)


def _validate_metric_values(snapshot: MarketMetricSnapshot) -> None:
    for result in snapshot.metric_results():
        if not isinstance(result.value, Decimal) or not result.value.is_finite():
            raise InvalidDiagnosticMetricError("diagnostic metrics must contain finite Decimals")
    if snapshot.long_sma.value <= 0:
        raise InvalidDiagnosticMetricError("long SMA must be greater than zero")
    if snapshot.short_sma.value <= 0:
        raise InvalidDiagnosticMetricError("short SMA must be greater than zero")
    if snapshot.rolling_volatility.value < 0:
        raise InvalidDiagnosticMetricError("rolling volatility must be non-negative")
    if snapshot.relative_volume.value < 0:
        raise InvalidDiagnosticMetricError("relative volume must be non-negative")


def diagnostic_result_id(
    request: MarketDiagnosticRequest,
    *,
    selected_metric_result_ids: tuple[UUID, ...],
    missing_requirements: tuple[str, ...],
    final_score: Decimal,
    confidence: Decimal,
    verdict: DiagnosticVerdict,
    quality: DataQuality,
) -> UUID:
    """Build the stable UUID5 identity for a normal or insufficient diagnostic."""
    windows = {
        "short_sma_window": request.short_sma_window,
        "long_sma_window": request.long_sma_window,
        "volatility_window": request.volatility_window,
        "relative_volume_window": request.relative_volume_window,
    }
    document: dict[str, object] = {
        "asset_id": request.query.asset_id,
        "source_id": request.query.source_id,
        "known_at": request.query.known_at.isoformat(),
        "start": request.query.start.isoformat(),
        "end": request.query.end.isoformat(),
        **windows,
        "algorithm_version": ALGORITHM_VERSION,
        "verdict": verdict.value,
    }
    if verdict is DiagnosticVerdict.INSUFFICIENT_DATA:
        document["missing_requirements"] = list(missing_requirements)
    else:
        document.update(
            {
                "selected_metric_result_ids": [
                    str(identifier) for identifier in selected_metric_result_ids
                ],
                "final_score": str(final_score),
                "confidence": str(confidence),
                "quality": quality.value,
            }
        )
    canonical = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(_DIAGNOSTIC_NAMESPACE, canonical)


class MarketDiagnosticEngine:
    """Apply fixed transparent rules to one complete persisted metric snapshot."""

    def compute(
        self,
        request: MarketDiagnosticRequest,
        snapshot: MarketMetricSnapshot | None,
        *,
        computed_at: datetime,
        fallback_metric_results: tuple[MetricResult, ...] = (),
    ) -> DiagnosticResult:
        """Produce a normal or insufficient-data diagnostic without recalculating metrics."""
        normalized_computed_at = _utc(computed_at, name="computed_at")
        if snapshot is None:
            return self._insufficient(
                request,
                normalized_computed_at,
                fallback_metric_results,
            )
        return self._normal(request, snapshot, normalized_computed_at)

    @staticmethod
    def _normal(
        request: MarketDiagnosticRequest,
        snapshot: MarketMetricSnapshot,
        computed_at: datetime,
    ) -> DiagnosticResult:
        if snapshot.asset_id != request.query.asset_id:
            raise InvalidDiagnosticMetricError("snapshot asset does not match request")
        if snapshot.source_id != request.query.source_id:
            raise InvalidDiagnosticMetricError("snapshot source does not match request")
        if snapshot.known_at != request.query.known_at:
            raise InvalidDiagnosticMetricError("snapshot known_at does not match request")
        if not request.query.start <= snapshot.as_of < request.query.end:
            raise InvalidDiagnosticMetricError("snapshot as_of is outside the request range")
        _validate_metric_values(snapshot)
        results = snapshot.metric_results()
        available_at = max(result.available_at for result in results)
        if available_at > computed_at:
            raise MarketDiagnosticRuleError(
                "computed_at must not be earlier than diagnostic availability"
            )

        with localcontext(Context(prec=34)):
            trend_spread = snapshot.short_sma.value / snapshot.long_sma.value - Decimal("1")
            trend_score = clamp(
                Decimal("50") + trend_spread * TREND_SCALE,
                Decimal("0"),
                Decimal("100"),
            )
            momentum_score = clamp(
                Decimal("50") + snapshot.simple_return.value * MOMENTUM_SCALE,
                Decimal("0"),
                Decimal("100"),
            )
            trend_contribution = trend_score * TREND_WEIGHT
            momentum_contribution = momentum_score * MOMENTUM_WEIGHT
            final_score = trend_contribution + momentum_contribution
            verdict = (
                DiagnosticVerdict.POSITIVE
                if final_score >= POSITIVE_THRESHOLD
                else DiagnosticVerdict.NEGATIVE
                if final_score <= NEGATIVE_THRESHOLD
                else DiagnosticVerdict.NEUTRAL
            )

            participation_factor = clamp(
                snapshot.relative_volume.value / RELATIVE_VOLUME_REFERENCE,
                Decimal("0"),
                Decimal("1"),
            )
            stability_factor = clamp(
                Decimal("1") - snapshot.rolling_volatility.value / VOLATILITY_REFERENCE,
                Decimal("0"),
                Decimal("1"),
            )
            raw_confidence = (participation_factor + stability_factor) / Decimal("2")
            quality = _quality(results)
            confidence = min(raw_confidence, _quality_cap(quality))

            trend_signal = clamp(
                trend_spread / Decimal("0.05"),
                Decimal("-1"),
                Decimal("1"),
            )
            momentum_signal = clamp(
                snapshot.simple_return.value / Decimal("0.05"),
                Decimal("-1"),
                Decimal("1"),
            )

        components = [
            DiagnosticComponent(
                component_key="trend_alignment",
                score=trend_score,
                weight=TREND_WEIGHT,
                weighted_contribution=trend_contribution,
                metric_result_ids=[snapshot.short_sma.result_id, snapshot.long_sma.result_id],
                explanation=(
                    "Compares the selected short and long simple moving averages using the "
                    "published trend-spread rule."
                ),
            ),
            DiagnosticComponent(
                component_key="recent_momentum",
                score=momentum_score,
                weight=MOMENTUM_WEIGHT,
                weighted_contribution=momentum_contribution,
                metric_result_ids=[snapshot.simple_return.result_id],
                explanation=(
                    "Maps the latest persisted one-bar simple return through the published "
                    "momentum rule."
                ),
            ),
        ]
        evidence = [
            DiagnosticEvidence(
                metric_result_id=snapshot.simple_return.result_id,
                direction=_direction(momentum_signal),
                contribution=momentum_signal,
                reason="The latest simple return supplies the directional momentum evidence.",
            ),
            DiagnosticEvidence(
                metric_result_id=snapshot.short_sma.result_id,
                direction=_direction(trend_signal),
                contribution=trend_signal / Decimal("2"),
                reason=(
                    "The short SMA forms one half of the explicit short-versus-long trend "
                    "comparison."
                ),
            ),
            DiagnosticEvidence(
                metric_result_id=snapshot.long_sma.result_id,
                direction=_direction(trend_signal),
                contribution=trend_signal / Decimal("2"),
                reason=(
                    "The long SMA forms one half of the explicit short-versus-long trend "
                    "comparison."
                ),
            ),
            DiagnosticEvidence(
                metric_result_id=snapshot.rolling_volatility.result_id,
                direction=EvidenceDirection.NEUTRAL,
                contribution=Decimal("0"),
                reason="Rolling volatility changes contextual confidence, not the verdict.",
            ),
            DiagnosticEvidence(
                metric_result_id=snapshot.relative_volume.result_id,
                direction=EvidenceDirection.NEUTRAL,
                contribution=Decimal("0"),
                reason="Relative volume changes contextual confidence, not the verdict.",
            ),
        ]
        selected_ids = snapshot.metric_result_ids()
        identifier = diagnostic_result_id(
            request,
            selected_metric_result_ids=selected_ids,
            missing_requirements=(),
            final_score=final_score,
            confidence=confidence,
            verdict=verdict,
            quality=quality,
        )
        summary = (
            f"Descriptive market condition for {request.query.asset_id} from "
            f"{request.query.source_id}, using SMA windows {request.short_sma_window} and "
            f"{request.long_sma_window}: {verdict.value}. This is not a recommendation; "
            "confidence describes context strength and data quality, not probability."
        )
        return DiagnosticResult(
            diagnostic_id=identifier,
            asset_id=request.query.asset_id,
            mode=DiagnosticMode.MARKET,
            verdict=verdict,
            final_score=final_score,
            confidence=confidence,
            as_of=snapshot.as_of,
            available_at=available_at,
            computed_at=computed_at,
            components=components,
            evidence=evidence,
            algorithm_version=ALGORITHM_VERSION,
            summary=summary,
            quality=quality,
        )

    @staticmethod
    def _insufficient(
        request: MarketDiagnosticRequest,
        computed_at: datetime,
        fallback_metric_results: tuple[MetricResult, ...],
    ) -> DiagnosticResult:
        missing = describe_missing_requirements(request, fallback_metric_results)
        if fallback_metric_results:
            as_of = max(result.as_of for result in fallback_metric_results)
            available_at = max(result.available_at for result in fallback_metric_results)
            quality = _quality(fallback_metric_results)
        else:
            as_of = request.query.start
            available_at = request.query.known_at
            quality = DataQuality.PARTIAL
        if available_at > computed_at:
            raise MarketDiagnosticRuleError(
                "computed_at must not be earlier than diagnostic availability"
            )
        identifier = diagnostic_result_id(
            request,
            selected_metric_result_ids=(),
            missing_requirements=missing,
            final_score=Decimal("0"),
            confidence=Decimal("0"),
            verdict=DiagnosticVerdict.INSUFFICIENT_DATA,
            quality=quality,
        )
        summary = (
            "Insufficient persisted metrics for a common point-in-time snapshot. Missing: "
            f"{', '.join(missing)}. No directional reading is produced, and this output is "
            "not a recommendation."
        )
        return DiagnosticResult(
            diagnostic_id=identifier,
            asset_id=request.query.asset_id,
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.INSUFFICIENT_DATA,
            final_score=Decimal("0"),
            confidence=Decimal("0"),
            as_of=as_of,
            available_at=available_at,
            computed_at=computed_at,
            components=[],
            evidence=[],
            algorithm_version=ALGORITHM_VERSION,
            summary=summary,
            quality=quality,
        )
