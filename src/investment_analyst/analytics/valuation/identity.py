"""Stable identities for append-only corporate valuation results."""

import json
from uuid import UUID, uuid5

from investment_analyst.analytics.valuation.models import CorporateValuationRequest

_NAMESPACE = UUID("12a332a7-6bd1-4b25-9a3a-48801b65c725")


def valuation_result_id(
    *,
    request: CorporateValuationRequest,
    metric_key: str,
    valuation_as_of: str,
    annual_period_start: str | None,
    annual_period_end: str,
    security_basis_version: str,
    input_observation_ids: tuple[UUID, ...],
    algorithm_version: str,
) -> UUID:
    """Identify one metric by cut, semantics, evidence and algorithm."""
    document = {
        "asset_id": request.asset_id,
        "metric_key": metric_key,
        "known_at": request.known_at.isoformat(),
        "valuation_date": request.valuation_date.isoformat(),
        "basis": request.basis,
        "valuation_as_of": valuation_as_of,
        "annual_period_start": annual_period_start,
        "annual_period_end": annual_period_end,
        "security_basis_version": security_basis_version,
        "input_observation_ids": sorted(str(item) for item in input_observation_ids),
        "algorithm_version": algorithm_version,
    }
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return uuid5(_NAMESPACE, encoded)


__all__ = ["valuation_result_id"]
