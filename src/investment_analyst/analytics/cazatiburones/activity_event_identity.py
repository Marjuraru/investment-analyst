"""Canonical UUID5 identities for immutable activity-event artifacts."""

import json
from datetime import datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

_EVENT = uuid5(NAMESPACE_URL, "investment-analyst:cazatiburones-activity-event:v1")
_CANDIDATE = uuid5(NAMESPACE_URL, "investment-analyst:cazatiburones-activity-candidate:v1")
_SNAPSHOT = uuid5(NAMESPACE_URL, "investment-analyst:cazatiburones-activity-event-snapshot:v1")


def canonical(value: object) -> str:
    return json.dumps(
        value, default=_default, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def event_id(payload: dict[str, object]) -> UUID:
    return uuid5(_EVENT, canonical(payload))


def candidate_id(event: UUID, policy_version: str) -> UUID:
    return uuid5(_CANDIDATE, canonical({"event_id": str(event), "policy_version": policy_version}))


def snapshot_id(payload: dict[str, object]) -> UUID:
    return uuid5(_SNAPSHOT, canonical(payload))


def _default(value: object) -> str:
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot canonically encode {type(value).__name__}")
