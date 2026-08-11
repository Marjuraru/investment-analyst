"""Point-in-time replay and read-only traceability tests."""

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from investment_analyst.analytics.crypto.derivatives_engine import CryptoDerivativesMetricEngine
from investment_analyst.analytics.crypto.derivatives_models import (
    CryptoDerivativesDiagnosticStatus,
)
from investment_analyst.analytics.crypto.derivatives_service import CryptoDerivativesService
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
_RETRIEVED = datetime(2026, 8, 4, tzinfo=UTC)
_SOURCES = (
    "deribit:btc-perpetual:book-summary",
    "deribit:btc-perpetual:funding-rate-history",
    "deribit:btc:dvol:daily",
    "deribit:eth-perpetual:book-summary",
    "deribit:eth-perpetual:funding-rate-history",
    "deribit:eth:dvol:daily",
)


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


def _query(storage: LocalStorage, known_at: datetime):
    configuration = _configuration()
    return CryptoDerivativesService(
        storage,
        CryptoDerivativesMetricEngine(),
        clock=lambda: datetime(2026, 8, 6, tzinfo=UTC),
    ).query(
        asset_id=configuration.asset_id,
        funding_source_id=configuration.funding_source_id,
        dvol_source_id=configuration.dvol_source_id,
        summary_source_id=configuration.summary_source_id,
        diagnostic_source_ids=_SOURCES,
        start=_START,
        end=_END,
        known_at=known_at,
    )


def test_backfill_visibility_replay_and_query_are_read_only(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path / "storage")
    configuration = _configuration()
    with LocalStorage(paths) as storage:
        evidence = DeribitEvidencePipeline(
            storage,
            DeribitClient(
                _Transport(
                    (_FIXTURES / "btc_funding_history.json").read_bytes(),
                    (_FIXTURES / "btc_dvol_daily.json").read_bytes(),
                    (_FIXTURES / "btc_perpetual_summary.json").read_bytes(),
                ),
                sleep=lambda _: None,
                clock=lambda: _RETRIEVED,
            ),
            configuration=configuration,
            clock=lambda: _RETRIEVED,
        )
        evidence.import_funding(_START, _END)
        evidence.import_dvol(_START, _END)
        evidence.capture_summary()

    before = sha256(paths.database_path.read_bytes()).hexdigest()
    with LocalStorage(paths, read_only=True) as storage:
        before_retrieval = _query(storage, datetime(2026, 8, 3, tzinfo=UTC))
        visible = _query(storage, datetime(2026, 8, 5, tzinfo=UTC))
        repeated = _query(storage, datetime(2026, 8, 5, tzinfo=UTC))

    assert before_retrieval.diagnostic.status is CryptoDerivativesDiagnosticStatus.INSUFFICIENT_DATA
    assert before_retrieval.coverage.metric_count == 0
    assert before_retrieval.raw_record_ids == ()
    assert visible.diagnostic.status is CryptoDerivativesDiagnosticStatus.PARTIAL
    assert visible.coverage.funding_observation_count == 2
    assert visible.coverage.dvol_observation_count == 2
    assert visible.coverage.summary_snapshot_count == 1
    assert visible.diagnostic.latest_current_funding is not None
    assert visible.diagnostic.latest_funding_8h is not None
    assert visible.diagnostic.latest_spread_bps is not None
    assert visible == repeated
    assert visible.traceability_verified
    assert sha256(paths.database_path.read_bytes()).hexdigest() == before
