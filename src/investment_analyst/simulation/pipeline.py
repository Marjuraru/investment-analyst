"""Deterministic end-to-end simulated analysis pipeline."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid5

from investment_analyst.core.models import (
    Asset,
    DataFrequency,
    DataQuality,
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticMode,
    DiagnosticResult,
    EvidenceDirection,
    MetricCategory,
    MetricDefinition,
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceDefinition,
    SourceReference,
)
from investment_analyst.simulation.fixtures import (
    SIMULATED_SOURCE_ID,
    SimulatedBar,
    simulated_assets,
    simulated_bars,
    simulated_source,
)
from investment_analyst.simulation.scoring import (
    RETURN_WEIGHT,
    VOLUME_WEIGHT,
    final_score,
    score_return,
    score_volume,
    verdict_for_score,
)
from investment_analyst.storage import LocalStorage

_UUID_NAMESPACE = UUID("4f63f570-7584-5fe9-9df7-347a271db13f")
_OBSERVATION_FIELDS = ("open", "high", "low", "close", "volume", "trade_count")
_EXPECTED_COUNTS = {
    "assets": 2,
    "sources": 1,
    "raw_records": 6,
    "observations": 36,
    "metric_definitions": 2,
    "metric_results": 4,
    "diagnostics": 2,
}


@dataclass(frozen=True, slots=True)
class SimulationCounts:
    """Immutable expected counts for one simulation run."""

    assets: int
    sources: int
    raw_records: int
    observations: int
    metric_definitions: int
    metric_results: int
    diagnostics: int

    def __getitem__(self, key: str) -> int:
        """Allow concise count lookup by the documented type name."""
        values = self.to_dict()
        try:
            return values[key]
        except KeyError as error:
            raise KeyError(f"unknown simulation count: {key}") from error

    def to_dict(self) -> dict[str, int]:
        """Return a JSON-compatible count mapping."""
        return {
            "assets": self.assets,
            "sources": self.sources,
            "raw_records": self.raw_records,
            "observations": self.observations,
            "metric_definitions": self.metric_definitions,
            "metric_results": self.metric_results,
            "diagnostics": self.diagnostics,
        }


@dataclass(frozen=True, slots=True)
class SimulationRunSummary:
    """Immutable, JSON-compatible audit summary for a completed run."""

    asset_ids: tuple[str, ...]
    source_id: str
    raw_record_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    metric_result_ids: tuple[str, ...]
    diagnostic_ids: tuple[str, ...]
    counts: SimulationCounts
    traceability_verified: bool

    def to_dict(self) -> dict[str, object]:
        """Return a stable JSON-compatible representation."""
        return {
            "asset_ids": list(self.asset_ids),
            "source_id": self.source_id,
            "raw_record_ids": list(self.raw_record_ids),
            "observation_ids": list(self.observation_ids),
            "metric_result_ids": list(self.metric_result_ids),
            "diagnostic_ids": list(self.diagnostic_ids),
            "counts": self.counts.to_dict(),
            "traceability_verified": self.traceability_verified,
        }


class SimulatedPipeline:
    """Build, store, retrieve, and audit one deterministic simulated run."""

    def __init__(self, storage: LocalStorage) -> None:
        storage.require_open()
        self._storage = storage

    def run(self) -> SimulationRunSummary:
        """Execute the complete idempotent simulated route."""
        assets = simulated_assets()
        source = simulated_source()
        bars = simulated_bars()
        definitions = _metric_definitions()

        for asset in assets:
            self._storage.assets.upsert(asset)
        self._storage.sources.upsert(source)

        raw_records = tuple(_raw_record(bar) for bar in bars)
        for record in raw_records:
            self._storage.raw_records.save(record)

        observations = tuple(
            observation
            for record, bar in zip(raw_records, bars, strict=True)
            for observation in _observations(record, bar)
        )
        for observation in observations:
            self._storage.observations.save(observation)

        for definition in definitions:
            self._storage.metric_definitions.upsert(definition)

        metric_results = tuple(
            result for asset in assets for result in _metric_results_for_asset(asset, observations)
        )
        for result in metric_results:
            self._storage.metric_results.save(result)

        diagnostics = tuple(_diagnostic_for_asset(asset, metric_results) for asset in assets)
        for diagnostic in diagnostics:
            self._storage.diagnostics.save(diagnostic)

        stored_assets = tuple(self._storage.assets.get(asset.asset_id) for asset in assets)
        stored_source = self._storage.sources.get(source.source_id)
        stored_raw_records = tuple(
            self._storage.raw_records.get(record.record_id) for record in raw_records
        )
        stored_observations = tuple(
            self._storage.observations.get(observation.observation_id)
            for observation in observations
        )
        stored_definitions = tuple(
            self._storage.metric_definitions.get(definition.metric_key)
            for definition in definitions
        )
        stored_metrics = tuple(
            self._storage.metric_results.get(result.result_id) for result in metric_results
        )
        stored_diagnostics = tuple(
            self._storage.diagnostics.get(result.diagnostic_id) for result in diagnostics
        )

        _verify_traceability(
            stored_assets,
            stored_source,
            stored_raw_records,
            stored_observations,
            stored_definitions,
            stored_metrics,
            stored_diagnostics,
        )
        counts = SimulationCounts(**_EXPECTED_COUNTS)
        return SimulationRunSummary(
            asset_ids=tuple(asset.asset_id for asset in stored_assets),
            source_id=stored_source.source_id,
            raw_record_ids=tuple(str(record.record_id) for record in stored_raw_records),
            observation_ids=tuple(
                str(observation.observation_id) for observation in stored_observations
            ),
            metric_result_ids=tuple(str(result.result_id) for result in stored_metrics),
            diagnostic_ids=tuple(
                str(diagnostic.diagnostic_id) for diagnostic in stored_diagnostics
            ),
            counts=counts,
            traceability_verified=True,
        )


def _stable_uuid(*parts: object) -> UUID:
    name = "|".join(str(part) for part in parts)
    return uuid5(_UUID_NAMESPACE, name)


def _raw_record(bar: SimulatedBar) -> RawRecord:
    available_at = bar.timestamp + timedelta(minutes=1)
    received_at = bar.timestamp + timedelta(minutes=2)
    record_id = _stable_uuid(SIMULATED_SOURCE_ID, bar.asset_id, bar.timestamp.isoformat())
    payload = {
        "provider_symbol": bar.provider_symbol,
        "timestamp": bar.timestamp.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
        "trade_count": str(bar.trade_count),
    }
    return RawRecord(
        record_id=record_id,
        asset_id=bar.asset_id,
        source=SourceReference(
            source_id=SIMULATED_SOURCE_ID,
            record_key=f"{bar.provider_symbol}:{bar.timestamp.isoformat()}",
            retrieved_at=received_at,
        ),
        event_time=bar.timestamp,
        available_at=available_at,
        received_at=received_at,
        payload=payload,
        schema_version="simulated-bars-v1",
    )


def _observations(record: RawRecord, bar: SimulatedBar) -> tuple[NormalizedObservation, ...]:
    normalized_at = record.received_at + timedelta(minutes=1)
    units = {
        "open": "USD",
        "high": "USD",
        "low": "USD",
        "close": "USD",
        "volume": "units",
        "trade_count": "count",
    }
    return tuple(
        NormalizedObservation(
            observation_id=_stable_uuid(record.record_id, field_name),
            raw_record_id=record.record_id,
            asset_id=bar.asset_id,
            field_name=field_name,
            value=getattr(bar, field_name),
            unit=units[field_name],
            frequency=DataFrequency.DAY_1,
            observed_at=bar.timestamp,
            available_at=record.available_at,
            normalized_at=normalized_at,
            source=SourceReference(
                source_id=SIMULATED_SOURCE_ID,
                record_key=str(record.record_id),
                retrieved_at=record.received_at,
            ),
            quality=DataQuality.VALID,
            transformation_version="simulated-normalizer-v1",
        )
        for field_name in _OBSERVATION_FIELDS
    )


def _metric_definitions() -> tuple[MetricDefinition, MetricDefinition]:
    return (
        MetricDefinition(
            metric_key="market.simple_return_1d",
            display_name="Simulated One-Day Simple Return",
            category=MetricCategory.MARKET,
            description="Demonstration return calculated from two simulated daily closes.",
            formula="(current_close / previous_close) - 1",
            unit="ratio",
            default_parameters={},
            limitations=["Uses fictitious data and is not investment analysis."],
            references=["Simulated pipeline technical specification."],
            definition_version="simulated-metrics-v1",
        ),
        MetricDefinition(
            metric_key="market.volume_ratio_1d",
            display_name="Simulated One-Day Volume Ratio",
            category=MetricCategory.MARKET,
            description="Demonstration ratio calculated from two simulated daily volumes.",
            formula="current_volume / previous_volume",
            unit="ratio",
            default_parameters={},
            limitations=["Uses fictitious data and is not investment analysis."],
            references=["Simulated pipeline technical specification."],
            definition_version="simulated-metrics-v1",
        ),
    )


def _metric_results_for_asset(
    asset: Asset,
    observations: tuple[NormalizedObservation, ...],
) -> tuple[MetricResult, MetricResult]:
    closes = _latest_two_observations(observations, asset.asset_id, "close")
    volumes = _latest_two_observations(observations, asset.asset_id, "volume")
    simple_return = closes[1].value / closes[0].value - Decimal("1")
    volume_ratio = volumes[1].value / volumes[0].value
    return (
        _metric_result(asset.asset_id, "market.simple_return_1d", simple_return, closes),
        _metric_result(asset.asset_id, "market.volume_ratio_1d", volume_ratio, volumes),
    )


def _latest_two_observations(
    observations: tuple[NormalizedObservation, ...],
    asset_id: str,
    field_name: str,
) -> tuple[NormalizedObservation, NormalizedObservation]:
    matches = sorted(
        (
            observation
            for observation in observations
            if observation.asset_id == asset_id and observation.field_name == field_name
        ),
        key=lambda observation: observation.observed_at,
    )
    if len(matches) != 3:
        raise RuntimeError(f"expected three {field_name} observations for {asset_id}")
    return matches[-2], matches[-1]


def _metric_result(
    asset_id: str,
    metric_key: str,
    value: Decimal,
    inputs: tuple[NormalizedObservation, NormalizedObservation],
) -> MetricResult:
    as_of = inputs[-1].observed_at
    if as_of is None:
        raise RuntimeError("metric inputs require observed_at")
    available_at = max(observation.available_at for observation in inputs)
    computed_at = max(observation.normalized_at for observation in inputs) + timedelta(minutes=1)
    return MetricResult(
        result_id=_stable_uuid(asset_id, metric_key, as_of.isoformat()),
        asset_id=asset_id,
        metric_key=metric_key,
        value=value,
        unit="ratio",
        as_of=as_of,
        available_at=available_at,
        computed_at=computed_at,
        parameters={},
        input_observation_ids=[observation.observation_id for observation in inputs],
        algorithm_version="simulated-metrics-v1",
        quality=DataQuality.VALID,
    )


def _diagnostic_for_asset(
    asset: Asset,
    metric_results: tuple[MetricResult, ...],
) -> DiagnosticResult:
    by_key = {
        result.metric_key: result for result in metric_results if result.asset_id == asset.asset_id
    }
    return_result = by_key["market.simple_return_1d"]
    volume_result = by_key["market.volume_ratio_1d"]
    return_component_score = score_return(return_result.value)
    volume_component_score = score_volume(volume_result.value)
    combined_score = final_score(return_component_score, volume_component_score)
    return_contribution = return_component_score * RETURN_WEIGHT
    volume_contribution = volume_component_score * VOLUME_WEIGHT
    computed_at = max(return_result.computed_at, volume_result.computed_at) + timedelta(minutes=1)
    available_at = max(return_result.available_at, volume_result.available_at)
    as_of = max(return_result.as_of, volume_result.as_of)
    components = [
        DiagnosticComponent(
            component_key="price_return",
            score=return_component_score,
            weight=RETURN_WEIGHT,
            weighted_contribution=return_contribution,
            metric_result_ids=[return_result.result_id],
            explanation="Explicit score from the simulated one-day simple return.",
        ),
        DiagnosticComponent(
            component_key="volume_activity",
            score=volume_component_score,
            weight=VOLUME_WEIGHT,
            weighted_contribution=volume_contribution,
            metric_result_ids=[volume_result.result_id],
            explanation="Explicit score from the simulated one-day volume ratio.",
        ),
    ]
    evidence = [
        DiagnosticEvidence(
            metric_result_id=return_result.result_id,
            direction=_evidence_direction(return_component_score),
            contribution=return_contribution,
            reason="Simulated return score contribution under the documented rule.",
        ),
        DiagnosticEvidence(
            metric_result_id=volume_result.result_id,
            direction=_evidence_direction(volume_component_score),
            contribution=volume_contribution,
            reason="Simulated volume score contribution under the documented rule.",
        ),
    ]
    return DiagnosticResult(
        diagnostic_id=_stable_uuid(asset.asset_id, DiagnosticMode.MARKET.value, as_of.isoformat()),
        asset_id=asset.asset_id,
        mode=DiagnosticMode.MARKET,
        verdict=verdict_for_score(combined_score),
        final_score=combined_score,
        confidence=Decimal("1"),
        as_of=as_of,
        available_at=available_at,
        computed_at=computed_at,
        components=components,
        evidence=evidence,
        algorithm_version="simulated-diagnostic-v1",
        summary=(
            "Technical simulation using fictitious data; this result is not an investment "
            "recommendation or real financial analysis."
        ),
        quality=DataQuality.VALID,
    )


def _evidence_direction(score: Decimal) -> EvidenceDirection:
    if score > Decimal("50"):
        return EvidenceDirection.SUPPORTS
    if score < Decimal("50"):
        return EvidenceDirection.OPPOSES
    return EvidenceDirection.NEUTRAL


def _verify_traceability(
    assets: tuple[Asset, ...],
    source: SourceDefinition,
    raw_records: tuple[RawRecord, ...],
    observations: tuple[NormalizedObservation, ...],
    definitions: tuple[MetricDefinition, ...],
    metric_results: tuple[MetricResult, ...],
    diagnostics: tuple[DiagnosticResult, ...],
) -> None:
    actual_counts = {
        "assets": len(assets),
        "sources": 1,
        "raw_records": len(raw_records),
        "observations": len(observations),
        "metric_definitions": len(definitions),
        "metric_results": len(metric_results),
        "diagnostics": len(diagnostics),
    }
    if actual_counts != _EXPECTED_COUNTS:
        raise RuntimeError(
            f"simulation count mismatch: expected {_EXPECTED_COUNTS}, got {actual_counts}"
        )
    if source.source_id != SIMULATED_SOURCE_ID or source.is_official:
        raise RuntimeError("simulation source is not the expected fictitious source")

    asset_ids = {asset.asset_id for asset in assets}
    raw_by_id = {record.record_id: record for record in raw_records}
    observation_by_id = {observation.observation_id: observation for observation in observations}
    metric_by_id = {result.result_id: result for result in metric_results}

    for record in raw_records:
        if record.asset_id not in asset_ids or record.source.source_id != source.source_id:
            raise RuntimeError(f"raw record {record.record_id} has inconsistent ownership")
        if not (record.event_time <= record.available_at <= record.received_at):
            raise RuntimeError(f"raw record {record.record_id} has invalid temporal order")

    for observation in observations:
        raw_record = raw_by_id.get(observation.raw_record_id)
        if raw_record is None:
            raise RuntimeError(f"observation {observation.observation_id} has no raw record")
        if observation.asset_id != raw_record.asset_id:
            raise RuntimeError(f"observation {observation.observation_id} has a mismatched asset")
        if observation.available_at != raw_record.available_at:
            raise RuntimeError(f"observation {observation.observation_id} changes availability")
        if observation.normalized_at < raw_record.received_at:
            raise RuntimeError(f"observation {observation.observation_id} predates receipt")

    for result in metric_results:
        for observation_id in result.input_observation_ids:
            observation = observation_by_id.get(observation_id)
            if observation is None:
                raise RuntimeError(f"metric {result.result_id} has no input observation")
            if observation.asset_id != result.asset_id:
                raise RuntimeError(f"metric {result.result_id} mixes asset observations")
            if observation.available_at > result.computed_at:
                raise RuntimeError(f"metric {result.result_id} uses future information")
            if observation.normalized_at > result.computed_at:
                raise RuntimeError(f"metric {result.result_id} predates normalization")

    for diagnostic in diagnostics:
        referenced_ids = {
            metric_id
            for component in diagnostic.components
            for metric_id in component.metric_result_ids
        }
        referenced_ids.update(evidence.metric_result_id for evidence in diagnostic.evidence)
        for metric_id in referenced_ids:
            metric = metric_by_id.get(metric_id)
            if metric is None:
                raise RuntimeError(f"diagnostic {diagnostic.diagnostic_id} has no metric")
            if metric.asset_id != diagnostic.asset_id:
                raise RuntimeError(f"diagnostic {diagnostic.diagnostic_id} mixes assets")
            if metric.computed_at > diagnostic.computed_at:
                raise RuntimeError(f"diagnostic {diagnostic.diagnostic_id} uses a future metric")
