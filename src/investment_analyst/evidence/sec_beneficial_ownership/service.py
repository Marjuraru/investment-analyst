"""Read-only point-in-time queries for beneficial-ownership evidence."""

from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BeneficialOwnershipQuery,
    BeneficialOwnershipQueryResult,
)
from investment_analyst.evidence.sec_beneficial_ownership.repository import (
    BeneficialOwnershipRepository,
)
from investment_analyst.storage import StorageError


class BeneficialOwnershipService:
    def __init__(self, storage, *, configuration) -> None:
        self._storage = storage
        self._configuration = configuration

    def query(self, query: BeneficialOwnershipQuery) -> BeneficialOwnershipQueryResult:
        if not self._storage.read_only:
            raise StorageError("beneficial ownership query requires read-only storage")
        if query.asset_id != self._configuration.asset_id:
            raise StorageError("beneficial ownership query issuer is invalid")
        matches = [
            statement
            for statement in BeneficialOwnershipRepository(self._storage.raw_records).list(
                asset_id=query.asset_id, known_at=query.known_at
            )
            if (query.form is None or statement.form == query.form)
            and (
                query.accession is None
                or statement.document_revision.document.filing.accession == query.accession
            )
        ]
        newest_first = tuple(reversed(matches))
        return BeneficialOwnershipQueryResult(
            statements=newest_first[: query.limit],
            total_matching=len(newest_first),
            truncated=len(newest_first) > query.limit,
        )
