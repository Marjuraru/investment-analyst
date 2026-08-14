"""Ordered refresh, idempotence, and partial-progress application tests."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from investment_analyst.application.crypto_derivatives import CryptoDerivativesRefreshService
from investment_analyst.application.crypto_derivatives_models import (
    CryptoDerivativesPlanMode,
    CryptoDerivativesRefreshRequest,
)
from investment_analyst.catalog.provider_configuration import resolve_deribit_configuration
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.providers.crypto.deribit import DeribitClient, DeribitError
from investment_analyst.providers.http import HttpResponse
from investment_analyst.storage import LocalStorage, StoragePaths

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "deribit"
_KNOWN = datetime(2026, 8, 10, tzinfo=UTC)


class _Transport:
    def __init__(self, *bodies: bytes) -> None:
        self._bodies = list(bodies)
        self.urls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        del headers, timeout_seconds, max_response_bytes
        self.urls.append(url)
        if not self._bodies:
            raise AssertionError("unexpected Deribit request")
        return HttpResponse(
            status_code=200,
            body=self._bodies.pop(0),
            headers={},
            url=url,
        )


def _configuration():
    return resolve_deribit_configuration(
        ProviderAssetContextResolver(AssetCatalogService.load_default()),
        asset_id="crypto:btc-usd",
    )


def _service(
    storage: LocalStorage,
    transport: _Transport,
    retrieved_at: datetime,
) -> CryptoDerivativesRefreshService:
    return CryptoDerivativesRefreshService(
        storage,
        DeribitClient(
            transport,
            sleep=lambda _: None,
            clock=lambda: retrieved_at,
        ),
        configuration=_configuration(),
        clock=lambda: retrieved_at,
    )


def _request() -> CryptoDerivativesRefreshRequest:
    return CryptoDerivativesRefreshRequest(
        asset_id="crypto:btc-usd",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        known_at=_KNOWN,
    )


def test_second_auto_run_skips_history_captures_summary_and_reuses_metrics(
    tmp_path: Path,
) -> None:
    funding = (_FIXTURES / "btc_funding_history.json").read_bytes()
    dvol = (_FIXTURES / "btc_dvol_daily.json").read_bytes()
    summary = (_FIXTURES / "btc_perpetual_summary.json").read_bytes()
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        first_transport = _Transport(funding, dvol, summary)
        first = _service(
            storage,
            first_transport,
            datetime(2026, 8, 4, tzinfo=UTC),
        ).run(_request())
        second_transport = _Transport(summary)
        second = _service(
            storage,
            second_transport,
            datetime(2026, 8, 5, tzinfo=UTC),
        ).run(_request())

        assert first.plan.funding.mode is CryptoDerivativesPlanMode.INITIAL
        assert second.plan.funding.mode is CryptoDerivativesPlanMode.ALREADY_CURRENT
        assert second.plan.dvol.mode is CryptoDerivativesPlanMode.ALREADY_CURRENT
        assert second.funding_stages == second.dvol_stages == ()
        assert len(second_transport.urls) == 1
        assert "get_book_summary_by_instrument" in second_transport.urls[0]
        assert second.summary_stage.raw_records_reused == 1
        assert second.metric_stage.results_created == 0
        assert second.metric_stage.results_reused == first.metric_stage.results_created
        assert first.effective_known_at == second.effective_known_at == _KNOWN
        assert first.traceability_verified and second.traceability_verified


def test_summary_failure_preserves_completed_historical_stages(tmp_path: Path) -> None:
    funding = (_FIXTURES / "btc_funding_history.json").read_bytes()
    dvol = (_FIXTURES / "btc_dvol_daily.json").read_bytes()
    invalid_summary = b'{"jsonrpc":"2.0","result":[]}'
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        with pytest.raises(DeribitError, match="exactly one"):
            _service(
                storage,
                _Transport(funding, dvol, invalid_summary),
                datetime(2026, 8, 4, tzinfo=UTC),
            ).run(_request())

        configuration = _configuration()
        assert storage.raw_records.list(source_id=configuration.funding_source_id)
        assert storage.raw_records.list(source_id=configuration.dvol_source_id)
        assert storage.raw_records.list(source_id=configuration.summary_source_id) == []
