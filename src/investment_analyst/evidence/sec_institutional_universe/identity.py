"""Deterministic UUID5 identity generation for Form 13F manager universe artifacts."""

from __future__ import annotations

import json
from datetime import date
from uuid import NAMESPACE_URL, UUID, uuid5

from investment_analyst.evidence.sec_documents.models import normalize_cik

_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:sec-13f-manager-universe:v1")

SEC_13F_DATA_SET_REVISION_SCHEMA_VERSION = "sec-13f-data-set-revision-v1"
SEC_13F_MANAGER_UNIVERSE_SCHEMA_VERSION = "sec-13f-manager-universe-v1"
SEC_13F_MANAGER_UNIVERSE_SELECTION_POLICY = "sec-13f-manager-universe-selection-v1"
SEC_13F_MANAGER_UNIVERSE_SOURCE_ID = "sec-edgar:form-13f-data-set:manager-universe"


def canonical_identity_json(values: list[object]) -> str:
    """Encode identity inputs with the same UTF-8-safe JSON rules in every caller."""
    return json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def dataset_revision_id(
    period_start: date,
    period_end: date,
    content_sha256: str,
    schema_version: str = SEC_13F_DATA_SET_REVISION_SCHEMA_VERSION,
) -> UUID:
    """Compute deterministic identity for one official dataset revision."""
    return uuid5(
        _NAMESPACE,
        canonical_identity_json(
            [
                schema_version,
                period_start.isoformat(),
                period_end.isoformat(),
                content_sha256.lower().strip(),
            ]
        ),
    )


def dataset_raw_record_id(revision_id: UUID) -> UUID:
    """Compute deterministic RawRecord identifier for a dataset revision."""
    return uuid5(_NAMESPACE, f"raw:dataset:{revision_id}")


def candidate_id(
    dataset_sha256: str,
    asset_id: str,
    cusip: str,
    manager_cik: str,
    accession: str,
    form: str,
    report_period: date,
    schema_version: str = SEC_13F_MANAGER_UNIVERSE_SCHEMA_VERSION,
) -> UUID:
    """Compute deterministic candidate manager identity within a dataset."""
    return uuid5(
        _NAMESPACE,
        canonical_identity_json(
            [
                schema_version,
                dataset_sha256.lower().strip(),
                asset_id.strip(),
                cusip.upper().strip(),
                normalize_cik(manager_cik),
                accession.strip(),
                form.upper().strip(),
                report_period.isoformat(),
            ]
        ),
    )


def snapshot_id(
    dataset_revision_id: UUID,
    policy_version: str,
    catalog_version: int | str,
    covered_cusips: tuple[str, ...],
    schema_version: str = SEC_13F_MANAGER_UNIVERSE_SCHEMA_VERSION,
) -> UUID:
    """Compute deterministic snapshot identity from revision, policy, and catalog state."""
    normalized_cusips = sorted(c.upper().strip() for c in covered_cusips)
    return uuid5(
        _NAMESPACE,
        canonical_identity_json(
            [
                schema_version,
                str(dataset_revision_id),
                policy_version.strip(),
                str(catalog_version),
                normalized_cusips,
            ]
        ),
    )


def snapshot_raw_record_id(snapshot_id: UUID) -> UUID:
    """Compute deterministic RawRecord identifier for a manager universe snapshot."""
    return uuid5(_NAMESPACE, f"raw:snapshot:{snapshot_id}")
