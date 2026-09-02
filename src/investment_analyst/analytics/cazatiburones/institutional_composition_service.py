"""Read-only projection over persisted semantic 13F artifacts."""

from datetime import datetime

from investment_analyst.analytics.cazatiburones.institutional_composition_candidates import (
    candidates_by_period,
)
from investment_analyst.analytics.cazatiburones.institutional_composition_engine import resolve
from investment_analyst.analytics.cazatiburones.institutional_composition_models import (
    InstitutionalCompositionResult,
)
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_semantics.artifact_reader import (
    InstitutionalSemanticsArtifactReader,
)
from investment_analyst.storage import LocalStorage, StorageError


class InstitutionalCompositionService:
    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage

    def query(
        self, *, manager_cik: str, known_at: datetime
    ) -> tuple[InstitutionalCompositionResult, ...]:
        if not self._storage.read_only:
            raise StorageError("institutional composition query requires read-only storage")
        normalized_manager_cik = normalize_cik(manager_cik)
        artifacts = [
            item
            for item in InstitutionalSemanticsArtifactReader(
                self._storage.raw_records
            ).list_visible(known_at=known_at)
            if item.manager_cik == normalized_manager_cik
        ]
        periods = candidates_by_period(tuple(artifacts), manager_cik=normalized_manager_cik)
        return tuple(
            resolve(
                manager_cik=normalized_manager_cik,
                report_period=period,
                known_at=known_at,
                candidates=candidates,
            )
            for period, candidates in sorted(
                periods.items(), key=lambda item: (item[0] is None, item[0])
            )
        )
