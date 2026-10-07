"""Stable identities for append-only corporate valuation results."""

import json
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid5

from pydantic import JsonValue

from investment_analyst.analytics.metric_identity_v2 import metric_result_id_v2
from investment_analyst.analytics.valuation.models import CorporateValuationRequest
from investment_analyst.core.models import DataQuality

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


def valuation_result_parameters_v2(
    *,
    request: CorporateValuationRequest,
    annual_period_start: datetime | None,
    annual_period_end: datetime,
    security_basis_version: str,
    formula: str,
    market_units_per_reported_share: Decimal,
) -> dict[str, JsonValue]:
    """Return semantic persisted parameters without the request cut."""
    return {
        "category": "valuation",
        "basis": request.basis,
        "valuation_date": request.valuation_date.isoformat(),
        "annual_period_start": (
            annual_period_start.isoformat() if annual_period_start is not None else None
        ),
        "annual_period_end": annual_period_end.isoformat(),
        "formula": formula,
        "security_basis_version": security_basis_version,
        "market_units_per_reported_share": str(market_units_per_reported_share),
    }


def valuation_result_id_v2(
    *,
    request: CorporateValuationRequest,
    metric_key: str,
    valuation_as_of: datetime,
    available_at: datetime,
    unit: str,
    annual_period_start: datetime | None,
    annual_period_end: datetime,
    security_basis_version: str,
    input_observation_ids: tuple[UUID, ...],
    algorithm_version: str,
    formula: str,
    market_units_per_reported_share: Decimal,
) -> UUID:
    """Return cut-independent UUIDv8 identity for one valuation metric."""
    parameters = valuation_result_parameters_v2(
        request=request,
        annual_period_start=annual_period_start,
        annual_period_end=annual_period_end,
        security_basis_version=security_basis_version,
        formula=formula,
        market_units_per_reported_share=market_units_per_reported_share,
    )
    return metric_result_id_v2(
        asset_id=request.asset_id,
        metric_key=metric_key,
        input_observation_ids=input_observation_ids,
        algorithm_version=algorithm_version,
        as_of=valuation_as_of,
        available_at=available_at,
        unit=unit,
        quality=DataQuality.VALID,
        parameters=parameters,
        known_at=request.known_at,
    )


__all__ = ["valuation_result_id", "valuation_result_id_v2", "valuation_result_parameters_v2"]
