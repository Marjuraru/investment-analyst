"""Stable identities for point-in-time market-statistics calculations."""

import json
from datetime import datetime
from uuid import UUID, uuid5

from investment_analyst.analytics.market.statistics_models import MetricCalculation
from investment_analyst.analytics.metric_identity_v2 import (
    metric_result_id_v2 as audited_metric_result_id_v2,
)

_RESULT_NAMESPACE = UUID("b5022527-e62b-5cb5-914e-3f67eedeb9be")
_SOURCE_PARAMETER = "source_id"


class MarketStatisticsIdentityError(RuntimeError):
    """Raised when a market calculation violates the semantic identity contract."""


def canonical_identity(calculation: MetricCalculation, known_at: datetime) -> str:
    """Return the UUID5 preimage, preserving the historical no-dependency form exactly."""
    document: dict[str, object] = {
        "asset_id": calculation.asset_id,
        "source_id": calculation.source_id,
        "metric_key": calculation.metric_key,
        "as_of": calculation.as_of.isoformat(),
        "available_at": calculation.available_at.isoformat(),
        "known_at": known_at.isoformat(),
        "parameters": calculation.parameters,
        "input_observation_ids": [str(item) for item in calculation.input_observation_ids],
        "algorithm_version": calculation.algorithm_version,
        "value": str(calculation.value),
        "unit": calculation.unit,
        "quality": calculation.quality.value,
    }
    if calculation.input_metric_result_ids:
        document["input_metric_result_ids"] = [
            str(item) for item in calculation.input_metric_result_ids
        ]
    return json.dumps(document, allow_nan=False, separators=(",", ":"), sort_keys=True)


def metric_result_id(calculation: MetricCalculation, known_at: datetime) -> UUID:
    """Return the stable UUID5 identity for one point-in-time calculation."""
    return uuid5(_RESULT_NAMESPACE, canonical_identity(calculation, known_at))


def semantic_metric_result_id(calculation: MetricCalculation) -> UUID:
    """Return the stable UUID8 semantic identity for one market calculation.

    Delegates to the audited v2 rule and fails closed when ``parameters["source_id"]``
    does not match the calculation source, because that parameter is where the v2
    identity keeps its per-source separation.
    """
    if calculation.parameters.get(_SOURCE_PARAMETER) != calculation.source_id:
        raise MarketStatisticsIdentityError(
            "v2 market identity requires parameters['source_id'] to match the calculation source"
        )
    return audited_metric_result_id_v2(
        asset_id=calculation.asset_id,
        metric_key=calculation.metric_key,
        input_observation_ids=calculation.input_observation_ids,
        input_metric_result_ids=calculation.input_metric_result_ids,
        algorithm_version=calculation.algorithm_version,
        as_of=calculation.as_of,
        available_at=calculation.available_at,
        unit=calculation.unit,
        quality=calculation.quality,
        parameters=calculation.parameters,
    )
