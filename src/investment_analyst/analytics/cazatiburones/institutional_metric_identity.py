"""Deterministic identity for institutional layer-3 metric results."""

import json
from uuid import NAMESPACE_URL, UUID, uuid5

from investment_analyst.analytics.cazatiburones.institutional_metric_definitions import (
    ALGORITHM_VERSION,
)
from investment_analyst.analytics.cazatiburones.institutional_metric_models import (
    InstitutionalMetricCandidate,
)
from investment_analyst.analytics.metric_identity_v2 import metric_result_id_v2

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


def semantic_institutional_metric_result_id(candidate: InstitutionalMetricCandidate) -> UUID:
    """Return the cut-independent v2 identity for the same 13F metric evidence."""
    return metric_result_id_v2(
        asset_id=candidate.asset_id,
        metric_key=candidate.metric_key,
        input_observation_ids=candidate.input_observation_ids,
        algorithm_version=ALGORITHM_VERSION,
        as_of=candidate.as_of,
        available_at=candidate.available_at,
        unit=candidate.unit,
        quality=candidate.quality,
        parameters=candidate.parameters,
        known_at=candidate.known_at,
    )
