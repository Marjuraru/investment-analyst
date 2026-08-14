"""Stable UUID5 identity for multi-asset market-comparison artifacts."""

import json
from uuid import UUID, uuid5

from investment_analyst.analytics.market.comparison_models import MarketComparisonRequest

_NAMESPACE = UUID("9e2a5602-476b-5ed1-9e2f-f0df11ffaf61")


def comparison_id(request: MarketComparisonRequest, observation_ids: tuple[UUID, ...]) -> UUID:
    """Return a deterministic identity tied to canonical request and close evidence."""
    document = {
        "algorithm_version": "market-multi-asset-comparison-v1-decimal34",
        "asset_ids": list(request.canonical_asset_ids),
        "benchmark_id": request.benchmark_id,
        "end_date": request.end_date.isoformat(),
        "known_at": request.known_at.isoformat(),
        "observation_ids": [str(item) for item in observation_ids],
        "start_date": request.start_date.isoformat(),
    }
    return uuid5(_NAMESPACE, json.dumps(document, separators=(",", ":"), sort_keys=True))
