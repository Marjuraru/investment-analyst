"""Read-only point-in-time queries for institutional holdings evidence."""

from investment_analyst.evidence.sec_institutional_holdings.models import (
    InstitutionalHoldingsQuery,
    InstitutionalHoldingsQueryResult,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
)
from investment_analyst.storage import StorageError


class InstitutionalHoldingsService:
    def __init__(self, storage) -> None:
        self._storage = storage

    def query(self, query: InstitutionalHoldingsQuery) -> InstitutionalHoldingsQueryResult:
        if not self._storage.read_only:
            raise StorageError("institutional holdings query requires read-only storage")
        repository = InstitutionalHoldingsRepository(self._storage.raw_records)
        matches = [
            report
            for report in repository.list_reports(
                manager_cik=query.manager_cik, known_at=query.known_at
            )
            if (
                query.period_from is None
                or report.report_period is not None
                and report.report_period >= query.period_from
            )
            and (
                query.period_to is None
                or report.report_period is not None
                and report.report_period <= query.period_to
            )
        ]
        newest_first = tuple(reversed(matches))
        reports = newest_first[: query.limit]
        positions = tuple(
            repository.list_positions(
                report_ids={report.report_id for report in reports}, known_at=query.known_at
            )
        )
        return InstitutionalHoldingsQueryResult(
            reports=reports,
            positions=positions,
            total_matching=len(newest_first),
            truncated=len(newest_first) > query.limit,
        )
