"""Pure Decimal-exact declared effective-close weight calculation."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Context, Decimal

from investment_analyst.core.models.enums import DataQuality
from investment_analyst.core.models.observation import NormalizedObservation

from .institutional_weight_definitions import WEIGHT_FIELDS
from .institutional_weight_models import InstitutionalWeightCandidate, InstitutionalWeightSkip


@dataclass(frozen=True, slots=True)
class InstitutionalWeightEngineResult:
    candidates: tuple[InstitutionalWeightCandidate, ...] = field(default_factory=tuple)
    skipped: tuple[InstitutionalWeightSkip, ...] = field(default_factory=tuple)


def calculate(
    *,
    asset_id: str,
    manager_cik: str,
    report_period: str,
    known_at: datetime,
    artifact_id: str | None,
    accession: str | None,
    status: str,
    total: Decimal | None,
    total_quality: DataQuality | None,
    observations: tuple[NormalizedObservation, ...],
    lineage: Mapping[str, str | None],
) -> InstitutionalWeightEngineResult:
    if status not in {"original_complete", "amended"}:
        return InstitutionalWeightEngineResult(
            skipped=tuple(
                InstitutionalWeightSkip(metric_key=k, reason="unresolved_close")
                for k, _ in WEIGHT_FIELDS
            )
        )
    if total is None:
        reason = "missing_total"
    elif total == 0:
        reason = "zero_total"
    else:
        reason = None
    if reason:
        return InstitutionalWeightEngineResult(
            skipped=tuple(
                InstitutionalWeightSkip(metric_key=k, reason=reason) for k, _ in WEIGHT_FIELDS
            )
        )
    candidates = []
    skipped = []
    for key, field_name in WEIGHT_FIELDS:
        matching = tuple(o for o in observations if o.field_name == field_name)
        if not matching:
            skipped.append(InstitutionalWeightSkip(metric_key=key, reason="missing_field"))
            continue
        if len(matching) != 1:
            skipped.append(InstitutionalWeightSkip(metric_key=key, reason="duplicate_position"))
            continue
        obs = matching[0]
        params = {
            "manager_cik": manager_cik,
            "report_period": report_period,
            "effective_artifact_id": artifact_id,
            "effective_accession": accession,
            "field_name": field_name,
            **lineage,
        }
        candidates.append(
            InstitutionalWeightCandidate(
                asset_id=asset_id,
                metric_key=key,
                value=Context(prec=28).divide(obs.value, total),
                available_at=obs.available_at,
                known_at=known_at,
                input_observation_id=obs.observation_id,
                parameters=params,
                quality=DataQuality.PARTIAL
                if DataQuality.PARTIAL in {obs.quality, total_quality}
                else DataQuality.VALID,
            )
        )
    return InstitutionalWeightEngineResult(tuple(candidates), tuple(skipped))
