from investment_analyst.analytics.cazatiburones.institutional_change_service import (
    InstitutionalChangeService,
)
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.workspace.models import WorkspaceAccessMode


class CazatiburonesInstitutionalChangeApplication:
    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "CazatiburonesInstitutionalChangeApplication":
        return cls(ApplicationRuntime.create_default())

    def query(self, *, manager_cik: str, known_at, location: StorageLocationRequest):
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            return InstitutionalChangeService(storage).query(
                manager_cik=manager_cik, known_at=known_at
            )
