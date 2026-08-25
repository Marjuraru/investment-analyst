"""Read-only generic listed-company report assembly."""

from investment_analyst.analytics.aapl_daily_report_service import AaplDailyReportService
from investment_analyst.analytics.listed_company_report_models import (
    ListedCompanyDiagnosticReport,
    ListedCompanyReportAsset,
    ListedCompanyReportRequest,
)
from investment_analyst.storage import LocalStorage


class ListedCompanyReportService:
    """Adapt the established PIT selection and metric presentation to any eligible issuer."""

    def __init__(self, storage: LocalStorage) -> None:
        storage.require_open()
        self._storage = storage

    def query(
        self,
        request: ListedCompanyReportRequest,
        *,
        symbol: str,
        name: str,
        source_ids: tuple[str, ...],
    ) -> ListedCompanyDiagnosticReport:
        """Build a generic report from persisted evidence only."""
        daily = AaplDailyReportService(self._storage).query(request)
        return ListedCompanyDiagnosticReport(
            asset=ListedCompanyReportAsset(
                asset_id=request.asset_id,
                symbol=symbol,
                name=name,
                source_ids=source_ids,
            ),
            view=daily.view,
            market=daily.market,
            fundamental=daily.fundamental,
        )
