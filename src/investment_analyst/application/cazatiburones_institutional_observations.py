"""Application boundary for institutional 13F observation normalization."""

from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.evidence.sec_institutional_observations.models import (
    InstitutionalObservationRequest,
    InstitutionalObservationSummary,
)
from investment_analyst.evidence.sec_institutional_observations.service import (
    InstitutionalObservationService,
)
from investment_analyst.workspace.models import WorkspaceAccessMode


class CazatiburonesInstitutionalObservationsApplication:
    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "CazatiburonesInstitutionalObservationsApplication":
        return cls(ApplicationRuntime.create_default())

    def normalize(
        self, request: InstitutionalObservationRequest, *, location: StorageLocationRequest
    ) -> InstitutionalObservationSummary:
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as storage:
            return InstitutionalObservationService(storage).normalize(request)

    def query(self, query, *, location: StorageLocationRequest):
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            return InstitutionalObservationService(storage).query(query)
