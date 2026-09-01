"""PIT persistence for linked institutional observations."""

from collections import Counter
from datetime import UTC, datetime

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
from .models import InstitutionalObservationRequest, InstitutionalObservationSummary
from .normalizer import normalize_row


class InstitutionalObservationService:
    def __init__(self, storage, *, clock=lambda: datetime.now(UTC)) -> None:
        self._storage = storage
        self._clock = clock

    def normalize(
        self, request: InstitutionalObservationRequest
    ) -> InstitutionalObservationSummary:
        if self._storage.read_only:
            raise StorageError("institutional observation normalization requires writable storage")
        now = self._clock().astimezone(UTC)
        holdings = InstitutionalHoldingsRepository(self._storage.raw_records)
        semantics = InstitutionalSemanticsRepository(self._storage.raw_records)
        correspondences = InstrumentCorrespondenceRepository(self._storage.raw_records).list(
            known_at=request.known_at, asset_id=request.asset_id
        )
        created = reused = rows = generated = 0
        skips: Counter[str] = Counter()
        for report_id in sorted(request.report_ids, key=str):
            parent = holdings.get_report(report_id)
            if (
                parent is None
                or parent.manager_cik != request.manager_cik
                or parent.available_at > request.known_at
            ):
                skips["missing_report"] += 1
                continue
            item = semantics.get_for_parent(parent)
            if item is None:
                skips["not_enriched"] += 1
                continue
            for row in item.rows:
                rows += 1
                exact = [
                    c
                    for c in correspondences
                    if c.cusip == row.cusip
                    and c.title_of_class == row.title_of_class
                    and c.is_effective_on(item.report_period)
                ]
                if len(exact) != 1:
                    skips[
                        "ambiguous_correspondence" if len(exact) > 1 else "missing_correspondence"
                    ] += 1
                    continue
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
            rows_examined=rows,
            observations_generated=generated,
            observations_created=created,
            observations_reused=reused,
            skipped_by_reason=dict(skips),
        )

    def query(self, *, asset_id: str, known_at: datetime):
        if not self._storage.read_only:
            raise StorageError("institutional observation query requires read-only storage")
        return tuple(
            self._storage.observations.list(
                asset_id=asset_id, source_id=SOURCE_ID, available_to=known_at
            )
        )
