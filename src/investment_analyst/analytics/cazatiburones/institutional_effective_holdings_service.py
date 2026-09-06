"""Read-only public-effective Form 13F holdings projection."""

from datetime import date, datetime

from investment_analyst.analytics.cazatiburones.institutional_effective_holdings_engine import (
    compose,
)
from investment_analyst.analytics.cazatiburones.institutional_effective_holdings_models import (
    InstitutionalEffectiveHoldingsQuery,
    InstitutionalEffectiveHoldingsResult,
)
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_semantics.artifact_reader import (
    InstitutionalSemanticsArtifactReader,
)
from investment_analyst.storage import LocalStorage, StorageError


class InstitutionalEffectiveHoldingsService:
    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage

    def query(
        self,
        *,
        manager_cik: str,
        report_period: date,
        known_at: datetime,
        offset: int = 0,
        limit: int = 1000,
    ) -> InstitutionalEffectiveHoldingsResult:
        if not self._storage.read_only:
            raise StorageError("institutional effective holdings query requires read-only storage")
        query = InstitutionalEffectiveHoldingsQuery(
            manager_cik=normalize_cik(manager_cik),
            report_period=report_period,
            known_at=known_at,
            offset=offset,
            limit=limit,
        )
        artifacts = tuple(
            item
            for item in InstitutionalSemanticsArtifactReader(
                self._storage.raw_records
            ).list_visible(known_at=known_at)
            if item.manager_cik == query.manager_cik and item.report_period == report_period
        )
        return compose(query=query, artifacts=artifacts)
