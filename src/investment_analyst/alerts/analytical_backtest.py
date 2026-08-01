"""Read-only point-in-time replay for analytical screening rules."""

import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.alerts.analytical_engine import AnalyticalScreeningEngine
from investment_analyst.alerts.analytical_models import (
    AnalyticalConditionState,
    AnalyticalScreeningDomain,
    AnalyticalScreeningRequest,
    AnalyticalScreeningResult,
    AnalyticalScreeningRule,
    ScreeningDecimal,
)
from investment_analyst.alerts.analytical_monitor import (
    AnalyticalMetricSnapshotSelector,
)
from investment_analyst.alerts.analytical_rule_registry import (
    AnalyticalRuleRegistryStore,
)
from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.core.models import AssetClass, MetricResult
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.workspace.models import WorkspaceAccessMode

_BACKTEST_NAMESPACE = UUID("b1fa419d-177e-5837-9758-7d3051af6881")


class AnalyticalBacktestError(RuntimeError):
    """Base error for unavailable or ambiguous backtest evidence."""


class AnalyticalBacktestUnavailableError(AnalyticalBacktestError):
    """Raised when no compatible persisted source or snapshots exist."""


class AnalyticalBacktestAmbiguousSourceError(AnalyticalBacktestError):
    """Raised when compatible metrics identify more than one source."""


class AnalyticalBacktestRequest(ContractModel):
    """One bounded local backtest request."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["analytical-backtest-request-v1"] = "analytical-backtest-request-v1"
    rule_id: NonEmptyStr
    asset_id: NonEmptyStr
    max_cuts: int = Field(default=200, ge=20, le=500)

    @model_validator(mode="after")
    def reject_boolean_limit(self) -> "AnalyticalBacktestRequest":
        """Reject booleans accepted by Python's integer hierarchy."""
        if isinstance(self.max_cuts, bool):
            raise ValueError("max_cuts must be an integer")
        return self


class AnalyticalBacktestEvaluation(ContractModel):
    """One historical screening result plus simulated lifecycle changes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result: AnalyticalScreeningResult
    candidate_opened: bool
    candidate_resolved: bool

    @model_validator(mode="after")
    def validate_lifecycle_flags(self) -> "AnalyticalBacktestEvaluation":
        """A single cut cannot open and resolve the same candidate."""
        if self.candidate_opened and self.candidate_resolved:
            raise ValueError("one backtest cut cannot both open and resolve a candidate")
        return self


class AnalyticalBacktestResult(ContractModel):
    """Transparent bounded replay summary without return-performance claims."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["analytical-backtest-result-v1"] = "analytical-backtest-result-v1"
    backtest_id: UUID
    rule: AnalyticalScreeningRule
    asset_id: NonEmptyStr
    asset_class: AssetClass
    source_id: NonEmptyStr
    total_available_cuts: int = Field(ge=1)
    truncated: bool
    evaluations: tuple[AnalyticalBacktestEvaluation, ...] = Field(
        min_length=1,
        max_length=500,
    )
    matched_count: int = Field(ge=0)
    not_evaluable_count: int = Field(ge=0)
    candidate_activation_count: int = Field(ge=0)
    candidate_resolution_count: int = Field(ge=0)
    match_rate: ScreeningDecimal
    first_known_at: UTCDateTime
    last_known_at: UTCDateTime
    traceability_verified: Literal[True] = True
    limitations: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_summary(self) -> "AnalyticalBacktestResult":
        """Derive all summary counts from the exact ordered evaluations."""
        if tuple(item.result.known_at for item in self.evaluations) != tuple(
            sorted(item.result.known_at for item in self.evaluations)
        ):
            raise ValueError("backtest evaluations must be chronologically ordered")
        if any(
            item.result.rule.semantic_fingerprint() != self.rule.semantic_fingerprint()
            or item.result.asset_id != self.asset_id
            or item.result.asset_class is not self.asset_class
            or item.result.source_id != self.source_id
            for item in self.evaluations
        ):
            raise ValueError("backtest evaluations do not share one rule and context")
        matched = sum(item.result.matched for item in self.evaluations)
        not_evaluable = sum(
            any(
                condition.state is AnalyticalConditionState.NOT_EVALUABLE
                for condition in item.result.conditions
            )
            for item in self.evaluations
        )
        activations = sum(item.candidate_opened for item in self.evaluations)
        resolutions = sum(item.candidate_resolved for item in self.evaluations)
        if (
            self.matched_count != matched
            or self.not_evaluable_count != not_evaluable
            or self.candidate_activation_count != activations
            or self.candidate_resolution_count != resolutions
        ):
            raise ValueError("backtest summary counts do not match evaluations")
        expected_rate = _match_rate(matched, len(self.evaluations))
        if self.match_rate != expected_rate:
            raise ValueError("backtest match_rate does not match evaluations")
        if self.first_known_at != self.evaluations[0].result.known_at:
            raise ValueError("backtest first_known_at is inconsistent")
        if self.last_known_at != self.evaluations[-1].result.known_at:
            raise ValueError("backtest last_known_at is inconsistent")
        if self.backtest_id != analytical_backtest_id(
            self.rule,
            asset_id=self.asset_id,
            asset_class=self.asset_class,
            source_id=self.source_id,
            total_available_cuts=self.total_available_cuts,
            evaluations=self.evaluations,
        ):
            raise ValueError("analytical backtest_id is not deterministic")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return complete exact evidence for the local API."""
        return self.model_dump(mode="json")


class AnalyticalBacktestService:
    """Replay one current configured rule over persisted point-in-time metrics."""

    def __init__(
        self,
        runtime: ApplicationRuntime,
        workspace: Path,
        registry: AnalyticalRuleRegistryStore,
        *,
        engine: AnalyticalScreeningEngine | None = None,
        selector: AnalyticalMetricSnapshotSelector | None = None,
    ) -> None:
        self._runtime = runtime
        self._workspace = workspace.expanduser().resolve(strict=False)
        self._registry = registry
        self._engine = engine or AnalyticalScreeningEngine()
        self._selector = selector or AnalyticalMetricSnapshotSelector()

    def run(self, request: AnalyticalBacktestRequest) -> AnalyticalBacktestResult:
        """Read metrics once and produce a bounded deterministic historical replay."""
        rule = self._registry.get(request.rule_id)
        asset = self._runtime.catalog.get(request.asset_id)
        if asset.asset_class not in rule.asset_classes:
            raise AnalyticalBacktestUnavailableError(
                "the analytical rule is not compatible with this asset class"
            )
        with self._runtime.open_storage(
            StorageLocationRequest(workspace=self._workspace),
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            metrics = tuple(storage.metric_results.list(asset_id=asset.asset_id))
        relevant = self._relevant_metrics(rule, metrics)
        sources = tuple(
            sorted(
                {
                    source_id
                    for metric in relevant
                    if isinstance(
                        source_id := metric.parameters.get("source_id"),
                        str,
                    )
                    and source_id.strip()
                }
            )
        )
        if not sources:
            raise AnalyticalBacktestUnavailableError(
                "no compatible persisted metrics exist for this rule and asset"
            )
        if len(sources) > 1:
            raise AnalyticalBacktestAmbiguousSourceError(
                "compatible backtest metrics identify multiple sources"
            )
        source_id = sources[0]
        scoped = tuple(item for item in relevant if item.parameters.get("source_id") == source_id)
        cuts = self._cuts(rule, scoped)
        if not cuts:
            raise AnalyticalBacktestUnavailableError(
                "no valid point-in-time cuts exist for this rule and asset"
            )
        total_available_cuts = len(cuts)
        selected_cuts = cuts[-request.max_cuts :]
        evaluations = self._evaluate_cuts(
            rule,
            asset.asset_id,
            asset.asset_class,
            source_id,
            scoped,
            selected_cuts,
        )
        if not evaluations:
            raise AnalyticalBacktestUnavailableError(
                "no distinct compatible metric snapshots exist in the requested cuts"
            )
        lifecycle = _simulate_lifecycle(rule, evaluations)
        matched = sum(item.result.matched for item in lifecycle)
        not_evaluable = sum(
            any(
                condition.state is AnalyticalConditionState.NOT_EVALUABLE
                for condition in item.result.conditions
            )
            for item in lifecycle
        )
        limitations = (
            "El replay mide frecuencia y ruido de la regla sobre snapshots locales persistidos.",
            "No calcula retornos posteriores, rentabilidad, precisión predictiva "
            "ni una recomendación.",
            "La cobertura histórica depende de los cortes que ya existen en este workspace.",
        )
        return AnalyticalBacktestResult(
            backtest_id=analytical_backtest_id(
                rule,
                asset_id=asset.asset_id,
                asset_class=asset.asset_class,
                source_id=source_id,
                total_available_cuts=total_available_cuts,
                evaluations=lifecycle,
            ),
            rule=rule,
            asset_id=asset.asset_id,
            asset_class=asset.asset_class,
            source_id=source_id,
            total_available_cuts=total_available_cuts,
            truncated=total_available_cuts > len(selected_cuts),
            evaluations=lifecycle,
            matched_count=matched,
            not_evaluable_count=not_evaluable,
            candidate_activation_count=sum(item.candidate_opened for item in lifecycle),
            candidate_resolution_count=sum(item.candidate_resolved for item in lifecycle),
            match_rate=_match_rate(matched, len(lifecycle)),
            first_known_at=lifecycle[0].result.known_at,
            last_known_at=lifecycle[-1].result.known_at,
            limitations=limitations,
        )

    @staticmethod
    def _relevant_metrics(
        rule: AnalyticalScreeningRule,
        metrics: tuple[MetricResult, ...],
    ) -> tuple[MetricResult, ...]:
        conditions = {item.metric_key: item for item in rule.conditions}
        return tuple(
            item
            for item in metrics
            if (condition := conditions.get(item.metric_key)) is not None
            and item.algorithm_version == condition.algorithm_version
            and item.unit == condition.unit
            and item.quality in condition.accepted_qualities
            and all(
                item.parameters.get(name) == expected
                for name, expected in condition.parameter_filters.items()
            )
        )

    @staticmethod
    def _cuts(
        rule: AnalyticalScreeningRule,
        metrics: tuple[MetricResult, ...],
    ) -> tuple[datetime, ...]:
        if rule.domain is AnalyticalScreeningDomain.FUNDAMENTALS:
            return tuple(sorted({item.available_at for item in metrics}))
        return tuple(
            sorted(
                {parsed for item in metrics if (parsed := _known_at_parameter(item)) is not None}
            )
        )

    def _evaluate_cuts(
        self,
        rule: AnalyticalScreeningRule,
        asset_id: str,
        asset_class: AssetClass,
        source_id: str,
        metrics: tuple[MetricResult, ...],
        cuts: tuple[datetime, ...],
    ) -> tuple[AnalyticalScreeningResult, ...]:
        results: list[AnalyticalScreeningResult] = []
        previous_evidence: tuple[UUID, ...] | None = None
        for known_at in cuts:
            selected = self._selector.select(
                rule=rule,
                metrics=metrics,
                source_id=source_id,
                known_at=known_at,
            )
            evidence = tuple(item.result_id for item in selected)
            if evidence == previous_evidence:
                continue
            previous_evidence = evidence
            results.append(
                self._engine.evaluate(
                    AnalyticalScreeningRequest(
                        rule=rule,
                        asset_id=asset_id,
                        asset_class=asset_class,
                        source_id=source_id,
                        known_at=known_at,
                        computed_at=known_at,
                        metrics=selected,
                    )
                )
            )
        return tuple(results)


def _known_at_parameter(metric: MetricResult) -> datetime | None:
    value = metric.parameters.get("known_at")
    if value is None:
        return None
    if not isinstance(value, str):
        raise AnalyticalBacktestError("market metric known_at parameter is invalid")
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise AnalyticalBacktestError("market metric known_at parameter is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AnalyticalBacktestError("market metric known_at parameter must include timezone")
    return parsed.astimezone(UTC)


def _simulate_lifecycle(
    rule: AnalyticalScreeningRule,
    results: tuple[AnalyticalScreeningResult, ...],
) -> tuple[AnalyticalBacktestEvaluation, ...]:
    candidate_open = False
    cooldown_until: datetime | None = None
    confirmation_count = 0
    latest_confirmation_period: datetime | None = None
    evaluations: list[AnalyticalBacktestEvaluation] = []
    for result in results:
        opened = False
        resolved = False
        if result.matched:
            if result.as_of != latest_confirmation_period:
                confirmation_count += 1
                latest_confirmation_period = result.as_of
        else:
            confirmation_count = 0
            latest_confirmation_period = None
        if candidate_open:
            if result.retained is False:
                candidate_open = False
                resolved = True
        elif (
            result.activated
            and result.as_of is not None
            and confirmation_count >= rule.confirmations_required
            and (cooldown_until is None or result.known_at >= cooldown_until)
        ):
            candidate_open = True
            opened = True
            cooldown_until = result.known_at + timedelta(seconds=rule.cooldown_seconds)
        evaluations.append(
            AnalyticalBacktestEvaluation(
                result=result,
                candidate_opened=opened,
                candidate_resolved=resolved,
            )
        )
    return tuple(evaluations)


def _match_rate(matched: int, total: int) -> Decimal:
    with localcontext() as context:
        context.prec = 34
        context.rounding = ROUND_HALF_EVEN
        return (Decimal(matched) / Decimal(total)).quantize(Decimal("0.0001"))


def analytical_backtest_id(
    rule: AnalyticalScreeningRule,
    *,
    asset_id: str,
    asset_class: AssetClass,
    source_id: str,
    total_available_cuts: int,
    evaluations: tuple[AnalyticalBacktestEvaluation, ...],
) -> UUID:
    """Return one stable identity for exact rule, evidence, and bounded coverage."""
    payload = {
        "rule_fingerprint": rule.semantic_fingerprint(),
        "asset_id": asset_id,
        "asset_class": asset_class.value,
        "source_id": source_id,
        "total_available_cuts": total_available_cuts,
        "evaluations": [
            {
                "result_id": str(item.result.result_id),
                "candidate_opened": item.candidate_opened,
                "candidate_resolved": item.candidate_resolved,
            }
            for item in evaluations
        ],
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return uuid5(_BACKTEST_NAMESPACE, encoded)


__all__ = [
    "AnalyticalBacktestAmbiguousSourceError",
    "AnalyticalBacktestError",
    "AnalyticalBacktestEvaluation",
    "AnalyticalBacktestRequest",
    "AnalyticalBacktestResult",
    "AnalyticalBacktestService",
    "AnalyticalBacktestUnavailableError",
    "analytical_backtest_id",
]
