"""Append-only persistence for evaluated corporate valuation metrics."""

from collections.abc import Collection
from typing import Protocol
from uuid import UUID

from pydantic import ConfigDict, Field

from investment_analyst.analytics.valuation.identity import valuation_result_parameters_v2
from investment_analyst.analytics.valuation.models import (
    CorporateValuationSnapshot,
    ValuationStatus,
)
from investment_analyst.core.interfaces.repositories import BatchWriteReceipt
from investment_analyst.core.models import (
    DataQuality,
    MetricCategory,
    MetricDefinition,
    MetricResult,
)
from investment_analyst.core.models.base import ContractModel


class ValuationPersistenceSummary(ContractModel):
    """Exact created/reused/unavailable counters for one snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    definitions_created: int = Field(ge=0)
    definitions_reused: int = Field(ge=0)
    metric_results_created: int = Field(ge=0)
    metric_results_reused: int = Field(ge=0)
    metrics_not_evaluable: int = Field(ge=0)
    metrics_not_applicable: int = Field(ge=0)


class _DefinitionRepository(Protocol):
    def list_all(self) -> list[MetricDefinition]: ...

    def upsert(self, definition: MetricDefinition) -> MetricDefinition: ...


class _ResultRepository(Protocol):
    def get_existing(self, result_ids: Collection[UUID]) -> dict[UUID, MetricResult]: ...

    def save_many(self, results: Collection[MetricResult]) -> BatchWriteReceipt: ...


class _Storage(Protocol):
    metric_definitions: _DefinitionRepository
    metric_results: _ResultRepository

    def require_open(self) -> None: ...


class CorporateValuationPersistencePipeline:
    """Persist definitions and evaluated values; unavailable states remain projections."""

    def __init__(self, storage: _Storage) -> None:
        storage.require_open()
        self._storage = storage

    def persist(self, snapshot: CorporateValuationSnapshot) -> ValuationPersistenceSummary:
        """Save deterministic results without rolling back already persisted progress."""
        self._storage.require_open()
        existing_definitions = {
            item.metric_key: item for item in self._storage.metric_definitions.list_all()
        }
        definitions_created = definitions_reused = 0
        for definition in snapshot.definitions:
            core_definition = MetricDefinition(
                metric_key=definition.metric_key,
                display_name=definition.display_name_es,
                category=MetricCategory.VALUATION,
                description="Métrica descriptiva de valoración corporativa point-in-time.",
                formula=definition.formula,
                unit=definition.unit,
                default_parameters={"basis": snapshot.request.basis},
                limitations=list(definition.limitations),
                references=["docs/corporate_valuation_point_in_time.md"],
                definition_version=definition.definition_version,
            )
            existing = existing_definitions.get(definition.metric_key)
            if existing is None:
                self._storage.metric_definitions.upsert(core_definition)
                definitions_created += 1
            elif existing != core_definition:
                raise ValueError(
                    "stored valuation definition conflicts with the published contract"
                )
            else:
                definitions_reused += 1

        candidate_ids = {
            metric.result_id
            for metric in snapshot.metrics
            if metric.status is ValuationStatus.EVALUATED and metric.result_id is not None
        }
        existing_results = self._storage.metric_results.get_existing(candidate_ids)
        created = reused = 0
        definitions = {item.metric_key: item for item in snapshot.definitions}
        to_save: list[MetricResult] = []
        for metric in snapshot.metrics:
            if metric.status is not ValuationStatus.EVALUATED or metric.value is None:
                continue
            if (
                metric.result_id is None
                or metric.available_at is None
                or snapshot.valuation_as_of is None
                or snapshot.annual_period_end is None
                or snapshot.security_basis is None
            ):
                raise ValueError("evaluated valuation metric lacks persistence metadata")
            definition = definitions[metric.metric_key]
            if metric.result_id.version == 8 and snapshot.security_basis is not None:
                parameters = valuation_result_parameters_v2(
                    request=snapshot.request,
                    annual_period_start=snapshot.annual_period_start,
                    annual_period_end=snapshot.annual_period_end,
                    security_basis_version=snapshot.security_basis.contract_version,
                    formula=definition.formula,
                    market_units_per_reported_share=(
                        snapshot.security_basis.market_units_per_reported_share
                    ),
                )
            else:
                parameters = {
                    "category": MetricCategory.VALUATION.value,
                    "basis": snapshot.request.basis,
                    "known_at": snapshot.known_at.isoformat(),
                    "valuation_date": snapshot.request.valuation_date.isoformat(),
                    "annual_period_start": (
                        snapshot.annual_period_start.isoformat()
                        if snapshot.annual_period_start is not None
                        else None
                    ),
                    "annual_period_end": snapshot.annual_period_end.isoformat(),
                    "formula": definition.formula,
                    "security_basis_version": snapshot.security_basis.contract_version,
                    "market_units_per_reported_share": str(
                        snapshot.security_basis.market_units_per_reported_share
                    ),
                }
            result = MetricResult(
                result_id=metric.result_id,
                asset_id=snapshot.asset_id,
                metric_key=metric.metric_key,
                value=metric.value,
                unit=definition.unit,
                as_of=snapshot.valuation_as_of,
                available_at=metric.available_at,
                computed_at=snapshot.computed_at,
                parameters=parameters,
                input_observation_ids=list(metric.input_observation_ids),
                algorithm_version=definition.algorithm_version,
                quality=DataQuality.VALID,
            )
            existing = existing_results.get(result.result_id)
            if existing is not None:
                if existing.model_dump(exclude={"computed_at"}) != result.model_dump(
                    exclude={"computed_at"}
                ):
                    raise ValueError(
                        "stored valuation result conflicts with deterministic identity"
                    )
                reused += 1
                continue
            to_save.append(result)
            existing_results[result.result_id] = result
            created += 1
        for result in to_save:
            self._storage.metric_results.save_many([result])
        return ValuationPersistenceSummary(
            definitions_created=definitions_created,
            definitions_reused=definitions_reused,
            metric_results_created=created,
            metric_results_reused=reused,
            metrics_not_evaluable=snapshot.coverage.not_evaluable,
            metrics_not_applicable=snapshot.coverage.not_applicable,
        )


__all__ = [
    "CorporateValuationPersistencePipeline",
    "ValuationPersistenceSummary",
]
