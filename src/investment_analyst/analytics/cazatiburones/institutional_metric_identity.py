"""Deterministic identity for institutional layer-3 metric results."""

import json
from uuid import NAMESPACE_URL, UUID, uuid5

from investment_analyst.analytics.cazatiburones.institutional_metric_models import (
    InstitutionalMetricCandidate,
)

_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:cazatiburones-institutional-metric-result:v1")


def expected_institutional_metric_result_id(candidate: InstitutionalMetricCandidate) -> UUID:
    return uuid5(
        _NAMESPACE,
        json.dumps(
            {
                "asset_id": candidate.asset_id,
                "metric_key": candidate.metric_key,
                "unit": candidate.unit,
                "as_of": candidate.as_of.isoformat(),
                "available_at": candidate.available_at.isoformat(),
                "known_at": candidate.known_at.isoformat(),
                "parameters": candidate.parameters,
                "input_observation_ids": [str(value) for value in candidate.input_observation_ids],
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
    )
