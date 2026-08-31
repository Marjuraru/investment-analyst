"""Canonical UUID5 identities for isolated Form 13F semantic evidence."""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, UUID, uuid5

_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:sec-institutional-semantics:v2")


def canonical_identity_json(values: list[object]) -> str:
    """Encode identity inputs with the same UTF-8-safe JSON rules in every caller."""
    return json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def artifact_id(
    parent_report_id: UUID,
    cover_revision_id: UUID,
    information_table_revision_id: UUID,
    parser_version: str,
    schema_version: str,
) -> UUID:
    return uuid5(
        _NAMESPACE,
        canonical_identity_json(
            [
                str(parent_report_id),
                str(cover_revision_id),
                str(information_table_revision_id),
                parser_version,
                schema_version,
            ]
        ),
    )


def row_id(artifact: UUID, row_number: int) -> UUID:
    return uuid5(_NAMESPACE, f"row:{canonical_identity_json([str(artifact), row_number])}")


def raw_record_id(artifact: UUID) -> UUID:
    return uuid5(_NAMESPACE, f"raw:{artifact}")
