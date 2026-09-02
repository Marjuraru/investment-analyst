"""Pure fail-closed calculations over adjacent resolved institutional closes."""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Context

from investment_analyst.analytics.cazatiburones.institutional_metric_definitions import (
    METRIC_FIELDS,
)
from investment_analyst.analytics.cazatiburones.institutional_metric_models import (
    InstitutionalMetricCandidate,
    InstitutionalMetricClose,
    InstitutionalMetricSkip,
)
from investment_analyst.core.models.enums import DataQuality
from investment_analyst.core.models.observation import NormalizedObservation


@dataclass(frozen=True, slots=True)
class InstitutionalMetricEngineResult:
    candidates: tuple[InstitutionalMetricCandidate, ...] = field(default_factory=tuple)
    skipped: tuple[InstitutionalMetricSkip, ...] = field(default_factory=tuple)


def calculate(
    *,
    asset_id: str,
    manager_cik: str,
    known_at: datetime,
    closes: tuple[InstitutionalMetricClose, ...],
) -> InstitutionalMetricEngineResult:
    candidates: list[InstitutionalMetricCandidate] = []
    skipped: list[InstitutionalMetricSkip] = []
    for prior, current in zip(closes, closes[1:], strict=False):
        if prior.status not in {"original_complete", "amended"} or current.status not in {
            "original_complete",
            "amended",
        }:
            skipped.extend(
                InstitutionalMetricSkip(metric_key=key, reason="unresolved_close")
                for key, *_ in METRIC_FIELDS
            )
            continue
        prior_positions = _positions(prior.observations)
        current_positions = _positions(current.observations)
        for position in sorted(set(prior_positions) | set(current_positions)):
            prior_rows = prior_positions.get(position, ())
            current_rows = current_positions.get(position, ())
            if len(prior_rows) != 1 or len(current_rows) != 1:
                reason = (
                    "missing_position"
                    if not prior_rows or not current_rows
                    else "duplicate_position"
                )
                skipped.extend(
                    InstitutionalMetricSkip(metric_key=key, reason=reason)
                    for key, *_ in METRIC_FIELDS
                )
                continue
            prior_fields, current_fields = prior_rows[0], current_rows[0]
            for key, field_name, unit, ratio in METRIC_FIELDS:
                left = prior_fields.get(field_name)
                right = current_fields.get(field_name)
                if left is None or right is None:
                    skipped.append(InstitutionalMetricSkip(metric_key=key, reason="missing_field"))
                    continue
                if ratio and left.value == 0:
                    skipped.append(InstitutionalMetricSkip(metric_key=key, reason="zero_prior"))
                    continue
                value = (
                    Context(prec=28).divide(right.value - left.value, left.value)
                    if ratio
                    else right.value - left.value
                )
                candidates.append(
                    InstitutionalMetricCandidate(
                        asset_id=asset_id,
                        metric_key=key,
                        value=value,
                        unit=unit,
                        as_of=right.period_end,
                        available_at=max(left.available_at, right.available_at),
                        known_at=known_at,
                        parameters={
                            "manager_cik": manager_cik,
                            "cusip": position[0],
                            "title_of_class": position[1],
                            "put_call": position[2],
                            "prior_report_period": prior.report_period.isoformat(),
                            "report_period": current.report_period.isoformat(),
                        },
                        input_observation_ids=(left.observation_id, right.observation_id),
                        quality=_quality(left.quality, right.quality),
                    )
                )
    return InstitutionalMetricEngineResult(tuple(candidates), tuple(skipped))


def _positions(
    observations: tuple[NormalizedObservation, ...],
) -> dict[tuple[str, str, str | None], tuple[dict[str, NormalizedObservation], ...]]:
    grouped = defaultdict(list)
    for observation in observations:
        key = json.loads(observation.source.record_key)
        grouped[(key["cusip"], key["title_of_class"], key["put_call"])].append(observation)
    result = {}
    for position, values in grouped.items():
        rows = defaultdict(dict)
        for value in values:
            key = json.loads(value.source.record_key)
            rows[key["row_id"]][value.field_name] = value
        result[position] = tuple(rows.values())
    return result


def _quality(left: DataQuality, right: DataQuality) -> DataQuality:
    return DataQuality.VALID if left == right == DataQuality.VALID else DataQuality.PARTIAL
