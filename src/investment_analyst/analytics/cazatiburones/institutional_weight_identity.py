"""Deterministic MetricResult identity for one declared effective close."""

import json
from collections.abc import Mapping
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

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
