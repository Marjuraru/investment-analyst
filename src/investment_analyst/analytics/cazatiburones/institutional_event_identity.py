"""Deterministic identity generation for institutional events, candidates, and snapshots."""

import json
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

_NAMESPACE_EVENT = UUID("f0123456-789a-bcde-f012-3456789abcde")
_NAMESPACE_CANDIDATE = UUID("01234567-89ab-cdef-0123-456789abcdef")
_NAMESPACE_SNAPSHOT = UUID("12345678-9abc-def0-1234-56789abcdef0")


def _json_default(val: Any) -> Any:
    if isinstance(val, UUID):
        return str(val)
    if isinstance(val, Decimal):
        return str(val)
    if hasattr(val, "isoformat"):
        return val.isoformat()
    raise TypeError(f"Object of type {type(val)} is not JSON serializable")


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)


def event_id(payload: dict[str, Any]) -> UUID:
    return uuid5(_NAMESPACE_EVENT, _canonical_json(payload))


def candidate_id(event_uuid: UUID, policy_version: str) -> UUID:
    payload = {"event_id": str(event_uuid), "policy_version": policy_version}
    return uuid5(_NAMESPACE_CANDIDATE, _canonical_json(payload))


def snapshot_id(payload: dict[str, Any]) -> UUID:
    return uuid5(_NAMESPACE_SNAPSHOT, _canonical_json(payload))
