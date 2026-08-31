"""Application boundary for the read-only universe coverage matrix."""

from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.application.universe_coverage_models import (
    UniverseCoverageRequest,
    UniverseCoverageResult,
)
from investment_analyst.application.universe_coverage_service import UniverseCoverageService
from investment_analyst.workspace.models import WorkspaceAccessMode


class UniverseCoverageApplication:
    """Open exactly one explicit workspace read-only for a coverage query."""

    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "UniverseCoverageApplication":
        return cls(ApplicationRuntime.create_default())

    def query(
        self,
        location: StorageLocationRequest,
        request: UniverseCoverageRequest,
    ) -> UniverseCoverageResult:
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return UniverseCoverageService(
                storage,
                self._runtime.catalog,
                self._runtime.provider_resolver,
            ).query(request)
