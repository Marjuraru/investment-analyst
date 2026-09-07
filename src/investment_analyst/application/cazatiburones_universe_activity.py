"""Application boundary for the read-only Cazatiburones universe activity index."""

from investment_analyst.application.cazatiburones_universe_activity_models import (
    CazatiburonesUniverseActivityRequest,
    CazatiburonesUniverseActivityResult,
)
from investment_analyst.application.cazatiburones_universe_activity_service import (
    CazatiburonesUniverseActivityService,
)
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.workspace.models import WorkspaceAccessMode


class CazatiburonesUniverseActivityApplication:
    """Open exactly one explicit workspace read-only for an activity query."""

    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "CazatiburonesUniverseActivityApplication":
        return cls(ApplicationRuntime.create_default())

    def query(
        self,
        location: StorageLocationRequest,
        request: CazatiburonesUniverseActivityRequest,
    ) -> CazatiburonesUniverseActivityResult:
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return CazatiburonesUniverseActivityService(
                storage,
                self._runtime.catalog,
                self._runtime.provider_resolver,
            ).query(request)
