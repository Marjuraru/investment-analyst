"""Persistence identity and traceability tests for derivatives metrics."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from investment_analyst.analytics.crypto.derivatives_engine import CryptoDerivativesMetricEngine
from investment_analyst.analytics.crypto.derivatives_pipeline import (
    CryptoDerivativesMetricPipeline,
)
from investment_analyst.catalog.provider_configuration import resolve_deribit_configuration
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.providers.crypto.deribit import DeribitClient
from investment_analyst.providers.crypto.deribit_pipeline import DeribitEvidencePipeline
from investment_analyst.providers.http import HttpResponse
from investment_analyst.storage import LocalStorage, StoragePaths

_FIXTURES = Path(__file__).parents[3] / "fixtures" / "deribit"
_START = datetime(2026, 8, 1, tzinfo=UTC)
_END = datetime(2026, 8, 3, tzinfo=UTC)
_KNOWN = datetime(2026, 8, 5, tzinfo=UTC)


class _Transport:
    def __init__(self, *bodies: bytes) -> None:
        self._bodies = list(bodies)

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        del headers, timeout_seconds, max_response_bytes
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


def test_equivalent_recomputation_reuses_original_computed_at(tmp_path: Path) -> None:
    configuration = _configuration()
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        evidence = DeribitEvidencePipeline(
            storage,
            DeribitClient(
                _Transport(
                    (_FIXTURES / "btc_funding_history.json").read_bytes(),
                    (_FIXTURES / "btc_dvol_daily.json").read_bytes(),
                    (_FIXTURES / "btc_perpetual_summary.json").read_bytes(),
                ),
                sleep=lambda _: None,
                clock=lambda: datetime(2026, 8, 4, tzinfo=UTC),
            ),
            configuration=configuration,
            clock=lambda: datetime(2026, 8, 4, tzinfo=UTC),
        )
        evidence.import_funding(_START, _END)
        evidence.import_dvol(_START, _END)
        evidence.capture_summary()
        first = CryptoDerivativesMetricPipeline(
            storage,
            CryptoDerivativesMetricEngine(),
            clock=lambda: _KNOWN,
        ).run(
            asset_id=configuration.asset_id,
            funding_source_id=configuration.funding_source_id,
            dvol_source_id=configuration.dvol_source_id,
            summary_source_id=configuration.summary_source_id,
            known_at=_KNOWN,
            as_of_from=_START,
            as_of_before=_END,
        )
        repeated = CryptoDerivativesMetricPipeline(
            storage,
            CryptoDerivativesMetricEngine(),
            clock=lambda: _KNOWN + timedelta(days=1),
        ).run(
            asset_id=configuration.asset_id,
            funding_source_id=configuration.funding_source_id,
            dvol_source_id=configuration.dvol_source_id,
            summary_source_id=configuration.summary_source_id,
            known_at=_KNOWN,
            as_of_from=_START,
            as_of_before=_END,
        )

        assert first.results_created == len(first.results) > 0
        assert first.results_reused == 0
        assert repeated.results_created == 0
        assert repeated.results_reused == len(first.results)
        assert repeated.results == first.results
        assert all(result.computed_at == _KNOWN for result in repeated.results)
        assert repeated.traceability_verified
