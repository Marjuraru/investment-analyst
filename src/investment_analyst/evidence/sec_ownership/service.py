"""Read-only ownership queries."""

from investment_analyst.evidence.sec_ownership.models import OwnershipQuery
from investment_analyst.evidence.sec_ownership.repository import OwnershipRepository
from investment_analyst.storage import StorageError


class OwnershipService:
    def __init__(self, storage, *, configuration) -> None:
        self._storage = storage
        self._configuration = configuration

    def query(self, query: OwnershipQuery):
        if not self._storage.read_only:
            raise StorageError("ownership query requires read-only storage")
        if query.asset_id != self._configuration.asset_id:
            raise StorageError("ownership query issuer is invalid")
        result = []
        for statement in OwnershipRepository(self._storage.raw_records).list(
            asset_id=query.asset_id, known_at=query.known_at
        ):
            filing = statement.document_revision.document.filing
            if query.form and statement.form != query.form:
                continue
            if query.accession and filing.accession != query.accession:
                continue
            if query.reporting_owner_cik and query.reporting_owner_cik not in {
                owner.cik for owner in statement.reporting_owners
            }:
                continue
            if query.transaction_code and query.transaction_code not in {
                entry.transaction_code for entry in statement.entries
            }:
                continue
            result.append(statement)
        return tuple(result[: query.limit])
