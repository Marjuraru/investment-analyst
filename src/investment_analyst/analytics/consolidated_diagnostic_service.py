"""Read-only point-in-time service for separate Apple diagnostic modes."""

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticRequest,
    ConsolidatedDiagnosticSection,
    ConsolidatedDiagnosticStatus,
    ConsolidatedDiagnosticView,
    ConsolidatedSectionStatus,
    ConsolidatedTemporalContext,
)
from investment_analyst.analytics.market.diagnostic_rules import (
    ALGORITHM_VERSION as MARKET_DIAGNOSTIC_ALGORITHM_VERSION,
)
from investment_analyst.analytics.market.statistics_definitions import (
    RELATIVE_VOLUME_KEY,
    SIMPLE_RETURN_KEY,
    SMA_KEY,
    VOLATILITY_KEY,
)
from investment_analyst.core.models import (
    DataFrequency,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
    MetricResult,
)
from investment_analyst.core.models.diagnostic import DECIMAL_TOLERANCE
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SEC_FUNDAMENTAL_METRIC_DEFINITIONS,
)
from investment_analyst.storage import LocalStorage

FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION = "sec-aapl-fundamental-diagnostic-v1.1-decimal34"

_MARKET_METRIC_KEYS = frozenset({SIMPLE_RETURN_KEY, SMA_KEY, VOLATILITY_KEY, RELATIVE_VOLUME_KEY})
_FUNDAMENTAL_DEFINITION_BY_KEY = {
    definition.metric_name: definition for definition in SEC_FUNDAMENTAL_METRIC_DEFINITIONS
}
_FUNDAMENTAL_METRIC_KEYS = frozenset(_FUNDAMENTAL_DEFINITION_BY_KEY)
_ALLOWED_FUNDAMENTAL_FREQUENCIES = frozenset({DataFrequency.ANNUAL, DataFrequency.QUARTERLY})


class ConsolidatedDiagnosticQueryError(RuntimeError):
    """Base error for consolidated diagnostic queries."""


class MalformedStoredDiagnosticError(ConsolidatedDiagnosticQueryError):
    """Raised when a stored current-version diagnostic violates its contract."""


class AmbiguousStoredDiagnosticRevisionError(ConsolidatedDiagnosticQueryError):
    """Raised when equally available revisions disagree semantically."""


class MissingReferencedMetricResultError(ConsolidatedDiagnosticQueryError):
    """Raised when a diagnostic references a missing persisted metric result."""


class MixedFundamentalFrequencyError(ConsolidatedDiagnosticQueryError):
    """Raised when one fundamental diagnostic mixes annual and quarterly metrics."""


class ConsolidatedDiagnosticTraceabilityError(ConsolidatedDiagnosticQueryError):
    """Raised when in-memory diagnostic-to-metric traceability fails."""


@dataclass(frozen=True, slots=True)
class _Candidate:
    diagnostic: DiagnosticResult
    metric_ids: tuple[UUID, ...]
    fundamental_frequency: DataFrequency | None


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is UTC and value.utcoffset() is not None


def _require_utc(value: datetime, name: str) -> None:
    if not _is_utc(value):
        raise MalformedStoredDiagnosticError(f"{name} must be normalized to UTC")


def _finite_decimal(value: Decimal, name: str) -> None:
    if isinstance(value, (bool, float)) or not isinstance(value, Decimal):
        raise MalformedStoredDiagnosticError(f"{name} must use Decimal")
    if not value.is_finite():
        raise MalformedStoredDiagnosticError(f"{name} must be finite")


def _referenced_metric_ids(diagnostic: DiagnosticResult) -> tuple[UUID, ...]:
    identifiers = {
        metric_id
        for component in diagnostic.components
        for metric_id in component.metric_result_ids
    }
    identifiers.update(item.metric_result_id for item in diagnostic.evidence)
    return tuple(sorted(identifiers, key=str))


def _metric_frequency(result: MetricResult) -> DataFrequency:
    value = result.parameters.get("frequency")
    if not isinstance(value, str):
        raise MalformedStoredDiagnosticError(
            f"fundamental metric {result.result_id} has no explicit frequency"
        )
    try:
        frequency = DataFrequency(value)
    except ValueError as error:
        raise MalformedStoredDiagnosticError(
            f"fundamental metric {result.result_id} has invalid frequency"
        ) from error
    if frequency not in _ALLOWED_FUNDAMENTAL_FREQUENCIES:
        raise MalformedStoredDiagnosticError(
            f"fundamental metric {result.result_id} must be annual or quarterly"
        )
    return frequency


def _validate_metric_common(result: MetricResult, asset_id: str) -> None:
    if result.asset_id != asset_id:
        raise ConsolidatedDiagnosticTraceabilityError(
            f"metric result {result.result_id} belongs to another asset"
        )
    _finite_decimal(result.value, f"metric result {result.result_id} value")
    _require_utc(result.as_of, "metric as_of")
    _require_utc(result.available_at, "metric available_at")
    _require_utc(result.computed_at, "metric computed_at")
    if result.available_at > result.computed_at:
        raise MalformedStoredDiagnosticError("metric available_at exceeds computed_at")
    if not result.unit.strip():
        raise MalformedStoredDiagnosticError("metric unit must not be empty")


def _validate_market_metric(result: MetricResult) -> None:
    if result.metric_key not in _MARKET_METRIC_KEYS:
        raise MalformedStoredDiagnosticError(
            f"market diagnostic references unsupported metric {result.metric_key!r}"
        )
    expected_unit = "USD" if result.metric_key == SMA_KEY else "ratio"
    if result.unit != expected_unit:
        raise MalformedStoredDiagnosticError(
            f"market metric {result.metric_key!r} has unexpected unit"
        )


def _validate_fundamental_metric(result: MetricResult) -> DataFrequency:
    definition = _FUNDAMENTAL_DEFINITION_BY_KEY.get(result.metric_key)
    if definition is None:
        raise MalformedStoredDiagnosticError(
            f"fundamental diagnostic references unsupported metric {result.metric_key!r}"
        )
    if result.unit != definition.unit:
        raise MalformedStoredDiagnosticError(
            f"fundamental metric {result.metric_key!r} has unexpected unit"
        )
    if result.algorithm_version != definition.algorithm_version:
        raise MalformedStoredDiagnosticError(
            f"fundamental metric {result.metric_key!r} has unexpected algorithm version"
        )
    return _metric_frequency(result)


def _semantic_identity(candidate: _Candidate) -> str:
    payload = candidate.diagnostic.model_dump(
        mode="json",
        exclude={"diagnostic_id", "computed_at"},
    )
    payload["metric_result_ids"] = [str(item) for item in candidate.metric_ids]
    payload["fundamental_frequency"] = (
        candidate.fundamental_frequency.value if candidate.fundamental_frequency else None
    )
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _validate_diagnostic_math(diagnostic: DiagnosticResult) -> None:
    _finite_decimal(diagnostic.final_score, "diagnostic final_score")
    _finite_decimal(diagnostic.confidence, "diagnostic confidence")
    if not Decimal("0") <= diagnostic.final_score <= Decimal("100"):
        raise MalformedStoredDiagnosticError("diagnostic score must be between 0 and 100")
    if not Decimal("0") <= diagnostic.confidence <= Decimal("1"):
        raise MalformedStoredDiagnosticError("diagnostic confidence must be between 0 and 1")
    if diagnostic.verdict is DiagnosticVerdict.INSUFFICIENT_DATA:
        if diagnostic.components or diagnostic.evidence:
            raise MalformedStoredDiagnosticError(
                "insufficient diagnostic cannot contain components or evidence"
            )
        return
    if not diagnostic.components or not diagnostic.evidence:
        raise MalformedStoredDiagnosticError("normal diagnostic requires components and evidence")
    weight_sum = sum((item.weight for item in diagnostic.components), Decimal("0"))
    if abs(weight_sum - Decimal("1")) > DECIMAL_TOLERANCE:
        raise MalformedStoredDiagnosticError("diagnostic component weights do not sum to one")
    contribution_sum = sum(
        (item.weighted_contribution for item in diagnostic.components),
        Decimal("0"),
    )
    if abs(contribution_sum - diagnostic.final_score) > DECIMAL_TOLERANCE:
        raise MalformedStoredDiagnosticError(
            "diagnostic final score does not match weighted contributions"
        )


def _validate_diagnostic_common(
    diagnostic: DiagnosticResult,
    *,
    request: ConsolidatedDiagnosticRequest,
    metric_index: dict[UUID, MetricResult],
) -> _Candidate:
    if diagnostic.asset_id != request.asset_id:
        raise MalformedStoredDiagnosticError("diagnostic asset does not match request")
    _require_utc(diagnostic.as_of, "diagnostic as_of")
    _require_utc(diagnostic.available_at, "diagnostic available_at")
    _require_utc(diagnostic.computed_at, "diagnostic computed_at")
    if diagnostic.available_at > diagnostic.computed_at:
        raise MalformedStoredDiagnosticError("diagnostic available_at exceeds computed_at")
    if diagnostic.as_of > diagnostic.computed_at:
        raise MalformedStoredDiagnosticError("diagnostic as_of exceeds computed_at")
    if not diagnostic.summary.strip():
        raise MalformedStoredDiagnosticError("diagnostic summary must not be empty")
    _validate_diagnostic_math(diagnostic)

    metric_ids = _referenced_metric_ids(diagnostic)
    if not metric_ids:
        if diagnostic.verdict is not DiagnosticVerdict.INSUFFICIENT_DATA:
            raise MalformedStoredDiagnosticError(
                "current-version diagnostic must reference persisted metric results"
            )
        if diagnostic.components or diagnostic.evidence:
            raise MalformedStoredDiagnosticError(
                "insufficient diagnostic must not contain components or evidence"
            )
        if diagnostic.final_score != 0 or diagnostic.confidence != 0:
            raise MalformedStoredDiagnosticError(
                "insufficient diagnostic score and confidence must be zero"
            )
        return _Candidate(diagnostic, metric_ids, None)

    metrics: list[MetricResult] = []
    for identifier in metric_ids:
        try:
            metric = metric_index[identifier]
        except KeyError as error:
            raise MissingReferencedMetricResultError(
                f"diagnostic references missing metric result {identifier}"
            ) from error
        _validate_metric_common(metric, request.asset_id)
        metrics.append(metric)

    fundamental_frequency: DataFrequency | None = None
    if diagnostic.mode is DiagnosticMode.MARKET:
        for metric in metrics:
            _validate_market_metric(metric)
    elif diagnostic.mode is DiagnosticMode.FUNDAMENTAL:
        frequencies = {_validate_fundamental_metric(metric) for metric in metrics}
        if len(frequencies) != 1:
            raise MixedFundamentalFrequencyError(
                "fundamental diagnostic references mixed metric frequencies"
            )
        fundamental_frequency = next(iter(frequencies))
    else:
        raise MalformedStoredDiagnosticError("only market and fundamental modes are supported")
    return _Candidate(diagnostic, metric_ids, fundamental_frequency)


def _current_version(mode: DiagnosticMode) -> str:
    if mode is DiagnosticMode.MARKET:
        return MARKET_DIAGNOSTIC_ALGORITHM_VERSION
    if mode is DiagnosticMode.FUNDAMENTAL:
        return FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION
    raise ValueError("unsupported diagnostic mode")


def _select_revision(
    candidates: list[_Candidate],
    *,
    mode: DiagnosticMode,
) -> tuple[list[_Candidate], int]:
    grouped: dict[tuple[DiagnosticMode, str, DataFrequency | None, datetime], list[_Candidate]]
    grouped = defaultdict(list)
    for candidate in candidates:
        key = (
            mode,
            candidate.diagnostic.algorithm_version,
            candidate.fundamental_frequency,
            candidate.diagnostic.as_of,
        )
        grouped[key].append(candidate)

    selected: list[_Candidate] = []
    superseded = 0
    for key in sorted(grouped, key=lambda item: item[3]):
        revisions = grouped[key]
        latest_available = max(item.diagnostic.available_at for item in revisions)
        latest = [item for item in revisions if item.diagnostic.available_at == latest_available]
        identities = {_semantic_identity(item) for item in latest}
        if len(identities) != 1:
            raise AmbiguousStoredDiagnosticRevisionError(
                f"ambiguous {mode.value} diagnostic revisions at {key[3].isoformat()}"
            )
        selected.append(latest[0])
        superseded += len(revisions) - 1
    return selected, superseded


def _not_found_section(
    mode: DiagnosticMode,
    *,
    reason: str,
    examined: int,
    eligible: int,
    superseded: int = 0,
) -> ConsolidatedDiagnosticSection:
    return ConsolidatedDiagnosticSection(
        status=ConsolidatedSectionStatus.NOT_FOUND,
        mode=mode,
        not_found_reason=reason,
        revisions_superseded=superseded,
        candidates_examined=examined,
        candidates_eligible=eligible,
    )


def _choose_period(
    candidates: list[_Candidate],
    *,
    exact_date: date | None,
    mode: DiagnosticMode,
    request: ConsolidatedDiagnosticRequest,
    examined: int,
    eligible: int,
    superseded: int,
) -> ConsolidatedDiagnosticSection:
    if exact_date is None:
        chosen = max(candidates, key=lambda item: item.diagnostic.as_of)
    else:
        matches = [item for item in candidates if item.diagnostic.as_of.date() == exact_date]
        if not matches:
            return _not_found_section(
                mode,
                reason=f"no eligible {mode.value} diagnostic exists for {exact_date.isoformat()}",
                examined=examined,
                eligible=eligible,
                superseded=superseded,
            )
        if len(matches) != 1:
            raise AmbiguousStoredDiagnosticRevisionError(
                f"multiple selected {mode.value} diagnostics remain for {exact_date.isoformat()}"
            )
        chosen = matches[0]
    return ConsolidatedDiagnosticSection(
        status=ConsolidatedSectionStatus.AVAILABLE,
        mode=mode,
        diagnostic=chosen.diagnostic,
        selected_metric_result_ids=chosen.metric_ids,
        computed_after_known_at=chosen.diagnostic.computed_at > request.known_at,
        revisions_superseded=superseded,
        candidates_examined=examined,
        candidates_eligible=eligible,
    )


class AaplConsolidatedDiagnosticService:
    """Select independent current-version diagnostics without recomputation or persistence."""

    def __init__(self, storage: LocalStorage) -> None:
        storage.require_open()
        self._storage = storage

    def query(self, request: ConsolidatedDiagnosticRequest) -> ConsolidatedDiagnosticView:
        """Return one compact point-in-time view from two repository reads."""
        self._storage.require_open()
        diagnostics = tuple(self._storage.diagnostics.list(asset_id=request.asset_id))
        metric_results = tuple(self._storage.metric_results.list(asset_id=request.asset_id))
        metric_index = {item.result_id: item for item in metric_results}
        if len(metric_index) != len(metric_results):
            raise ConsolidatedDiagnosticTraceabilityError(
                "metric result identifiers are not unique"
            )

        ignored_versions = 0
        sections: dict[DiagnosticMode, ConsolidatedDiagnosticSection] = {}
        for mode, exact_date in (
            (DiagnosticMode.MARKET, request.market_as_of),
            (DiagnosticMode.FUNDAMENTAL, request.fundamental_as_of),
        ):
            mode_results = [item for item in diagnostics if item.mode is mode]
            current_version = _current_version(mode)
            current = []
            for diagnostic in mode_results:
                if diagnostic.algorithm_version != current_version:
                    ignored_versions += 1
                    continue
                if diagnostic.available_at > request.known_at:
                    continue
                if diagnostic.as_of > request.known_at:
                    continue
                candidate = _validate_diagnostic_common(
                    diagnostic,
                    request=request,
                    metric_index=metric_index,
                )
                if (
                    mode is DiagnosticMode.FUNDAMENTAL
                    and candidate.fundamental_frequency is not request.fundamental_frequency
                ):
                    continue
                current.append(candidate)

            if not current:
                sections[mode] = _not_found_section(
                    mode,
                    reason=(
                        f"no eligible current-version {mode.value} diagnostic was available "
                        "at known_at"
                    ),
                    examined=len(mode_results),
                    eligible=0,
                )
                continue
            selected_revisions, superseded = _select_revision(current, mode=mode)
            sections[mode] = _choose_period(
                selected_revisions,
                exact_date=exact_date,
                mode=mode,
                request=request,
                examined=len(mode_results),
                eligible=len(current),
                superseded=superseded,
            )

        market = sections[DiagnosticMode.MARKET]
        fundamental = sections[DiagnosticMode.FUNDAMENTAL]
        temporal_context = _temporal_context(market, fundamental)
        available_count = sum(
            section.status is ConsolidatedSectionStatus.AVAILABLE
            for section in (market, fundamental)
        )
        status = {
            0: ConsolidatedDiagnosticStatus.UNAVAILABLE,
            1: ConsolidatedDiagnosticStatus.PARTIAL,
            2: ConsolidatedDiagnosticStatus.COMPLETE,
        }[available_count]
        view = ConsolidatedDiagnosticView(
            request=request,
            status=status,
            market=market,
            fundamental=fundamental,
            temporal_context=temporal_context,
            diagnostics_examined=len(diagnostics),
            metric_results_examined=len(metric_results),
            ignored_algorithm_versions=ignored_versions,
            traceability_verified=True,
        )
        self._verify_view(view, metric_index)
        return view

    @staticmethod
    def _verify_view(
        view: ConsolidatedDiagnosticView,
        metric_index: dict[UUID, MetricResult],
    ) -> None:
        """Verify the final compact view without rerunning either diagnostic engine."""
        for section in (view.market, view.fundamental):
            if section.status is ConsolidatedSectionStatus.NOT_FOUND:
                continue
            diagnostic = section.diagnostic
            if diagnostic is None:
                raise ConsolidatedDiagnosticTraceabilityError(
                    "available section lost its diagnostic"
                )
            if diagnostic.available_at > view.request.known_at:
                raise ConsolidatedDiagnosticTraceabilityError(
                    "selected diagnostic was unavailable at known_at"
                )
            if any(
                identifier not in metric_index for identifier in section.selected_metric_result_ids
            ):
                raise ConsolidatedDiagnosticTraceabilityError(
                    "selected section references a missing metric result"
                )


def _temporal_context(
    market: ConsolidatedDiagnosticSection,
    fundamental: ConsolidatedDiagnosticSection,
) -> ConsolidatedTemporalContext:
    market_diagnostic = market.diagnostic
    fundamental_diagnostic = fundamental.diagnostic
    gap: int | None = None
    if market_diagnostic is not None and fundamental_diagnostic is not None:
        gap = abs((market_diagnostic.as_of.date() - fundamental_diagnostic.as_of.date()).days)
    return ConsolidatedTemporalContext(
        market_as_of=market_diagnostic.as_of if market_diagnostic else None,
        fundamental_as_of=(fundamental_diagnostic.as_of if fundamental_diagnostic else None),
        reference_gap_days=gap,
        market_available_at=(market_diagnostic.available_at if market_diagnostic else None),
        fundamental_available_at=(
            fundamental_diagnostic.available_at if fundamental_diagnostic else None
        ),
    )


__all__ = [
    "AaplConsolidatedDiagnosticService",
    "AmbiguousStoredDiagnosticRevisionError",
    "ConsolidatedDiagnosticQueryError",
    "ConsolidatedDiagnosticTraceabilityError",
    "FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION",
    "MalformedStoredDiagnosticError",
    "MissingReferencedMetricResultError",
    "MixedFundamentalFrequencyError",
]
