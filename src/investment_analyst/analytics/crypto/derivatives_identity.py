"""Canonical identities for derivatives metrics and read-only diagnostics."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import JsonValue

from investment_analyst.analytics.metric_identity_v2 import (
    metric_result_id_v2 as audited_metric_result_id_v2,
)
from investment_analyst.core.models import DataQuality


def metric_result_id(
    *,
    asset_id: str,
    metric_key: str,
    input_observation_ids: tuple[UUID, ...],
    parameters: dict[str, JsonValue],
    algorithm_version: str,
    as_of: datetime,
    available_at: datetime,
    value: Decimal,
    unit: str,
    quality: DataQuality,
) -> UUID:
    """Return the fixed v1 metric identity; computed_at is deliberately excluded."""
    preimage: dict[str, JsonValue] = {
        "algorithm_version": algorithm_version,
        "as_of": _utc(as_of).isoformat(),
        "asset_id": asset_id,
        "available_at": _utc(available_at).isoformat(),
        "input_observation_ids": [str(item) for item in input_observation_ids],
        "metric_key": metric_key,
        "parameters": parameters,
        "quality": quality.value,
        "unit": unit,
        "value": str(value),
    }
    return _identity(preimage)


def semantic_metric_result_id(
    *,
    asset_id: str,
    metric_key: str,
    input_observation_ids: tuple[UUID, ...],
    parameters: dict[str, JsonValue],
    algorithm_version: str,
    as_of: datetime,
    available_at: datetime,
    unit: str,
    quality: DataQuality,
    value: Decimal | None = None,
    known_at: datetime | None = None,
    computed_at: datetime | None = None,
    **execution_kwargs: object,
) -> UUID:
    """Return the UUID8 semantic identity of one derivatives metric.

    Delegates to the audited v2 rule, which deliberately excludes the cut
    (``known_at``), the clock (``computed_at``), ``value`` and execution
    parameters from the semantic coordinate.
    """
    return audited_metric_result_id_v2(
        asset_id=asset_id,
        metric_key=metric_key,
        input_observation_ids=input_observation_ids,
        algorithm_version=algorithm_version,
        as_of=as_of,
        available_at=available_at,
        unit=unit,
        quality=quality,
        parameters=parameters,
        value=value,
        known_at=known_at,
        computed_at=computed_at,
        **execution_kwargs,
    )


def diagnostic_id(
    *,
    asset_id: str,
    source_ids: tuple[str, ...],
    known_at: datetime,
    observation_ids: tuple[UUID, ...],
    metric_result_ids: tuple[UUID, ...],
    dimensional_states: dict[str, JsonValue],
    algorithm_version: str,
) -> UUID:
    """Return the provider-independent read-only diagnostic identity."""
    preimage: dict[str, JsonValue] = {
        "algorithm_version": algorithm_version,
        "asset_id": asset_id,
        "dimensional_states": dimensional_states,
        "known_at": _utc(known_at).isoformat(),
        "metric_result_ids": [str(item) for item in metric_result_ids],
        "observation_ids": [str(item) for item in observation_ids],
        "source_ids": list(source_ids),
    }
    return _identity(preimage)


def _identity(preimage: dict[str, JsonValue]) -> UUID:
    text = json.dumps(
        preimage,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(NAMESPACE_URL, text)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("identity datetimes must include timezone information")
    return value.astimezone(UTC)


__all__ = ["diagnostic_id", "metric_result_id"]
