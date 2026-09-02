"""Read-only declared concentration projection over semantic 13F artifacts."""

from datetime import date, datetime
from uuid import UUID

from investment_analyst.analytics.cazatiburones.institutional_close_totals import (
    effective_close_total,
)
from investment_analyst.analytics.cazatiburones.institutional_composition_candidates import (
    candidates_by_period,
)
from investment_analyst.analytics.cazatiburones.institutional_composition_engine import resolve
from investment_analyst.analytics.cazatiburones.institutional_composition_models import (
    InstitutionalCompositionCandidate,
)
from investment_analyst.analytics.cazatiburones.institutional_concentration_engine import calculate
from investment_analyst.analytics.cazatiburones.institutional_concentration_models import (
    InstitutionalConcentrationInput,
    InstitutionalConcentrationPosition,
    InstitutionalConcentrationResult,
)
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_semantics.artifact_reader import (
    InstitutionalSemanticsArtifactReader,
)
from investment_analyst.evidence.sec_institutional_semantics.models import (
    InstitutionalHoldingsSemantics,
)
from investment_analyst.storage import LocalStorage, StorageError


class InstitutionalConcentrationService:
    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage

    def query(
        self, *, manager_cik: str, known_at: datetime
    ) -> tuple[InstitutionalConcentrationResult, ...]:
        if not self._storage.read_only:
            raise StorageError("institutional concentration query requires read-only storage")
        normalized_manager_cik = normalize_cik(manager_cik)
        artifacts = tuple(
            item
            for item in InstitutionalSemanticsArtifactReader(
                self._storage.raw_records
            ).list_visible(known_at=known_at)
            if item.manager_cik == normalized_manager_cik
        )
        by_artifact_id = {item.artifact_id: item for item in artifacts}
        periods = candidates_by_period(artifacts, manager_cik=normalized_manager_cik)
        return tuple(
            self._calculate_period(
                manager_cik=normalized_manager_cik,
                known_at=known_at,
                artifacts=by_artifact_id,
                report_period=report_period,
                candidates=candidates,
            )
            for report_period, candidates in sorted(
                periods.items(), key=lambda item: (item[0] is None, item[0])
            )
        )

    @staticmethod
    def _calculate_period(
        *,
        manager_cik: str,
        known_at: datetime,
        artifacts: dict[UUID, InstitutionalHoldingsSemantics],
        report_period: date | None,
        candidates: tuple[InstitutionalCompositionCandidate, ...],
    ) -> InstitutionalConcentrationResult:
        composition = resolve(
            manager_cik=manager_cik,
            report_period=report_period,
            known_at=known_at,
            candidates=candidates,
        )
        artifact = (
            artifacts.get(composition.effective_artifact_id)
            if composition.effective_artifact_id is not None
            else None
        )
        total, quality = effective_close_total(artifact) if artifact is not None else (None, None)
        return calculate(
            InstitutionalConcentrationInput(
                manager_cik=manager_cik,
                report_period=composition.report_period,
                known_at=known_at,
                close_status=composition.status,
                close_reason=composition.reason,
                effective_artifact_id=composition.effective_artifact_id,
                effective_accession=composition.effective_accession,
                accepted_at=(
                    artifact.cover_revision.document.filing.accepted_at
                    if artifact is not None
                    else None
                ),
                effective_close_total=total,
                total_quality=quality,
                positions=(
                    tuple(
                        InstitutionalConcentrationPosition(
                            cusip=row.cusip,
                            title_of_class=row.title_of_class,
                            put_call=row.put_call,
                            value_as_reported=row.value_as_reported,
                        )
                        for row in artifact.rows
                    )
                    if artifact is not None
                    else ()
                ),
            )
        )
