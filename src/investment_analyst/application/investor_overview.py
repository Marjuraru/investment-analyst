"""Application boundary for the read-only investor overview."""

from investment_analyst.application.investor_overview_models import (
    InvestorOverviewRequest,
    InvestorOverviewResult,
)
from investment_analyst.application.investor_overview_service import InvestorOverviewService
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.workspace.models import WorkspaceAccessMode


class InvestorOverviewApplication:
    """Open exactly one explicit workspace read-only per overview query."""

    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    def query(
        self,
        location: StorageLocationRequest,
        request: InvestorOverviewRequest,
    ) -> InvestorOverviewResult:
        with self._runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            return InvestorOverviewService(
                storage,
                self._runtime.catalog,
                self._runtime.provider_resolver,
            ).query(request)
