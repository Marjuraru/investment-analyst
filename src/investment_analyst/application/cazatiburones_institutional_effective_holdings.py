"""Application boundary for effective public Form 13F holdings."""

from datetime import date, datetime

from investment_analyst.analytics.cazatiburones.institutional_effective_holdings_models import (
    InstitutionalEffectiveHoldingsResult,
)
from investment_analyst.analytics.cazatiburones.institutional_effective_holdings_service import (
    InstitutionalEffectiveHoldingsService,
)
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.workspace.models import WorkspaceAccessMode


class CazatiburonesInstitutionalEffectiveHoldingsApplication:
    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "CazatiburonesInstitutionalEffectiveHoldingsApplication":
        return cls(ApplicationRuntime.create_default())

    def query(
        self,
        *,
        manager_cik: str,
        report_period: date,
        known_at: datetime,
        offset: int,
        limit: int,
        location: StorageLocationRequest,
    ) -> InstitutionalEffectiveHoldingsResult:
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            return InstitutionalEffectiveHoldingsService(storage).query(
                manager_cik=manager_cik,
                report_period=report_period,
                known_at=known_at,
                offset=offset,
                limit=limit,
            )
