"""Application boundary for read-only 13F composition queries."""

from datetime import datetime

from investment_analyst.analytics.cazatiburones.institutional_composition_models import (
    InstitutionalCompositionResult,
)
from investment_analyst.analytics.cazatiburones.institutional_composition_service import (
    InstitutionalCompositionService,
)
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.workspace.models import WorkspaceAccessMode


class CazatiburonesInstitutionalCompositionApplication:
    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "CazatiburonesInstitutionalCompositionApplication":
        return cls(ApplicationRuntime.create_default())

    def query(
        self, *, manager_cik: str, known_at: datetime, location: StorageLocationRequest
    ) -> tuple[InstitutionalCompositionResult, ...]:
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            return InstitutionalCompositionService(storage).query(
                manager_cik=manager_cik, known_at=known_at
            )
