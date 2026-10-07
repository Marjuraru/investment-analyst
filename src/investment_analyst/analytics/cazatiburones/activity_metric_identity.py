"""Deterministic identity for persisted cazatiburones activity metric results.

The preimage excludes ``computed_at`` and the value: a reexecution at the same cut with a
different clock reuses the identity, while a genuinely different corte (``known_at``) always
produces a distinct identity, even when the computed value happens to coincide.
"""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, UUID, uuid5

from investment_analyst.analytics.cazatiburones.activity_metric_models import (
    ActivityMetricCandidate,
)
from investment_analyst.analytics.metric_identity_v2 import metric_result_id_v2

_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:cazatiburones-activity-metric-result:v1")


def expected_activity_metric_result_id(candidate: ActivityMetricCandidate) -> UUID:
    """Deterministic identity excluding the computation clock and the computed value."""
    preimage = json.dumps(
        {
            "asset_id": candidate.asset_id,
            "metric_key": candidate.metric_key,
            "algorithm_version": candidate.algorithm_version,
            "unit": candidate.unit,
            "as_of": candidate.as_of.isoformat(),
            "available_at": candidate.available_at.isoformat(),
            "known_at": candidate.known_at.isoformat(),
            "parameters": candidate.parameters,
            "input_observation_ids": [str(item) for item in candidate.input_observation_ids],
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return uuid5(_NAMESPACE, preimage)


def semantic_activity_metric_result_id(candidate: ActivityMetricCandidate) -> UUID:
    """Return the cut-independent v2 identity for the same semantic activity metric."""
    return metric_result_id_v2(
        asset_id=candidate.asset_id,
        metric_key=candidate.metric_key,
        input_observation_ids=candidate.input_observation_ids,
        algorithm_version=candidate.algorithm_version,
        as_of=candidate.as_of,
        available_at=candidate.available_at,
        unit=candidate.unit,
        quality=candidate.quality,
        parameters=candidate.parameters,
        known_at=candidate.known_at,
    )
