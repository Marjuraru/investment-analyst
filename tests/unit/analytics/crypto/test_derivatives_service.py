"""Point-in-time replay and read-only traceability tests."""

from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from investment_analyst.analytics.crypto.derivatives_engine import (
    DVOL_CHANGE_KEY,
    FUNDING_SUM_KEY,
    SPREAD_BPS_KEY,
    CryptoDerivativesMetricEngine,
)
from investment_analyst.analytics.crypto.derivatives_identity import metric_result_id
from investment_analyst.analytics.crypto.derivatives_models import (
    CryptoDerivativesDiagnosticStatus,
)
from investment_analyst.analytics.crypto.derivatives_pipeline import (
    CryptoDerivativesMetricPipeline,
)
from investment_analyst.analytics.crypto.derivatives_service import (
    CryptoDerivativesService,
    _latest_metric,
)
from investment_analyst.catalog.provider_configuration import resolve_deribit_configuration
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import MetricResult
from investment_analyst.providers.crypto.deribit import DeribitClient
from investment_analyst.providers.crypto.deribit_pipeline import DeribitEvidencePipeline
from investment_analyst.providers.http import HttpResponse
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.errors import RecordNotFoundError

_FIXTURES = Path(__file__).parents[3] / "fixtures" / "deribit"
_START = datetime(2026, 8, 1, tzinfo=UTC)
_END = datetime(2026, 8, 3, tzinfo=UTC)
_RETRIEVED = datetime(2026, 8, 4, tzinfo=UTC)
_KNOWN = datetime(2026, 8, 5, tzinfo=UTC)
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


def _setup_evidence(storage: LocalStorage, configuration) -> None:
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


def _legacy_rows(
    storage: LocalStorage, configuration, *, known_at: datetime
) -> tuple[MetricResult, ...]:
    """Persist the same evidence under the legacy v1 identity of one cut."""
    computation = CryptoDerivativesMetricEngine().compute(
        tuple(storage.observations.list(asset_id=configuration.asset_id, available_to=known_at)),
        asset_id=configuration.asset_id,
        funding_source_id=configuration.funding_source_id,
        dvol_source_id=configuration.dvol_source_id,
        summary_source_id=configuration.summary_source_id,
        known_at=known_at,
        computed_at=known_at,
        as_of_from=_START,
        as_of_before=_END,
    )
    rows: list[MetricResult] = []
    for candidate in computation.results:
        parameters = {**candidate.parameters, "known_at": known_at.isoformat()}
        identifier = metric_result_id(
            asset_id=candidate.asset_id,
            metric_key=candidate.metric_key,
            input_observation_ids=tuple(candidate.input_observation_ids),
            parameters=parameters,
            algorithm_version=candidate.algorithm_version,
            as_of=candidate.as_of,
            available_at=candidate.available_at,
            value=candidate.value,
            unit=candidate.unit,
            quality=candidate.quality,
        )
        rows.append(
            candidate.model_copy(update={"result_id": identifier, "parameters": parameters})
        )
    for row in rows:
        storage.metric_results.save(row)
    return tuple(rows)


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


def test_replay_prefers_persisted_v2_then_v1_then_in_memory_candidate(tmp_path: Path) -> None:
    """A7: the replay resolves the v2 row, else the v1 row of the cut, else memory."""
    configuration = _configuration()

    with LocalStorage(StoragePaths.from_root(tmp_path / "in-memory")) as storage:
        _setup_evidence(storage, configuration)

        candidate = _query(storage, _KNOWN)

        assert candidate.metrics
        assert all(item.result_id.version == 8 for item in candidate.metrics)
        assert candidate.diagnostic.status is CryptoDerivativesDiagnosticStatus.PARTIAL

    with LocalStorage(StoragePaths.from_root(tmp_path / "legacy")) as storage:
        _setup_evidence(storage, configuration)
        legacy = _legacy_rows(storage, configuration, known_at=_KNOWN)

        replayed = _query(storage, _KNOWN)

        assert legacy and all(row.result_id.version == 5 for row in legacy)
        assert {item.result_id for item in replayed.metrics} == {row.result_id for row in legacy}

    with LocalStorage(StoragePaths.from_root(tmp_path / "v2")) as storage:
        _setup_evidence(storage, configuration)
        _legacy_rows(storage, configuration, known_at=_KNOWN)
        persisted = CryptoDerivativesMetricPipeline(
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

        replayed = _query(storage, _KNOWN)

        assert persisted.results_created > 0
        assert {item.result_id for item in replayed.metrics} == {
            row.result_id for row in persisted.results
        }
        assert all(item.result_id.version == 8 for item in replayed.metrics)


def test_query_traceability_reads_observations_in_bounded_chunks(tmp_path: Path) -> None:
    """A4: traceability verifies observations using bounded chunks of at most 1,000 without get."""
    paths = StoragePaths.from_root(tmp_path / "storage")
    configuration = _configuration()
    with LocalStorage(paths) as storage:
        _setup_evidence(storage, configuration)

    with LocalStorage(paths, read_only=True) as storage:
        get_calls: list[UUID] = []
        get_many_calls: list[tuple[UUID, ...]] = []

        original_get = storage.observations.get
        original_get_many = storage.observations.get_many

        def spy_get(identifier: UUID):
            get_calls.append(identifier)
            return original_get(identifier)

        def spy_get_many(identifiers):
            chunk = tuple(identifiers)
            get_many_calls.append(chunk)
            return original_get_many(chunk)

        storage.observations.get = spy_get  # type: ignore[method-assign]
        storage.observations.get_many = spy_get_many  # type: ignore[method-assign]

        result = _query(storage, _KNOWN)

        assert result.traceability_verified
        assert len(get_calls) == 0
        assert len(get_many_calls) > 0
        assert all(len(chunk) <= 1_000 for chunk in get_many_calls)

        # Test chunking with >1,000 identifiers
        service = CryptoDerivativesService(storage, CryptoDerivativesMetricEngine())
        synthetic_ids = tuple(uuid4() for _ in range(2_500))
        chunk_sizes: list[int] = []

        def spy_chunked_get_many(identifiers):
            chunk = tuple(identifiers)
            chunk_sizes.append(len(chunk))
            sample_obs = original_get(next(iter(result.diagnostic.observation_ids)))
            return {ident: sample_obs for ident in chunk}

        storage.observations.get_many = spy_chunked_get_many  # type: ignore[method-assign]
        service._verify_traceability(synthetic_ids, ())
        assert chunk_sizes == [1_000, 1_000, 500]
        assert len(get_calls) == 0


def test_diagnostic_still_resolves_the_same_three_metric_values(tmp_path: Path) -> None:
    """X1: diagnostic contract still exposes funding_sum_168h, dvol_change_7d and spread."""
    paths = StoragePaths.from_root(tmp_path / "storage")
    configuration = _configuration()
    with LocalStorage(paths) as storage:
        _setup_evidence(storage, configuration)

    with LocalStorage(paths, read_only=True) as storage:
        result = _query(storage, _KNOWN)

        diagnostic = result.diagnostic
        assert hasattr(diagnostic, "funding_sum_168h")
        assert hasattr(diagnostic, "dvol_change_7d")
        assert hasattr(diagnostic, "latest_spread_bps")
        assert diagnostic.latest_spread_bps is not None
        assert diagnostic.latest_spread_bps.metric_key == SPREAD_BPS_KEY
        assert diagnostic.latest_spread_bps.value == Decimal("1.999800019998000199980001999800020")

        sample_metrics = result.metrics
        assert _latest_metric(sample_metrics, FUNDING_SUM_KEY, window=168) is None
        assert _latest_metric(sample_metrics, DVOL_CHANGE_KEY, window=7) is None
        assert _latest_metric(sample_metrics, SPREAD_BPS_KEY, window=1) is not None


def test_query_traceability_still_fails_on_a_missing_observation(tmp_path: Path) -> None:
    """X2: traceability fails closed with RecordNotFoundError on any missing observation."""
    paths = StoragePaths.from_root(tmp_path / "storage")
    configuration = _configuration()
    with LocalStorage(paths) as storage:
        _setup_evidence(storage, configuration)

    with LocalStorage(paths, read_only=True) as storage:
        service = CryptoDerivativesService(storage, CryptoDerivativesMetricEngine())
        missing_id = uuid4()
        with pytest.raises(RecordNotFoundError):
            service._verify_traceability((missing_id,), ())
