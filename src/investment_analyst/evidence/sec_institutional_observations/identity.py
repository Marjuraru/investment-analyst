"""Deterministic native identities for one linked 13F row field."""

import json
from uuid import NAMESPACE_URL, UUID, uuid5

from .definitions import TRANSFORMATION_VERSION

_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:sec-institutional-observation:v1")


def observation_id(
    artifact_id: UUID, row_id: UUID, correspondence_id: UUID, field_name: str
) -> UUID:
    return uuid5(
        _NAMESPACE,
        json.dumps(
            [
                str(artifact_id),
                str(row_id),
                str(correspondence_id),
                field_name,
                TRANSFORMATION_VERSION,
            ],
            separators=(",", ":"),
        ),
    )
