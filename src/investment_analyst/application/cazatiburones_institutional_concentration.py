"""Application boundary for read-only declared 13F concentration queries."""

from datetime import datetime

from investment_analyst.analytics.cazatiburones.institutional_concentration_models import (
    InstitutionalConcentrationResult,
)
from investment_analyst.analytics.cazatiburones.institutional_concentration_service import (
    InstitutionalConcentrationService,
)
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.workspace.models import WorkspaceAccessMode


class CazatiburonesInstitutionalConcentrationApplication:
    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "CazatiburonesInstitutionalConcentrationApplication":
        return cls(ApplicationRuntime.create_default())

    def query(
        self, *, manager_cik: str, known_at: datetime, location: StorageLocationRequest
    ) -> tuple[InstitutionalConcentrationResult, ...]:
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            return InstitutionalConcentrationService(storage).query(
                manager_cik=manager_cik, known_at=known_at
            )
