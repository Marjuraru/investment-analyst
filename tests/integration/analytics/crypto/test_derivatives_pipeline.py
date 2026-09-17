"""Persistence identity and traceability tests for derivatives metrics."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

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
            clock=lambda: datetime(2026, 8, 4, tzinfo=UTC),
        ),
        configuration=configuration,
        clock=lambda: datetime(2026, 8, 4, tzinfo=UTC),
    )
    evidence.import_funding(_START, _END)
    evidence.import_dvol(_START, _END)
    evidence.capture_summary()


def test_equivalent_recomputation_reuses_original_computed_at(tmp_path: Path) -> None:
    configuration = _configuration()
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        _setup_evidence(storage, configuration)
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


def test_pipeline_reads_and_writes_in_batches_without_get_after_save(
    tmp_path: Path,
) -> None:
    configuration = _configuration()
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        _setup_evidence(storage, configuration)

        save_many_calls: list[int] = []
        orig_save_many = storage.metric_results.save_many

        def spy_save_many(results):
            save_many_calls.append(len(results))
            return orig_save_many(results)

        storage.metric_results.save_many = spy_save_many

        def fail_save(*args, **kwargs):
            pytest.fail("pipeline called per-row metric_results.save instead of save_many")

        storage.metric_results.save = fail_save

        obs_get_many_calls: list[int] = []
        orig_obs_get_many = storage.observations.get_many

        def spy_obs_get_many(ids):
            obs_get_many_calls.append(len(ids))
            return orig_obs_get_many(ids)

        storage.observations.get_many = spy_obs_get_many

        def fail_obs_get(*args, **kwargs):
            pytest.fail("pipeline called per-row observations.get instead of get_many")

        storage.observations.get = fail_obs_get

        orig_metric_get = storage.metric_results.get

        def fail_metric_get_after_save(*args, **kwargs):
            if save_many_calls:
                pytest.fail("pipeline called metric_results.get after save_many")
            return orig_metric_get(*args, **kwargs)

        storage.metric_results.get = fail_metric_get_after_save

        summary = CryptoDerivativesMetricPipeline(
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

        assert len(save_many_calls) == 1
        assert save_many_calls[0] == len(summary.results) > 0
        assert len(obs_get_many_calls) == 1
        assert summary.traceability_verified is True
        assert summary.results_created == len(summary.results)


def test_batch_path_produces_identical_results_ids_and_decimals(
    tmp_path: Path,
) -> None:
    configuration = _configuration()
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        _setup_evidence(storage, configuration)
        engine = CryptoDerivativesMetricEngine()
        observations = tuple(
            storage.observations.list(
                asset_id=configuration.asset_id,
                available_to=_KNOWN,
            )
        )
        computation = engine.compute(
            observations,
            asset_id=configuration.asset_id,
            funding_source_id=configuration.funding_source_id,
            dvol_source_id=configuration.dvol_source_id,
            summary_source_id=configuration.summary_source_id,
            known_at=_KNOWN,
            computed_at=_KNOWN,
            as_of_from=_START,
            as_of_before=_END,
        )
        summary = CryptoDerivativesMetricPipeline(
            storage,
            engine,
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

        assert len(summary.results) == len(computation.results)
        for actual, expected in zip(summary.results, computation.results, strict=True):
            assert actual.result_id == expected.result_id
            assert actual.metric_key == expected.metric_key
            assert actual.value == expected.value
            assert isinstance(actual.value, Decimal)
            assert actual.unit == expected.unit
            assert actual.as_of == expected.as_of
            assert actual.available_at == expected.available_at
            assert actual.parameters == expected.parameters
            assert actual.input_observation_ids == expected.input_observation_ids
            assert actual.quality == expected.quality


def test_pipeline_consumes_batch_write_receipt_instead_of_global_counts(
    tmp_path: Path,
) -> None:
    configuration = _configuration()
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        _setup_evidence(storage, configuration)

        for repo in (
            storage.raw_records,
            storage.observations,
            storage.metric_results,
            storage.diagnostics,
        ):
            repo.count = lambda *args, **kwargs: pytest.fail("global count was called")

        summary = CryptoDerivativesMetricPipeline(
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

        assert summary.results_created > 0
        assert summary.traceability_verified is True


class _QueryCountingConnection:
    def __init__(self, target) -> None:
        self._target = target
        self.execute_count = 0

    def execute(self, *args, **kwargs):
        self.execute_count += 1
        return self._target.execute(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._target, name)


def test_query_count_is_bounded_and_not_proportional_to_rows(
    tmp_path: Path,
) -> None:
    configuration = _configuration()
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        _setup_evidence(storage, configuration)
        pipeline = CryptoDerivativesMetricPipeline(
            storage,
            CryptoDerivativesMetricEngine(),
            clock=lambda: _KNOWN,
        )

        first = pipeline.run(
            asset_id=configuration.asset_id,
            funding_source_id=configuration.funding_source_id,
            dvol_source_id=configuration.dvol_source_id,
            summary_source_id=configuration.summary_source_id,
            known_at=_KNOWN,
            as_of_from=_START,
            as_of_before=_END,
        )
        assert len(first.results) >= 2

        wrapper = _QueryCountingConnection(storage.metric_results._connection)
        storage.metric_results._connection = wrapper
        storage.observations._connection = wrapper

        wrapper.execute_count = 0
        repeated = pipeline.run(
            asset_id=configuration.asset_id,
            funding_source_id=configuration.funding_source_id,
            dvol_source_id=configuration.dvol_source_id,
            summary_source_id=configuration.summary_source_id,
            known_at=_KNOWN,
            as_of_from=_START,
            as_of_before=_END,
        )
        assert repeated.results_reused == len(first.results)
        # Bounded query count independent of row count:
        # 1 observations list + definitions upsert + 1 metric_results list
        assert wrapper.execute_count <= 8


def test_decimal_utc_and_available_at_are_preserved(tmp_path: Path) -> None:
    configuration = _configuration()
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        _setup_evidence(storage, configuration)
        summary = CryptoDerivativesMetricPipeline(
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

        assert len(summary.results) > 0
        for result in summary.results:
            assert isinstance(result.value, Decimal)
            assert result.as_of.tzinfo is UTC
            assert result.available_at.tzinfo is UTC
            assert result.computed_at.tzinfo is UTC
            assert result.available_at <= _KNOWN
            assert result.available_at <= result.computed_at
