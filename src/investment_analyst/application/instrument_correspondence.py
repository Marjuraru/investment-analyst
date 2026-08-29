from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.catalog.provider_configuration import resolve_sec_cusip_binding
from investment_analyst.evidence.instrument_correspondence.models import InstrumentCorrespondence
from investment_analyst.evidence.instrument_correspondence.repository import (
    InstrumentCorrespondenceRepository,
)
from investment_analyst.evidence.instrument_correspondence.service import (
    InstrumentCorrespondenceQuery,
    InstrumentCorrespondenceQueryResult,
    InstrumentCorrespondenceService,
)
from investment_analyst.storage import StorageError
from investment_analyst.workspace.models import WorkspaceAccessMode


class InstrumentCorrespondenceApplication:
    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "InstrumentCorrespondenceApplication":
        return cls(ApplicationRuntime.create_default())

    @property
    def catalog_version(self) -> int:
        return self._runtime.catalog.catalog_version

    def declare_correspondence(
        self,
        *,
        correspondence: InstrumentCorrespondence,
        catalog_version: int,
        declared_by: str,
        location: StorageLocationRequest,
    ) -> InstrumentCorrespondence:
        if correspondence.cusip != resolve_sec_cusip_binding(
            self._runtime.provider_resolver, asset_id=correspondence.asset_id
        ):
            raise StorageError("instrument correspondence CUSIP conflicts with the asset catalog")
        if catalog_version != self.catalog_version:
            raise StorageError("instrument correspondence catalog version is invalid")
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as storage:
            return InstrumentCorrespondenceRepository(storage.raw_records).save(
                correspondence, catalog_version=catalog_version, declared_by=declared_by
            )

    def query_institutional_holdings_by_asset(
        self, *, query: InstrumentCorrespondenceQuery, location: StorageLocationRequest
    ) -> InstrumentCorrespondenceQueryResult:
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            return InstrumentCorrespondenceService(storage).query(query)
