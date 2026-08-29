"""Read-only projection of consecutive institutional closes."""

from investment_analyst.analytics.cazatiburones.institutional_change_engine import compare
from investment_analyst.analytics.cazatiburones.institutional_change_models import (
    InstitutionalClose,
    InstitutionalPosition,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
)
from investment_analyst.storage import StorageError
from investment_analyst.storage.local import LocalStorage


class InstitutionalChangeService:
    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage

    def query(self, *, manager_cik: str, known_at):
        if not self._storage.read_only:
            raise StorageError("institutional change query requires read-only storage")
        repository = InstitutionalHoldingsRepository(self._storage.raw_records)
        reports = [
            report
            for report in repository.list_reports(manager_cik=manager_cik, known_at=known_at)
            if report.report_period is not None
        ]
        by_period = {}
        for report in reports:
            existing = by_period.get(report.report_period)
            if existing is None or report.available_at > existing.available_at:
                by_period[report.report_period] = report
        closes = []
        for report in sorted(by_period.values(), key=lambda item: item.report_period):
            positions = repository.list_positions(report_ids={report.report_id}, known_at=known_at)
            closes.append(
                InstitutionalClose(
                    manager_cik=report.manager_cik,
                    report_period=report.report_period,
                    available_at=report.available_at,
                    declared_value_total=report.declared_value_total,
                    positions=tuple(
                        InstitutionalPosition(
                            cusip=position.cusip,
                            title_of_class=position.title_of_class,
                            quantity=position.quantity,
                            value=position.value,
                        )
                        for position in positions
                    ),
                )
            )
        if len(closes) < 2:
            return ()
        return tuple(
            compare(previous, current)
            for previous, current in zip(closes, closes[1:], strict=False)
        )
