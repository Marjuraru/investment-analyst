"""Offline facade integration for both catalog-backed Deribit asset families."""

from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from investment_analyst.application.crypto_derivatives_models import (
    CryptoDerivativesPlanMode,
    CryptoDerivativesQueryRequest,
    CryptoDerivativesRefreshRequest,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.providers.http import HttpResponse
from investment_analyst.storage import StoragePaths
from investment_analyst.workspace.service import WorkspaceService

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "deribit"
_KNOWN = datetime(2030, 1, 1, tzinfo=UTC)
_BEFORE_RETRIEVAL = datetime(2026, 8, 3, tzinfo=UTC)


class _FixtureTransport:
    """Route the three public method shapes to deterministic BTC/ETH fixtures."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        del timeout_seconds, max_response_bytes
        assert headers == {
            "Accept": "application/json",
            "User-Agent": "investment-analyst/0.1.0",
        }
        self.urls.append(url)
        prefix = "eth" if "ETH" in url else "btc"
        if "get_funding_rate_history" in url:
            name = f"{prefix}_funding_history.json"
        elif "get_volatility_index_data" in url:
            name = f"{prefix}_dvol_daily.json"
        elif "get_book_summary_by_instrument" in url:
            name = f"{prefix}_perpetual_summary.json"
        else:
            raise AssertionError(f"unexpected public Deribit method: {url}")
        return HttpResponse(
            status_code=200,
            body=(_FIXTURES / name).read_bytes(),
            headers={},
            url=url,
        )


def _application(home: Path, transport: _FixtureTransport) -> InvestmentAnalystApplication:
    runtime = ApplicationRuntime.create_default(
        workspace_service=WorkspaceService(environ={}, home=home)
    )
    return InvestmentAnalystApplication(runtime, transport_factory=lambda: transport)


@pytest.mark.parametrize("asset_id", ("crypto:btc-usd", "crypto:eth-usd"))
def test_facade_refresh_rerun_and_read_only_replay_are_traceable(
    tmp_path: Path,
    asset_id: str,
) -> None:
    root = tmp_path / asset_id.replace(":", "-")
    location = StorageLocationRequest(legacy_root=root)
    transport = _FixtureTransport()
    application = _application(tmp_path, transport)
    refresh = CryptoDerivativesRefreshRequest(
        asset_id=asset_id,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        known_at=_KNOWN,
    )

    first = application.refresh_crypto_derivatives(refresh, location=location)
    second = application.refresh_crypto_derivatives(refresh, location=location)

    assert first.plan.funding.mode is CryptoDerivativesPlanMode.INITIAL
    assert first.plan.dvol.mode is CryptoDerivativesPlanMode.INITIAL
    assert first.funding_stages[0].rows_received == 2
    assert first.dvol_stages[0].rows_received == 2
    assert first.summary_stage.rows_received == 1
    assert first.metric_stage.results_created > 0
    assert first.traceability_verified is True
    assert second.plan.funding.mode is CryptoDerivativesPlanMode.ALREADY_CURRENT
    assert second.plan.dvol.mode is CryptoDerivativesPlanMode.ALREADY_CURRENT
    assert second.funding_stages == second.dvol_stages == ()
    assert second.summary_stage.raw_records_reused == 1
    assert second.metric_stage.results_created == 0
    assert second.metric_stage.results_reused == first.metric_stage.results_created
    assert second.effective_known_at == first.effective_known_at == _KNOWN
    assert [url.split("?")[0].rsplit("/", 1)[-1] for url in transport.urls] == [
        "get_funding_rate_history",
        "get_volatility_index_data",
        "get_book_summary_by_instrument",
        "get_book_summary_by_instrument",
    ]

    database_path = StoragePaths.from_root(root).database_path
    before_query = database_path.read_bytes()
    before = application.query_crypto_derivatives(
        CryptoDerivativesQueryRequest(
            asset_id=asset_id,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 2),
            known_at=_BEFORE_RETRIEVAL,
        ),
        location=location,
    )
    replay = application.query_crypto_derivatives(
        CryptoDerivativesQueryRequest(
            asset_id=asset_id,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 2),
            known_at=_KNOWN,
        ),
        location=location,
    )
    repeated_replay = application.query_crypto_derivatives(
        CryptoDerivativesQueryRequest(
            asset_id=asset_id,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 2),
            known_at=_KNOWN,
        ),
        location=location,
    )

    assert before.coverage.funding_observation_count == 0
    assert before.coverage.dvol_observation_count == 0
    assert before.coverage.summary_snapshot_count == 0
    assert replay.coverage.funding_observation_count > 0
    assert replay.coverage.dvol_observation_count > 0
    assert replay.coverage.summary_snapshot_count == 1
    assert replay.metrics
    assert repeated_replay == replay
    assert len(replay.source_ids) == 6
    assert replay.traceability_verified is True
    assert database_path.read_bytes() == before_query
    assert len(transport.urls) == 4
