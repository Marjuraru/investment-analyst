"""Deterministic UUID5 identities for one row-scoped 13F correspondence claim."""

from __future__ import annotations

import json
from datetime import date, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:sec-institutional-row-correspondence:v1")


def _canonical(values: list[object]) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def correspondence_id(
    *,
    asset_id: str,
    cusip: str,
    title_of_class: str,
    report_period: date,
    available_at: datetime,
    universe_snapshot_id: UUID,
    candidate_id: UUID,
    artifact_id: UUID,
    row_id: UUID,
    policy_version: str,
    schema_version: str,
) -> UUID:
    """Compute the identity of one row-scoped match under its declared policy version."""
    return uuid5(
        _NAMESPACE,
        _canonical(
            [
                schema_version,
                policy_version,
                str(universe_snapshot_id),
                str(candidate_id),
                str(artifact_id),
                str(row_id),
                asset_id.strip(),
                cusip.upper().strip(),
                title_of_class.strip(),
                report_period.isoformat(),
                available_at.isoformat(),
            ]
        ),
    )


def correspondence_raw_record_id(value: UUID) -> UUID:
    """Compute the RawRecord identifier that stores one claim."""
    return uuid5(_NAMESPACE, f"raw-record:{value}")
