"""Explicit, bounded compatibility between a persisted 13F observation and its replay.

The helper is pure: it receives already-resolved parents and never opens storage. Only two
processing/legacy differences are tolerated, and both require every economic field, identity,
availability and document hash to remain identical:

* ``replayed_retrieval_time``: the retrieval clock is replayed by a later parse, as designed.
* ``legacy_acceptance_timezone``: a historical acceptance timestamp was labelled UTC while its
  wall time was America/New_York local time; it converts exactly to the current documentary UTC
  acceptance instant without crossing the monetary policy threshold.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from investment_analyst.core.models.observation import NormalizedObservation
from investment_analyst.evidence.instrument_correspondence.models import InstrumentCorrespondence
from investment_analyst.evidence.sec_institutional_correspondence.models import (
    SecInstitutionalRowCorrespondence,
)
from investment_analyst.evidence.sec_institutional_holdings.models import (
    InstitutionalHoldingsReport,
)
from investment_analyst.evidence.sec_institutional_semantics.models import (
    InstitutionalHoldingsSemantics,
    InstitutionalSemanticsRow,
)
from investment_analyst.storage import StorageError

from .definitions import MONETARY_POLICY_VERSION, monetary_value

CompatibilityNote = Literal["replayed_retrieval_time", "legacy_acceptance_timezone"]

COMPATIBILITY_NOTE_ORDER: tuple[CompatibilityNote, ...] = (
    "replayed_retrieval_time",
    "legacy_acceptance_timezone",
)

RECORD_KEY_FIELDS = frozenset(
    {
        "artifact_id",
        "report_id",
        "manager_cik",
        "row_id",
        "correspondence_id",
        "cover_revision_id",
        "cover_content_sha256",
        "information_table_revision_id",
        "information_table_content_sha256",
        "cusip",
        "title_of_class",
        "put_call",
        "quantity_type",
        "transformation_version",
        "monetary_policy_version",
        "filing_accepted_at",
        "field_name",
    }
)

_NEW_YORK = ZoneInfo("America/New_York")


class InstitutionalObservationCompatibilityError(StorageError):
    """A replay differs from the persisted observation outside the admitted tolerances."""


def parse_record_key(observation: NormalizedObservation) -> dict[str, str | None]:
    """Parse and canonically validate the complete institutional lineage key."""
    try:
        parsed = json.loads(observation.source.record_key)
    except (TypeError, json.JSONDecodeError) as error:
        raise InstitutionalObservationCompatibilityError(
            "institutional observation record_key is invalid"
        ) from error
    if not isinstance(parsed, dict) or set(parsed) != RECORD_KEY_FIELDS:
        raise InstitutionalObservationCompatibilityError(
            "institutional observation record_key is incomplete"
        )
    required_strings = RECORD_KEY_FIELDS - {"put_call", "quantity_type"}
    nullable_strings = {"put_call", "quantity_type"}
    if any(not isinstance(parsed[name], str) for name in required_strings) or any(
        parsed[name] is not None and not isinstance(parsed[name], str) for name in nullable_strings
    ):
        raise InstitutionalObservationCompatibilityError(
            "institutional observation record_key has invalid types"
        )
    if json.dumps(parsed, sort_keys=True, separators=(",", ":")) != observation.source.record_key:
        raise InstitutionalObservationCompatibilityError(
            "institutional observation record_key is not canonical"
        )
    try:
        UUID(parsed["artifact_id"])
        UUID(parsed["report_id"])
        UUID(parsed["row_id"])
        UUID(parsed["correspondence_id"])
        UUID(parsed["cover_revision_id"])
        UUID(parsed["information_table_revision_id"])
    except ValueError as error:
        raise InstitutionalObservationCompatibilityError(
            "institutional observation record_key has invalid IDs"
        ) from error
    return cast(dict[str, str | None], parsed)


def compare_reconstruction(
    existing: NormalizedObservation,
    candidate: NormalizedObservation,
    *,
    existing_key: dict[str, str | None],
    candidate_key: dict[str, str | None],
    artifact: InstitutionalHoldingsSemantics,
    report: InstitutionalHoldingsReport,
    row: InstitutionalSemanticsRow,
    correspondence: InstrumentCorrespondence | SecInstitutionalRowCorrespondence,
) -> tuple[CompatibilityNote, ...]:
    """Return the admitted compatibility notes or fail on any unproved difference.

    ``normalized_at`` and ``source.retrieved_at`` describe processing clocks and are excluded
    from semantic equality. The legacy acceptance tolerance is strictly narrower: the old
    timestamp must be UTC-labelled, its naive wall time must localize unambiguously in
    America/New_York and convert exactly to the current documentary acceptance instant.
    """
    _verify_parents(artifact, report, row, correspondence)
    if (
        correspondence.asset_id != existing.asset_id
        or correspondence.asset_id != candidate.asset_id
    ):
        raise InstitutionalObservationCompatibilityError(
            "institutional observation asset conflicts with its correspondence"
        )
    _verify_key(existing_key, existing, artifact, report, row, correspondence)
    _verify_key(candidate_key, candidate, artifact, report, row, correspondence)
    accepted_at = artifact.cover_revision.document.filing.accepted_at
    if datetime.fromisoformat(candidate_key["filing_accepted_at"]) != accepted_at:
        raise InstitutionalObservationCompatibilityError(
            "institutional observation candidate acceptance differs from its filing"
        )
    restored = existing.model_copy(update={"normalized_at": candidate.normalized_at})
    if restored == candidate:
        return ()
    _require_equal_material(existing, candidate, existing_key, candidate_key)
    notes: list[CompatibilityNote] = []
    if existing.source.retrieved_at != candidate.source.retrieved_at:
        notes.append("replayed_retrieval_time")
    old_accepted = existing_key["filing_accepted_at"]
    new_accepted = candidate_key["filing_accepted_at"]
    if old_accepted != new_accepted:
        _verify_legacy_acceptance(
            old_text=old_accepted,
            new_text=new_accepted,
            current=accepted_at,
            row=row,
        )
        notes.append("legacy_acceptance_timezone")
    if not notes:
        raise InstitutionalObservationCompatibilityError(
            "institutional observation content conflicts"
        )
    return tuple(note for note in COMPATIBILITY_NOTE_ORDER if note in notes)


def compare_witnessed_reconstruction(
    existing: NormalizedObservation,
    candidate: NormalizedObservation,
    *,
    existing_key: dict[str, str | None],
    candidate_key: dict[str, str | None],
    original_claim: SecInstitutionalRowCorrespondence,
    current_claim: SecInstitutionalRowCorrespondence,
    artifact: InstitutionalHoldingsSemantics,
    report: InstitutionalHoldingsReport,
    row: InstitutionalSemanticsRow,
) -> None:
    """Prove that a stale original claim and a current verified claim cover the same row.

    The only admitted differences are the original/current claim identities and the availability
    derived from them, plus the bounded A1 metadata tolerances. The historical observation's
    ``available_at`` is never rewritten and the old claim is never certified as current.
    """
    _compare_claim_linked_reconstruction(
        existing,
        candidate,
        existing_key=existing_key,
        candidate_key=candidate_key,
        original_claim=original_claim,
        current_claim=current_claim,
        artifact=artifact,
        report=report,
        row=row,
        require_original_older=True,
    )


def compare_equivalent_reconstruction(
    existing: NormalizedObservation,
    candidate: NormalizedObservation,
    *,
    existing_key: dict[str, str | None],
    candidate_key: dict[str, str | None],
    original_claim: SecInstitutionalRowCorrespondence,
    current_claim: SecInstitutionalRowCorrespondence,
    artifact: InstitutionalHoldingsSemantics,
    report: InstitutionalHoldingsReport,
    row: InstitutionalSemanticsRow,
) -> None:
    """Prove that two strictly verified equivalent claims cover the same row and fields."""
    _compare_claim_linked_reconstruction(
        existing,
        candidate,
        existing_key=existing_key,
        candidate_key=candidate_key,
        original_claim=original_claim,
        current_claim=current_claim,
        artifact=artifact,
        report=report,
        row=row,
        require_original_older=False,
    )


def _compare_claim_linked_reconstruction(
    existing: NormalizedObservation,
    candidate: NormalizedObservation,
    *,
    existing_key: dict[str, str | None],
    candidate_key: dict[str, str | None],
    original_claim: SecInstitutionalRowCorrespondence,
    current_claim: SecInstitutionalRowCorrespondence,
    artifact: InstitutionalHoldingsSemantics,
    report: InstitutionalHoldingsReport,
    row: InstitutionalSemanticsRow,
    require_original_older: bool,
) -> None:
    _verify_parents(artifact, report, row, original_claim)
    _verify_parents(artifact, report, row, current_claim)
    _verify_key(existing_key, existing, artifact, report, row, original_claim)
    _verify_key(candidate_key, candidate, artifact, report, row, current_claim)
    accepted_at = artifact.cover_revision.document.filing.accepted_at
    if datetime.fromisoformat(candidate_key["filing_accepted_at"]) != accepted_at:
        raise InstitutionalObservationCompatibilityError(
            "claim-linked candidate acceptance differs from its filing"
        )
    for name in (
        "asset_id",
        "cusip",
        "title_of_class",
        "report_period",
        "manager_cik",
        "report_id",
        "artifact_id",
        "row_id",
        "dataset_revision_id",
        "candidate_id",
        "policy_version",
        "schema_version",
    ):
        if getattr(original_claim, name) != getattr(current_claim, name):
            raise InstitutionalObservationCompatibilityError(
                "claim-linked claims disagree on their row, dataset or candidate"
            )
    if original_claim.correspondence_id == current_claim.correspondence_id:
        raise InstitutionalObservationCompatibilityError(
            "claim-linked reconstruction requires distinct claim identities"
        )
    if require_original_older and original_claim.available_at >= current_claim.available_at:
        raise InstitutionalObservationCompatibilityError(
            "witnessed original claim is not temporally older than the current proof"
        )
    if existing.available_at != max(original_claim.available_at, artifact.available_at):
        raise InstitutionalObservationCompatibilityError(
            "historical observation availability differs from its original claim"
        )
    if candidate.available_at != max(current_claim.available_at, artifact.available_at):
        raise InstitutionalObservationCompatibilityError(
            "current reconstruction availability differs from its current claim"
        )
    if existing.observation_id == candidate.observation_id:
        raise InstitutionalObservationCompatibilityError(
            "claim-linked reconstruction must use distinct observation identities"
        )
    for name in (
        "asset_id",
        "raw_record_id",
        "field_name",
        "value",
        "unit",
        "frequency",
        "period_end",
        "quality",
        "transformation_version",
    ):
        if getattr(existing, name) != getattr(candidate, name):
            raise InstitutionalObservationCompatibilityError(
                "claim-linked reconstruction changes an economic or identity field"
            )
    if (
        existing.source.source_id != candidate.source.source_id
        or existing.source.raw_uri != candidate.source.raw_uri
        or existing.source.checksum_sha256 != candidate.source.checksum_sha256
    ):
        raise InstitutionalObservationCompatibilityError(
            "claim-linked reconstruction changes its source documents"
        )
    if any(
        existing_key[name] != candidate_key[name]
        for name in RECORD_KEY_FIELDS - {"correspondence_id", "filing_accepted_at"}
    ):
        raise InstitutionalObservationCompatibilityError(
            "claim-linked reconstruction changes a lineage field"
        )
    if existing_key["filing_accepted_at"] != candidate_key["filing_accepted_at"]:
        _verify_legacy_acceptance(
            old_text=existing_key["filing_accepted_at"],
            new_text=candidate_key["filing_accepted_at"],
            current=accepted_at,
            row=row,
        )


def _verify_parents(
    artifact: InstitutionalHoldingsSemantics,
    report: InstitutionalHoldingsReport,
    row: InstitutionalSemanticsRow,
    correspondence: InstrumentCorrespondence | SecInstitutionalRowCorrespondence,
) -> None:
    if (
        artifact.parent_report_id != report.report_id
        or artifact.manager_cik != report.manager_cik
        or artifact.cover_revision != report.cover_revision
        or artifact.information_table_revision != report.information_table_revision
    ):
        raise InstitutionalObservationCompatibilityError(
            "institutional observation report lineage conflicts"
        )
    if row not in artifact.rows:
        raise InstitutionalObservationCompatibilityError(
            "institutional observation row lineage conflicts"
        )
    if isinstance(correspondence, SecInstitutionalRowCorrespondence):
        if (
            correspondence.report_id != report.report_id
            or correspondence.artifact_id != artifact.artifact_id
            or correspondence.row_id != row.row_id
            or correspondence.manager_cik != report.manager_cik
            or correspondence.report_period != report.report_period
            or correspondence.cusip != row.cusip
        ):
            raise InstitutionalObservationCompatibilityError(
                "institutional observation claim lineage conflicts"
            )
    elif not (
        correspondence.cusip == row.cusip
        and correspondence.title_of_class == row.title_of_class
        and correspondence.is_effective_on(report.report_period)
    ):
        raise InstitutionalObservationCompatibilityError(
            "institutional observation correspondence lineage conflicts"
        )


def _verify_key(
    key: dict[str, str | None],
    observation: NormalizedObservation,
    artifact: InstitutionalHoldingsSemantics,
    report: InstitutionalHoldingsReport,
    row: InstitutionalSemanticsRow,
    correspondence: InstrumentCorrespondence | SecInstitutionalRowCorrespondence,
) -> None:
    if set(key) != RECORD_KEY_FIELDS:
        raise InstitutionalObservationCompatibilityError(
            "institutional observation record_key is incomplete"
        )
    expected = {
        "artifact_id": str(artifact.artifact_id),
        "report_id": str(report.report_id),
        "manager_cik": artifact.manager_cik,
        "row_id": str(row.row_id),
        "correspondence_id": str(correspondence.correspondence_id),
        "cover_revision_id": str(artifact.cover_revision.revision_id),
        "cover_content_sha256": artifact.cover_revision.content_sha256,
        "information_table_revision_id": str(artifact.information_table_revision.revision_id),
        "information_table_content_sha256": artifact.information_table_revision.content_sha256,
        "cusip": row.cusip,
        "title_of_class": row.title_of_class,
        "put_call": row.put_call,
        "quantity_type": row.quantity_type,
        "transformation_version": observation.transformation_version,
        "monetary_policy_version": MONETARY_POLICY_VERSION,
        "field_name": observation.field_name,
    }
    if any(key[name] != value for name, value in expected.items()):
        raise InstitutionalObservationCompatibilityError(
            "institutional observation record_key lineage conflicts"
        )
    old_accepted = key["filing_accepted_at"]
    try:
        parsed = datetime.fromisoformat(old_accepted)
    except (TypeError, ValueError) as error:
        raise InstitutionalObservationCompatibilityError(
            "institutional observation filing acceptance is invalid"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InstitutionalObservationCompatibilityError(
            "institutional observation filing acceptance lacks a timezone"
        )


def _require_equal_material(
    existing: NormalizedObservation,
    candidate: NormalizedObservation,
    existing_key: dict[str, str | None],
    candidate_key: dict[str, str | None],
) -> None:
    for name in (
        "observation_id",
        "raw_record_id",
        "asset_id",
        "field_name",
        "value",
        "unit",
        "frequency",
        "period_end",
        "available_at",
        "quality",
        "transformation_version",
    ):
        if getattr(existing, name) != getattr(candidate, name):
            raise InstitutionalObservationCompatibilityError(
                "institutional observation content conflicts"
            )
    if (
        existing.source.source_id != candidate.source.source_id
        or existing.source.raw_uri != candidate.source.raw_uri
        or existing.source.checksum_sha256 != candidate.source.checksum_sha256
    ):
        raise InstitutionalObservationCompatibilityError(
            "institutional observation content conflicts"
        )
    if any(
        existing_key[name] != candidate_key[name]
        for name in RECORD_KEY_FIELDS - {"filing_accepted_at"}
    ):
        raise InstitutionalObservationCompatibilityError(
            "institutional observation content conflicts"
        )


def _verify_legacy_acceptance(
    *,
    old_text: str,
    new_text: str,
    current: datetime,
    row: InstitutionalSemanticsRow,
) -> None:
    try:
        old = datetime.fromisoformat(old_text)
        new = datetime.fromisoformat(new_text)
    except (TypeError, ValueError) as error:
        raise InstitutionalObservationCompatibilityError(
            "institutional observation filing acceptance is invalid"
        ) from error
    if old.tzinfo is None or old.utcoffset() != timedelta(0):
        raise InstitutionalObservationCompatibilityError(
            "legacy acceptance timestamp is not UTC-labelled"
        )
    if new != current:
        raise InstitutionalObservationCompatibilityError(
            "current acceptance timestamp differs from the filing evidence"
        )
    converted = _unambiguous_new_york_utc(old)
    if converted is None or converted != new:
        raise InstitutionalObservationCompatibilityError(
            "legacy acceptance timestamp does not convert exactly to the filing evidence"
        )
    old_value = monetary_value(row.value_as_reported, accepted_at=old)
    new_value = monetary_value(row.value_as_reported, accepted_at=new)
    if old_value != new_value:
        raise InstitutionalObservationCompatibilityError(
            "legacy acceptance tolerance crosses the monetary policy threshold"
        )


def _unambiguous_new_york_utc(value: datetime) -> datetime | None:
    """Convert a UTC-labelled wall time from New York without accepting DST gaps/folds."""
    naive = value.astimezone(UTC).replace(tzinfo=None)
    candidates: set[datetime] = set()
    for fold in (0, 1):
        localized = naive.replace(tzinfo=_NEW_YORK, fold=fold)
        converted = localized.astimezone(UTC)
        if converted.astimezone(_NEW_YORK).replace(tzinfo=None) == naive:
            candidates.add(converted)
    return candidates.pop() if len(candidates) == 1 else None


__all__ = [
    "COMPATIBILITY_NOTE_ORDER",
    "RECORD_KEY_FIELDS",
    "CompatibilityNote",
    "InstitutionalObservationCompatibilityError",
    "compare_reconstruction",
    "compare_equivalent_reconstruction",
    "compare_witnessed_reconstruction",
    "parse_record_key",
]
