"""Deterministic MetricResult identity for one declared effective close."""

import json
from collections.abc import Mapping
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import JsonValue

from investment_analyst.analytics.metric_identity_v2 import metric_result_id_v2
from investment_analyst.core.models import DataQuality

_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:cazatiburones-institutional-weight:v1")


def expected_weight_result_id(
    *,
    asset_id: str,
    metric_key: str,
    known_at: datetime,
    parameters: Mapping[str, object],
    input_observation_id: UUID,
) -> UUID:
    return uuid5(
        _NAMESPACE,
        json.dumps(
            {
                "asset_id": asset_id,
                "metric_key": metric_key,
                "known_at": known_at.isoformat(),
                "parameters": parameters,
                "input_observation_id": str(input_observation_id),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
    )


def semantic_weight_result_id(
    *,
    asset_id: str,
    metric_key: str,
    as_of: datetime,
    available_at: datetime,
    quality: DataQuality,
    algorithm_version: str,
    parameters: Mapping[str, JsonValue],
    input_observation_id: UUID,
    known_at: datetime,
) -> UUID:
    """Return the cut-independent v2 identity for one declared position weight."""
    return metric_result_id_v2(
        asset_id=asset_id,
        metric_key=metric_key,
        input_observation_ids=(input_observation_id,),
        algorithm_version=algorithm_version,
        as_of=as_of,
        available_at=available_at,
        unit="ratio",
        quality=quality,
        parameters=parameters,
        known_at=known_at,
    )
