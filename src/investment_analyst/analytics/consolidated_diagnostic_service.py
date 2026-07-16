"""Read-only point-in-time service for separate Apple diagnostic modes."""

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

from pydantic import JsonValue

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
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.core.models.diagnostic import DECIMAL_TOLERANCE
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SEC_FUNDAMENTAL_METRIC_DEFINITIONS,
)
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.errors import RecordNotFoundError

FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION = "sec-aapl-fundamental-diagnostic-v1.1-decimal34"

_MARKET_METRIC_KEYS = frozenset({SIMPLE_RETURN_KEY, SMA_KEY, VOLATILITY_KEY, RELATIVE_VOLUME_KEY})
_FUNDAMENTAL_DEFINITION_BY_KEY = {
    definition.metric_name: definition for definition in SEC_FUNDAMENTAL_METRIC_DEFINITIONS
}
_FUNDAMENTAL_METRIC_KEYS = frozenset(_FUNDAMENTAL_DEFINITION_BY_KEY)
_ALLOWED_FUNDAMENTAL_FREQUENCIES = frozenset({DataFrequency.ANNUAL, DataFrequency.QUARTERLY})
_POINT_IN_TIME_CUTOFF_PARAMETER = "known_at"


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


def _canonical_json(value: JsonValue) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    )


def _semantic_digest(value: JsonValue) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _source_identity(source: SourceReference) -> dict[str, JsonValue]:
    return {
        "source_id": source.source_id,
        "record_key": source.record_key,
        "raw_uri": source.raw_uri,
        "checksum_sha256": source.checksum_sha256,
    }


def _canonical_parameter_value(
    value: JsonValue,
    observation_identities: dict[UUID, str],
) -> JsonValue:
    if isinstance(value, str):
        try:
            identifier = UUID(value)
        except ValueError:
            return value
        identity = observation_identities.get(identifier)
        if identity is None:
            return value
        return {"semantic_observation": identity}
    if isinstance(value, list):
        canonical = [_canonical_parameter_value(item, observation_identities) for item in value]
        if canonical and all(
            isinstance(item, dict) and isinstance(item.get("role"), str) for item in canonical
        ):
            return sorted(canonical, key=_canonical_json)
        return canonical
    if isinstance(value, dict):
        return {
            key: _canonical_parameter_value(item, observation_identities)
            for key, item in sorted(value.items())
        }
    return value


def _parse_utc_parameter(value: JsonValue) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if _is_utc(parsed) else None


class _SemanticTraceabilityResolver:
    """Resolve compact semantic identities lazily for tied diagnostic revisions."""

    def __init__(
        self,
        storage: LocalStorage,
        metric_index: dict[UUID, MetricResult],
        *,
        asset_id: str,
    ) -> None:
        self._storage = storage
        self._metric_index = metric_index
        self._asset_id = asset_id
        self._metric_identities: dict[UUID, str] = {}
        self._observations: dict[UUID, NormalizedObservation] = {}
        self._observation_identities: dict[UUID, str] = {}
        self._raw_records: dict[UUID, RawRecord] = {}
        self._raw_identities: dict[UUID, str] = {}

    def metric_identity(self, identifier: UUID) -> str:
        cached = self._metric_identities.get(identifier)
        if cached is not None:
            return cached
        try:
            result = self._metric_index[identifier]
        except KeyError as error:
            raise MissingReferencedMetricResultError(
                f"diagnostic references missing metric result {identifier}"
            ) from error
        _validate_metric_common(result, self._asset_id)

        observations = {
            observation_id: self._observation(observation_id)
            for observation_id in result.input_observation_ids
        }
        observation_identities = {
            observation_id: self._observation_identity(observation_id)
            for observation_id in result.input_observation_ids
        }
        parameters: dict[str, JsonValue] = {}
        for key, value in sorted(result.parameters.items()):
            if key == _POINT_IN_TIME_CUTOFF_PARAMETER:
                cutoff = _parse_utc_parameter(value)
                if cutoff is not None:
                    if any(item.available_at > cutoff for item in observations.values()):
                        raise ConsolidatedDiagnosticTraceabilityError(
                            f"metric result {result.result_id} uses an input after known_at"
                        )
                    parameters[key] = {"effective_inputs": sorted(observation_identities.values())}
                    continue
            parameters[key] = _canonical_parameter_value(value, observation_identities)
        document: dict[str, JsonValue] = {
            "asset_id": result.asset_id,
            "metric_key": result.metric_key,
            "value": str(result.value),
            "unit": result.unit,
            "as_of": result.as_of.isoformat(),
            "available_at": result.available_at.isoformat(),
            "parameters": parameters,
            "input_observations": sorted(
                observation_identities[observation_id]
                for observation_id in result.input_observation_ids
            ),
            "algorithm_version": result.algorithm_version,
            "quality": result.quality.value,
        }
        identity = _semantic_digest(document)
        self._metric_identities[identifier] = identity
        return identity

    def _observation_identity(self, identifier: UUID) -> str:
        cached = self._observation_identities.get(identifier)
        if cached is not None:
            return cached
        observation = self._observation(identifier)
        raw_record = self._raw_record(observation.raw_record_id)
        if raw_record.source.source_id != observation.source.source_id:
            raise ConsolidatedDiagnosticTraceabilityError(
                f"observation {identifier} source does not match its raw record"
            )
        document: dict[str, JsonValue] = {
            "raw_record": self._raw_identity(raw_record.record_id),
            "asset_id": observation.asset_id,
            "field_name": observation.field_name,
            "value": str(observation.value),
            "unit": observation.unit,
            "frequency": observation.frequency.value,
            "observed_at": (
                observation.observed_at.isoformat() if observation.observed_at else None
            ),
            "period_start": (
                observation.period_start.isoformat() if observation.period_start else None
            ),
            "period_end": observation.period_end.isoformat() if observation.period_end else None,
            "available_at": observation.available_at.isoformat(),
            "source": _source_identity(observation.source),
            "quality": observation.quality.value,
            "transformation_version": observation.transformation_version,
        }
        identity = _semantic_digest(document)
        self._observation_identities[identifier] = identity
        return identity

    def _observation(self, identifier: UUID) -> NormalizedObservation:
        cached = self._observations.get(identifier)
        if cached is not None:
            return cached
        try:
            observation = self._storage.observations.get(identifier)
        except RecordNotFoundError as error:
            raise ConsolidatedDiagnosticTraceabilityError(
                f"metric references missing observation {identifier}"
            ) from error
        self._validate_observation(observation)
        self._observations[identifier] = observation
        return observation

    def _raw_identity(self, identifier: UUID) -> str:
        cached = self._raw_identities.get(identifier)
        if cached is not None:
            return cached
        record = self._raw_record(identifier)
        document: dict[str, JsonValue] = {
            "asset_id": record.asset_id,
            "source": _source_identity(record.source),
            "event_time": record.event_time.isoformat() if record.event_time else None,
            "available_at": record.available_at.isoformat(),
            "payload": record.payload,
            "schema_version": record.schema_version,
        }
        identity = _semantic_digest(document)
        self._raw_identities[identifier] = identity
        return identity

    def _raw_record(self, identifier: UUID) -> RawRecord:
        cached = self._raw_records.get(identifier)
        if cached is not None:
            return cached
        try:
            record = self._storage.raw_records.get(identifier)
        except RecordNotFoundError as error:
            raise ConsolidatedDiagnosticTraceabilityError(
                f"observation references missing raw record {identifier}"
            ) from error
        self._validate_raw_record(record)
        self._raw_records[identifier] = record
        return record

    def _validate_observation(self, observation: NormalizedObservation) -> None:
        if observation.asset_id != self._asset_id:
            raise ConsolidatedDiagnosticTraceabilityError(
                f"observation {observation.observation_id} belongs to another asset"
            )
        for name, value in (
            ("observation observed_at", observation.observed_at),
            ("observation period_start", observation.period_start),
            ("observation period_end", observation.period_end),
        ):
            if value is not None:
                _require_utc(value, name)
        _require_utc(observation.available_at, "observation available_at")
        _require_utc(observation.normalized_at, "observation normalized_at")
        _require_utc(observation.source.retrieved_at, "observation source retrieved_at")

    def _validate_raw_record(self, record: RawRecord) -> None:
        if record.asset_id is not None and record.asset_id != self._asset_id:
            raise ConsolidatedDiagnosticTraceabilityError(
                f"raw record {record.record_id} belongs to another asset"
            )
        if record.event_time is not None:
            _require_utc(record.event_time, "raw record event_time")
        _require_utc(record.available_at, "raw record available_at")
        _require_utc(record.received_at, "raw record received_at")
        _require_utc(record.source.retrieved_at, "raw record source retrieved_at")


def _semantic_identity(
    candidate: _Candidate,
    resolver: _SemanticTraceabilityResolver,
) -> str:
    diagnostic = candidate.diagnostic
    components: list[JsonValue] = []
    for component in diagnostic.components:
        document: dict[str, JsonValue] = {
            "component_key": component.component_key,
            "score": str(component.score),
            "weight": str(component.weight),
            "weighted_contribution": str(component.weighted_contribution),
            "metric_results": sorted(
                resolver.metric_identity(identifier) for identifier in component.metric_result_ids
            ),
            "explanation": component.explanation,
        }
        components.append(document)
    evidence: list[JsonValue] = []
    for item in diagnostic.evidence:
        document = {
            "metric_result": resolver.metric_identity(item.metric_result_id),
            "direction": item.direction.value,
            "contribution": str(item.contribution),
            "reason": item.reason,
        }
        evidence.append(document)
    payload: dict[str, JsonValue] = {
        "asset_id": diagnostic.asset_id,
        "mode": diagnostic.mode.value,
        "verdict": diagnostic.verdict.value,
        "final_score": str(diagnostic.final_score),
        "confidence": str(diagnostic.confidence),
        "as_of": diagnostic.as_of.isoformat(),
        "available_at": diagnostic.available_at.isoformat(),
        "components": sorted(components, key=_canonical_json),
        "evidence": sorted(evidence, key=_canonical_json),
        "algorithm_version": diagnostic.algorithm_version,
        "summary": diagnostic.summary,
        "quality": diagnostic.quality.value,
        "fundamental_frequency": (
            candidate.fundamental_frequency.value if candidate.fundamental_frequency else None
        ),
    }
    return _semantic_digest(payload)


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
    storage: LocalStorage,
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
        except KeyError:
            try:
                metric = storage.metric_results.get(identifier)
            except RecordNotFoundError as not_found:
                raise MissingReferencedMetricResultError(
                    f"diagnostic references missing metric result {identifier}"
                ) from not_found
            metric_index[identifier] = metric
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
    resolver: _SemanticTraceabilityResolver,
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
        if len(latest) > 1:
            identities = {_semantic_identity(item, resolver) for item in latest}
            if len(identities) != 1:
                raise AmbiguousStoredDiagnosticRevisionError(
                    f"ambiguous {mode.value} diagnostic revisions at {key[3].isoformat()}"
                )
        selected.append(
            min(
                latest,
                key=lambda item: (
                    item.diagnostic.computed_at,
                    str(item.diagnostic.diagnostic_id),
                ),
            )
        )
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
        semantic_resolver = _SemanticTraceabilityResolver(
            self._storage,
            metric_index,
            asset_id=request.asset_id,
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
                    storage=self._storage,
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
            selected_revisions, superseded = _select_revision(
                current,
                mode=mode,
                resolver=semantic_resolver,
            )
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
