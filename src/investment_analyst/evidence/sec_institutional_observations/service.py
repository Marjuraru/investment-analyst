"""PIT persistence for linked institutional observations."""

import json
from collections import Counter
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from investment_analyst.evidence.instrument_correspondence.repository import (
    InstrumentCorrespondenceRepository,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    InstitutionalSemanticsRepository,
)
from investment_analyst.storage import RecordNotFoundError, StorageError

from .definitions import SOURCE_ID
from .models import (
    InstitutionalObservationQuery,
    InstitutionalObservationQueryResult,
    InstitutionalObservationRequest,
    InstitutionalObservationSummary,
    InstitutionalObservationView,
)
from .normalizer import normalize_row

_RECORD_KEY_FIELDS = frozenset(
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


class InstitutionalObservationLineageError(StorageError):
    """An observation cannot be reconstructed to its immutable parents."""


class InstitutionalObservationService:
    def __init__(self, storage, *, clock=lambda: datetime.now(UTC)) -> None:
        self._storage = storage
        self._clock = clock

    def normalize(
        self, request: InstitutionalObservationRequest
    ) -> InstitutionalObservationSummary:
        if self._storage.read_only:
            raise StorageError("institutional observation normalization requires writable storage")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise StorageError("institutional observation clock must include timezone")
        now = now.astimezone(UTC)
        holdings = InstitutionalHoldingsRepository(self._storage.raw_records)
        semantics = InstitutionalSemanticsRepository(self._storage.raw_records)
        correspondences = InstrumentCorrespondenceRepository(self._storage.raw_records).list(
            known_at=request.known_at, asset_id=request.asset_id
        )
        created = reused = rows = linked = unlinked = values = generated = 0
        reports_missing = reports_not_enriched = 0
        skips: Counter[str] = Counter()
        for report_id in sorted(request.report_ids, key=str):
            parent = holdings.get_report(report_id)
            if (
                parent is None
                or parent.manager_cik != request.manager_cik
                or parent.available_at > request.known_at
            ):
                reports_missing += 1
                skips["missing_report"] += 1
                continue
            item = semantics.get_for_parent(parent)
            if item is None:
                reports_not_enriched += 1
                skips["not_enriched"] += 1
                continue
            for row in item.rows:
                rows += 1
                if item.report_period is None:
                    unlinked += 1
                    skips["missing_report_period"] += 1
                    continue
                same_cusip = [c for c in correspondences if c.cusip == row.cusip]
                exact = [
                    c
                    for c in correspondences
                    if c.cusip == row.cusip
                    and c.title_of_class == row.title_of_class
                    and c.is_effective_on(item.report_period)
                ]
                if len(exact) != 1:
                    if len(exact) > 1:
                        reason = "ambiguous_correspondence"
                    elif any(c.is_effective_on(item.report_period) for c in same_cusip):
                        reason = "class_mismatch"
                    elif same_cusip:
                        reason = "outside_effective_period"
                    else:
                        reason = "missing_correspondence"
                    unlinked += 1
                    skips[reason] += 1
                    continue
                linked += 1
                values += 1
                candidates = normalize_row(item, row, exact[0], normalized_at=now)
                if not candidates:
                    skips["unsupported_row"] += 1
                for candidate in candidates:
                    generated += 1
                    try:
                        existing = self._storage.observations.get(candidate.observation_id)
                    except RecordNotFoundError:
                        self._storage.observations.save(candidate)
                        created += 1
                    else:
                        if (
                            existing.model_copy(update={"normalized_at": candidate.normalized_at})
                            != candidate
                        ):
                            raise StorageError("institutional observation identity conflicts")
                        reused += 1
        return InstitutionalObservationSummary(
            asset_id=request.asset_id,
            known_at=request.known_at,
            normalized_at=now,
            reports_examined=len(request.report_ids),
            reports_missing=reports_missing,
            reports_not_enriched=reports_not_enriched,
            rows_examined=rows,
            rows_linked=linked,
            rows_unlinked=unlinked,
            values_examined=values,
            observations_generated=generated,
            observations_created=created,
            observations_reused=reused,
            skipped_by_reason=dict(skips),
        )

    def query(self, query: InstitutionalObservationQuery) -> InstitutionalObservationQueryResult:
        if not self._storage.read_only:
            raise StorageError("institutional observation query requires read-only storage")
        observations = tuple(
            self._storage.observations.list(
                asset_id=query.asset_id,
                source_id=SOURCE_ID,
                field_name=query.field_name,
                available_to=query.known_at,
            )
        )
        keys = {item.observation_id: _record_key(item) for item in observations}
        artifact_ids = {UUID(key["artifact_id"]) for key in keys.values()}
        correspondence_ids = {UUID(key["correspondence_id"]) for key in keys.values()}
        report_ids = {UUID(key["report_id"]) for key in keys.values()}
        artifacts = _resolve_all(
            artifact_ids,
            InstitutionalSemanticsRepository(self._storage.raw_records).get,
            "artifact",
        )
        correspondences = _resolve_all(
            correspondence_ids,
            InstrumentCorrespondenceRepository(self._storage.raw_records).get,
            "correspondence",
        )
        reports = _resolve_all(
            report_ids,
            InstitutionalHoldingsRepository(self._storage.raw_records).get_report,
            "report",
        )
        verified = tuple(
            _verified_view(
                observation,
                keys[observation.observation_id],
                artifacts,
                reports,
                correspondences,
            )
            for observation in observations
        )
        matching = tuple(
            view
            for view in verified
            if (query.report_id is None or view.report.report_id == query.report_id)
            and (query.manager_cik is None or view.artifact.manager_cik == query.manager_cik)
            and (query.cusip is None or view.row.cusip == query.cusip)
        )
        page = matching[query.offset : query.offset + query.limit]
        return InstitutionalObservationQueryResult(
            observations=page,
            total_matching=len(matching),
            truncated=query.offset + len(page) < len(matching),
        )


def _record_key(observation) -> dict[str, str | None]:
    if observation.source.source_id != SOURCE_ID:
        raise InstitutionalObservationLineageError("institutional observation source is invalid")
    try:
        parsed = json.loads(observation.source.record_key)
    except (TypeError, json.JSONDecodeError) as error:
        raise InstitutionalObservationLineageError(
            "institutional observation record_key is invalid"
        ) from error
    if not isinstance(parsed, dict) or set(parsed) != _RECORD_KEY_FIELDS:
        raise InstitutionalObservationLineageError(
            "institutional observation record_key is incomplete"
        )
    required_strings = _RECORD_KEY_FIELDS - {"put_call", "quantity_type"}
    nullable_strings = {"put_call", "quantity_type"}
    if any(not isinstance(parsed[name], str) for name in required_strings) or any(
        parsed[name] is not None and not isinstance(parsed[name], str) for name in nullable_strings
    ):
        raise InstitutionalObservationLineageError(
            "institutional observation record_key has invalid types"
        )
    if json.dumps(parsed, sort_keys=True, separators=(",", ":")) != observation.source.record_key:
        raise InstitutionalObservationLineageError(
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
        raise InstitutionalObservationLineageError(
            "institutional observation record_key has invalid IDs"
        ) from error
    return cast(dict[str, str | None], parsed)


def _resolve_all(ids, resolver, kind: str):
    resolved = {}
    for identifier in ids:
        item = resolver(identifier)
        if item is None:
            raise InstitutionalObservationLineageError(
                f"institutional observation {kind} is missing"
            )
        resolved[identifier] = item
    return resolved


def _verified_view(observation, key, artifacts, reports, correspondences):
    artifact = artifacts[UUID(key["artifact_id"])]
    report = reports[UUID(key["report_id"])]
    correspondence = correspondences[UUID(key["correspondence_id"])]
    row = next((item for item in artifact.rows if str(item.row_id) == key["row_id"]), None)
    if row is None:
        raise InstitutionalObservationLineageError("institutional observation row is missing")
    if (
        observation.raw_record_id != artifact.raw_record_id
        or observation.asset_id != correspondence.asset_id
        or artifact.parent_report_id != report.report_id
        or artifact.manager_cik != report.manager_cik
        or artifact.cover_revision != report.cover_revision
        or artifact.information_table_revision != report.information_table_revision
        or key["report_id"] != str(report.report_id)
        or key["manager_cik"] != artifact.manager_cik
        or key["correspondence_id"] != str(correspondence.correspondence_id)
        or key["cover_revision_id"] != str(artifact.cover_revision.revision_id)
        or key["cover_content_sha256"] != artifact.cover_revision.content_sha256
        or key["information_table_revision_id"]
        != str(artifact.information_table_revision.revision_id)
        or key["information_table_content_sha256"]
        != artifact.information_table_revision.content_sha256
        or key["cusip"] != row.cusip
        or key["title_of_class"] != row.title_of_class
        or key["put_call"] != row.put_call
        or key["quantity_type"] != row.quantity_type
    ):
        raise InstitutionalObservationLineageError("institutional observation lineage conflicts")
    expected = {
        candidate.field_name: candidate
        for candidate in normalize_row(
            artifact, row, correspondence, normalized_at=observation.normalized_at
        )
    }.get(observation.field_name)
    if expected is None or expected != observation or key["field_name"] != observation.field_name:
        raise InstitutionalObservationLineageError("institutional observation content conflicts")
    return InstitutionalObservationView(
        observation=observation,
        report=report,
        artifact=artifact,
        row=row,
        correspondence=correspondence,
    )
