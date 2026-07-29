"""Integration tests for FRED/ALFRED through the stable application facade."""

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.macro.fred_alfred import FredApiKey
from investment_analyst.providers.macro.fred_point_in_time import FredPointInTimeQuery
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.service import WorkspaceService

FIXTURE = Path("tests/fixtures/fred/gdp_vintage_2020-01-15.json").read_bytes()


class FixtureTransport:
    """Return one official-shaped offline response and count provider calls."""

    def __init__(self) -> None:
        self.calls = 0

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        self.calls += 1
        return HttpResponse(200, FIXTURE, {}, url)


def test_facade_imports_and_queries_one_macro_vintage_without_asset_coupling(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy-fred"
    with LocalStorage(StoragePaths.from_root(root)):
        pass
    transport = FixtureTransport()
    application = InvestmentAnalystApplication(
        ApplicationRuntime.create_default(
            workspace_service=WorkspaceService(environ={}, home=tmp_path),
        ),
        transport_factory=lambda: transport,
    )
    location = StorageLocationRequest(legacy_root=root)

    summary = application.refresh_fred_vintage(
        "GDP",
        vintage_date=date(2020, 1, 15),
        observation_start=date(2019, 1, 1),
        observation_end=date(2020, 1, 1),
        location=location,
        api_key=FredApiKey("e" * 32),
    )
    result = application.query_fred_point_in_time(
        FredPointInTimeQuery(
            series_id="GDP",
            known_at=datetime(2020, 1, 16, tzinfo=UTC),
        ),
        location=location,
    )

    assert summary.raw_records_created == 1
    assert summary.traceability_verified is True
    assert transport.calls == 1
    assert [item.value for item in result.observations] == [
        Decimal("100.125"),
        None,
    ]
    assert result.traceability_verified is True
    with LocalStorage(StoragePaths.from_root(root), read_only=True) as storage:
        assert storage.assets.list_all() == []
        assert storage.observations.list() == []
        assert storage.metric_results.list() == []
        assert storage.diagnostics.list() == []
