"""Canonical semantic metric identity v2 and version resolution adapter.

In v2, metric identity is a pure semantic coordinate rather than a content hash.
The identity preimage contains exactly:
- asset_id
- metric_key
- as_of (normalized UTC ISO-8601)
- available_at (normalized UTC ISO-8601)
- algorithm_version
- parameters (filtered to semantic parameters only)
- input_observation_ids (sorted string UUIDs)
- input_metric_result_ids (sorted string UUIDs)
- unit
- quality (normalized string value)

Explicitly excluded:
- known_at (neither in root preimage nor in parameters)
- computed_at (neither in root preimage nor in parameters)
- value (neither in root preimage nor in parameters)
- execution parameters (job_id, attempt_id, run_id, worker_id, execution_id,
  batch_id, trace_id, requested_cut, clock)
- job identifiers
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from uuid import UUID

from pydantic import JsonValue

from investment_analyst.core.models.enums import DataQuality
from investment_analyst.core.models.metric import MetricResult

# Explicit, tested set of execution parameter keys (never heuristic)
EXECUTION_PARAMETER_KEYS: frozenset[str] = frozenset(
    {
        "known_at",
        "computed_at",
        "value",
        "job_id",
        "attempt_id",
        "run_id",
        "worker_id",
        "execution_id",
        "batch_id",
        "trace_id",
        "requested_cut",
        "clock",
    }
)


class MetricIdentityVersion(StrEnum):
    """Supported metric identity versions."""

    V1 = "v1"
    V2 = "v2"


def is_execution_parameter(key: str) -> bool:
    """Explicit, non-heuristic determination of execution parameter keys."""
    return key in EXECUTION_PARAMETER_KEYS


def filter_semantic_parameters(parameters: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Return only semantic parameters, filtering out all execution parameters."""
    return {
        key: value for key, value in sorted(parameters.items()) if not is_execution_parameter(key)
    }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("identity datetimes must include timezone information")
    return value.astimezone(UTC)


def _utc_iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _canonical_json(value: object) -> str:
    def _default(obj: object) -> object:
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, UUID):
            return str(obj)
        if isinstance(obj, datetime):
            return _utc_iso(obj)
        if isinstance(obj, Enum):
            return obj.value
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    return json.dumps(
        value,
        default=_default,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_preimage_dict_v2(
    *,
    asset_id: str,
    metric_key: str,
    input_observation_ids: Sequence[UUID | str],
    algorithm_version: str,
    as_of: datetime,
    available_at: datetime,
    unit: str,
    quality: DataQuality | str,
    parameters: Mapping[str, JsonValue] | None = None,
    input_metric_result_ids: Sequence[UUID | str] | None = None,
    # Explicitly ignored / excluded execution coordinates
    value: Decimal | None = None,
    known_at: datetime | None = None,
    computed_at: datetime | None = None,
    **execution_kwargs: object,
) -> dict[str, JsonValue]:
    """Return the exact, canonical semantic coordinate dictionary for v2 identity.

    Deliberately excludes known_at, computed_at, value, execution parameters,
    and job identifiers case by case.
    """
    del value, known_at, computed_at, execution_kwargs
    return {
        "algorithm_version": algorithm_version,
        "as_of": _utc_iso(as_of),
        "asset_id": asset_id,
        "available_at": _utc_iso(available_at),
        "input_metric_result_ids": sorted(str(item) for item in (input_metric_result_ids or ())),
        "input_observation_ids": sorted(str(item) for item in input_observation_ids),
        "metric_key": metric_key,
        "parameters": filter_semantic_parameters(parameters or {}),
        "quality": quality.value if isinstance(quality, DataQuality) else str(quality),
        "unit": unit,
    }


def canonical_preimage_v2(
    *,
    asset_id: str,
    metric_key: str,
    input_observation_ids: Sequence[UUID | str],
    algorithm_version: str,
    as_of: datetime,
    available_at: datetime,
    unit: str,
    quality: DataQuality | str,
    parameters: Mapping[str, JsonValue] | None = None,
    input_metric_result_ids: Sequence[UUID | str] | None = None,
    value: Decimal | None = None,
    known_at: datetime | None = None,
    computed_at: datetime | None = None,
    **execution_kwargs: object,
) -> str:
    """Return the canonical serialized JSON preimage string for v2 identity."""
    preimage = canonical_preimage_dict_v2(
        asset_id=asset_id,
        metric_key=metric_key,
        input_observation_ids=input_observation_ids,
        algorithm_version=algorithm_version,
        as_of=as_of,
        available_at=available_at,
        unit=unit,
        quality=quality,
        parameters=parameters,
        input_metric_result_ids=input_metric_result_ids,
        value=value,
        known_at=known_at,
        computed_at=computed_at,
        **execution_kwargs,
    )
    return _canonical_json(preimage)


def _uuid_v8_from_bytes(digest: bytes) -> UUID:
    """Construct an RFC 9562 UUIDv8 from a 16+ byte hash digest."""
    raw = bytearray(digest[:16])
    # Set version bits to 8 (0b1000)
    raw[6] = (raw[6] & 0x0F) | 0x80
    # Set variant bits to RFC 4122 (0b10xx)
    raw[8] = (raw[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(raw))


def metric_result_id_v2(
    *,
    asset_id: str,
    metric_key: str,
    input_observation_ids: Sequence[UUID | str],
    algorithm_version: str,
    as_of: datetime,
    available_at: datetime,
    unit: str,
    quality: DataQuality | str,
    parameters: Mapping[str, JsonValue] | None = None,
    input_metric_result_ids: Sequence[UUID | str] | None = None,
    value: Decimal | None = None,
    known_at: datetime | None = None,
    computed_at: datetime | None = None,
    **execution_kwargs: object,
) -> UUID:
    """Return the deterministic v2 UUID for the given semantic coordinates.

    Uses an RFC 9562 UUIDv8 constructed from the SHA-256 digest of the canonical
    semantic preimage. Because v1 metric identities use UUIDv5, the identity
    space of v2 is disjoint from v1 by construction, without reading storage.
    """
    preimage_str = canonical_preimage_v2(
        asset_id=asset_id,
        metric_key=metric_key,
        input_observation_ids=input_observation_ids,
        algorithm_version=algorithm_version,
        as_of=as_of,
        available_at=available_at,
        unit=unit,
        quality=quality,
        parameters=parameters,
        input_metric_result_ids=input_metric_result_ids,
        value=value,
        known_at=known_at,
        computed_at=computed_at,
        **execution_kwargs,
    )
    digest = hashlib.sha256(preimage_str.encode("utf-8")).digest()
    return _uuid_v8_from_bytes(digest)


def metric_result_id_from_model_v2(result: MetricResult) -> UUID:
    """Derive the v2 identity directly from a MetricResult model."""
    return metric_result_id_v2(
        asset_id=result.asset_id,
        metric_key=result.metric_key,
        input_observation_ids=result.input_observation_ids,
        input_metric_result_ids=result.input_metric_result_ids,
        algorithm_version=result.algorithm_version,
        as_of=result.as_of,
        available_at=result.available_at,
        unit=result.unit,
        quality=result.quality,
        parameters=result.parameters,
    )


def resolve_metric_identity_version(identifier: UUID | str) -> MetricIdentityVersion:
    """Resolve whether an identifier belongs to v1 or v2 without reading storage.

    v1 identities are UUIDv5 (version == 5).
    v2 identities are UUIDv8 (version == 8).
    Resolution is pure, in-memory, and O(1).
    """
    if isinstance(identifier, str):
        parsed = UUID(identifier)
    elif isinstance(identifier, UUID):
        parsed = identifier
    else:
        raise TypeError(f"Expected UUID or str, got {type(identifier).__name__}")

    if parsed.version == 8:
        return MetricIdentityVersion.V2
    if parsed.version == 5:
        return MetricIdentityVersion.V1
    raise ValueError(
        f"Identifier {identifier} with UUID version {parsed.version} "
        "does not belong to metric identity v1 or v2"
    )


# Canonical alias for the adapter
resolve_identity_version = resolve_metric_identity_version

__all__ = [
    "EXECUTION_PARAMETER_KEYS",
    "MetricIdentityVersion",
    "canonical_preimage_dict_v2",
    "canonical_preimage_v2",
    "filter_semantic_parameters",
    "is_execution_parameter",
    "metric_result_id_from_model_v2",
    "metric_result_id_v2",
    "resolve_identity_version",
    "resolve_metric_identity_version",
]
