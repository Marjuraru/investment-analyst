"""Integration coverage for the derivatives write-path adoption of identity v2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from investment_analyst.analytics.crypto.derivatives_engine import (
    DVOL_CHANGE_KEY,
    CryptoDerivativesMetricEngine,
)
from investment_analyst.analytics.crypto.derivatives_identity import metric_result_id
from investment_analyst.analytics.crypto.derivatives_pipeline import (
    CryptoDerivativesMetricPipeline,
)
from investment_analyst.analytics.crypto.derivatives_service import CryptoDerivativesService
from investment_analyst.catalog.provider_configuration import resolve_deribit_configuration
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    MetricResult,
    NormalizedObservation,
    SourceReference,
)
from investment_analyst.providers.crypto.deribit import DeribitClient
from investment_analyst.providers.crypto.deribit_pipeline import DeribitEvidencePipeline
from investment_analyst.providers.http import HttpResponse
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.errors import StorageError
from investment_analyst.storage.serialization import canonical_json_text

_FIXTURES = Path(__file__).parents[3] / "fixtures" / "deribit"
_PRE_ADOPTION_DIAGNOSTIC_ID = "35ac79d9-9031-5554-8c54-7f879e5c699c"
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
_ASSET = "crypto:btc-usd"
_FUNDING = "deribit:btc-perpetual:funding-rate-history"


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
        return HttpResponse(status_code=200, body=self._bodies.pop(0), headers={}, url=url)


def _configuration():
    return resolve_deribit_configuration(
        ProviderAssetContextResolver(AssetCatalogService.load_default()),
        asset_id=_ASSET,
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


def _pipeline(storage: LocalStorage, clock: datetime) -> CryptoDerivativesMetricPipeline:
    return CryptoDerivativesMetricPipeline(
        storage,
        CryptoDerivativesMetricEngine(),
        clock=lambda: clock,
    )


def _run(storage: LocalStorage, configuration, *, known_at: datetime, clock: datetime):
    return _pipeline(storage, clock).run(
        asset_id=configuration.asset_id,
        funding_source_id=configuration.funding_source_id,
        dvol_source_id=configuration.dvol_source_id,
        summary_source_id=configuration.summary_source_id,
        known_at=known_at,
        as_of_from=_START,
        as_of_before=_END,
    )


def _query(storage: LocalStorage, *, known_at: datetime):
    configuration = _configuration()
    return CryptoDerivativesService(
        storage,
        CryptoDerivativesMetricEngine(),
        clock=lambda: _RETRIEVED,
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


def _rows(storage: LocalStorage) -> dict:
    return {item.result_id: item for item in storage.metric_results.list(asset_id=_ASSET)}


def _funding_observation(
    observed_at: datetime,
    value: str,
    *,
    available_at: datetime,
    raw_record_id=None,
) -> NormalizedObservation:
    from uuid import NAMESPACE_URL, uuid5

    raw_id = raw_record_id or uuid5(NAMESPACE_URL, f"raw:{observed_at.isoformat()}:{value}")
    reference = SourceReference(
        source_id=_FUNDING,
        record_key=f"funding:{observed_at.isoformat()}",
        retrieved_at=available_at,
    )
    return NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, f"obs:{raw_id}:{value}"),
        raw_record_id=raw_id,
        asset_id=_ASSET,
        field_name="funding_interest_1h",
        value=Decimal(value),
        unit="ratio",
        frequency=DataFrequency.HOUR_1,
        observed_at=observed_at,
        available_at=available_at,
        normalized_at=available_at + timedelta(minutes=1),
        source=reference,
        quality=DataQuality.VALID,
        transformation_version="derivatives-adoption-v2-test-v1",
    )


def test_new_known_at_without_new_evidence_creates_zero_derivatives_metrics(tmp_path) -> None:
    """A4: a later cut over the same evidence reuses every derivatives metric."""
    configuration = _configuration()
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        _setup_evidence(storage, configuration)
        first = _run(storage, configuration, known_at=_KNOWN, clock=_KNOWN)
        before = _rows(storage)

        assert first.results_created == len(first.results) > 0
        assert first.results_reused == 0
        assert all(row.result_id.version == 8 for row in before.values())
        assert all("known_at" not in row.parameters for row in before.values())

        later_cut = _KNOWN + timedelta(days=1)
        second = _run(storage, configuration, known_at=later_cut, clock=later_cut)
        after = _rows(storage)

        assert second.results_created == 0
        assert second.results_reused == len(first.results)
        assert set(after) == set(before)
        assert all(after[identifier] == before[identifier] for identifier in before)


def test_a_different_value_for_the_same_v2_coordinate_fails_closed(tmp_path) -> None:
    """A5: a rewritten value under the same semantic coordinate is a conflict."""
    configuration = _configuration()
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        _setup_evidence(storage, configuration)
        _run(storage, configuration, known_at=_KNOWN, clock=_KNOWN)

        target = next(row for row in _rows(storage).values() if row.metric_key == DVOL_CHANGE_KEY)
        assert target.result_id.version == 8

        corrupted = target.model_copy(update={"value": Decimal("999999.99")})
        storage.metric_results._connection.execute(
            "UPDATE metric_results SET document_json = ? WHERE result_id = ?",
            [canonical_json_text(corrupted), str(corrupted.result_id)],
        )

        with pytest.raises(StorageError, match="collides with different semantic content"):
            _run(storage, configuration, known_at=_KNOWN, clock=_KNOWN + timedelta(days=1))


def test_replay_of_a_pre_adoption_cut_returns_the_persisted_v1_rows_and_diagnostic_id(
    tmp_path,
) -> None:
    """A6: with only v1 rows persisted, the replay resolves them and keeps its identity."""
    configuration = _configuration()
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        _setup_evidence(storage, configuration)
        legacy = _legacy_rows(storage, configuration, known_at=_KNOWN)

        assert legacy
        assert all(row.result_id.version == 5 for row in legacy)

        result = _query(storage, known_at=_KNOWN)
        legacy_ids = {row.result_id for row in legacy}

        assert {item.result_id for item in result.metrics} == legacy_ids
        assert set(result.diagnostic.metric_result_ids) == legacy_ids
        assert str(result.diagnostic.diagnostic_id) == _PRE_ADOPTION_DIAGNOSTIC_ID
        assert result.traceability_verified


def test_observation_revision_yields_a_new_v2_identity_invisible_to_earlier_cuts() -> None:
    """A8: a revised observation changes the v2 coordinate and is invisible before it exists."""
    start = datetime(2026, 6, 1, tzinfo=UTC)
    available = datetime(2026, 8, 1, tzinfo=UTC)
    known = datetime(2026, 8, 2, tzinfo=UTC)
    original = tuple(
        _funding_observation(
            start + timedelta(hours=index),
            "0.000001",
            available_at=available,
        )
        for index in range(24)
    )
    engine = CryptoDerivativesMetricEngine()
    coordinate = {
        "asset_id": _ASSET,
        "funding_source_id": _FUNDING,
        "dvol_source_id": "deribit:btc:dvol:daily",
        "summary_source_id": "deribit:btc-perpetual:book-summary",
        "as_of_from": start,
        "as_of_before": start + timedelta(days=1),
    }

    baseline = engine.compute(
        original,
        known_at=known,
        computed_at=known,
        **coordinate,
    )
    baseline_ids = {item.result_id for item in baseline.results}
    assert baseline_ids

    revision_at = known + timedelta(hours=1)
    revised = original + (
        _funding_observation(
            start + timedelta(hours=23),
            "0.000009",
            available_at=revision_at,
            raw_record_id=original[-1].raw_record_id,
        ),
    )
    after_revision = engine.compute(
        revised,
        known_at=revision_at,
        computed_at=revision_at,
        **coordinate,
    )
    revised_ids = {item.result_id for item in after_revision.results}

    assert revised_ids
    assert revised_ids.isdisjoint(baseline_ids)

    earlier = engine.compute(
        revised,
        known_at=known,
        computed_at=known,
        **coordinate,
    )
    assert {item.result_id for item in earlier.results} == baseline_ids


def test_existing_v1_rows_are_never_rewritten_or_reassigned(tmp_path) -> None:
    """N1: the adoption writes UUID8 rows and leaves every UUID5 row byte-identical."""
    configuration = _configuration()
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        _setup_evidence(storage, configuration)
        legacy = _legacy_rows(storage, configuration, known_at=_KNOWN)
        before = _rows(storage)

        summary = _run(storage, configuration, known_at=_KNOWN, clock=_KNOWN + timedelta(days=1))
        after = _rows(storage)
        created = {identifier: row for identifier, row in after.items() if identifier not in before}

        assert summary.results_created == len(created) > 0
        assert {row.result_id for row in legacy} <= set(after)
        assert all(after[row.result_id] == row for row in legacy)
        assert all(row.result_id.version == 8 for row in created.values())
        assert set(before) & set(created) == set()
        assert all("known_at" not in row.parameters for row in created.values())
        assert all(row.parameters.get("source_ids") for row in created.values())
        assert {row.metric_key for row in created.values()} == {row.metric_key for row in legacy}
