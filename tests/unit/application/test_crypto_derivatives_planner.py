"""Edge-only receipt planning tests."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from investment_analyst.application.crypto_derivatives_models import (
    CryptoDerivativesPlanMode,
    CryptoDerivativesRefreshMode,
)
from investment_analyst.application.crypto_derivatives_planner import CryptoDerivativesPlanner
from investment_analyst.catalog.provider_configuration import resolve_deribit_configuration
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.providers.crypto.deribit_pipeline import (
    DeribitFetchReceipt,
    receipt_id,
    receipt_to_raw_record,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_START = datetime(2026, 8, 1, tzinfo=UTC)


def _configuration():
    return resolve_deribit_configuration(
        ProviderAssetContextResolver(AssetCatalogService.load_default()),
        asset_id="crypto:btc-usd",
    )


def _save_receipt(
    storage: LocalStorage,
    *,
    dataset: str,
    source_id: str,
    start: datetime,
    end: datetime,
) -> None:
    identifier = receipt_id(
        asset_id="crypto:btc-usd",
        source_id=source_id,
        dataset=dataset,
        requested_start=start,
        requested_end=end,
    )
    storage.raw_records.save(
        receipt_to_raw_record(
            DeribitFetchReceipt(
                receipt_id=identifier,
                asset_id="crypto:btc-usd",
                source_id=source_id,
                dataset=dataset,
                requested_start=start,
                requested_end=end,
                completed_at=datetime(2026, 8, 10, tzinfo=UTC),
                request_count=1,
                row_count=0,
            )
        )
    )


def test_initial_full_prefix_suffix_and_already_current_plans(tmp_path: Path) -> None:
    configuration = _configuration()
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        planner = CryptoDerivativesPlanner(storage, configuration=configuration)
        initial = planner.plan(
            _START,
            _START + timedelta(days=5),
            refresh_mode=CryptoDerivativesRefreshMode.AUTO,
        )
        assert initial.funding.mode is CryptoDerivativesPlanMode.INITIAL
        assert initial.dvol.mode is CryptoDerivativesPlanMode.INITIAL

        for dataset, source_id in (
            ("funding_history", configuration.funding_source_id),
            ("dvol_daily", configuration.dvol_source_id),
        ):
            _save_receipt(
                storage,
                dataset=dataset,
                source_id=source_id,
                start=_START + timedelta(days=1),
                end=_START + timedelta(days=4),
            )
        edges = planner.plan(
            _START,
            _START + timedelta(days=5),
            refresh_mode=CryptoDerivativesRefreshMode.AUTO,
        )
        assert edges.funding.mode is CryptoDerivativesPlanMode.INCREMENTAL
        assert tuple((item.start, item.end) for item in edges.funding.intervals) == (
            (_START, _START + timedelta(days=1)),
            (_START + timedelta(days=4), _START + timedelta(days=5)),
        )
        current = planner.plan(
            _START + timedelta(days=1),
            _START + timedelta(days=4),
            refresh_mode=CryptoDerivativesRefreshMode.AUTO,
        )
        assert current.funding.mode is CryptoDerivativesPlanMode.ALREADY_CURRENT
        assert current.funding.intervals == ()

        full = planner.plan(
            _START + timedelta(days=1),
            _START + timedelta(days=4),
            refresh_mode=CryptoDerivativesRefreshMode.FULL,
        )
        assert full.funding.mode is CryptoDerivativesPlanMode.FULL
        assert len(full.funding.intervals) == 1


def test_internal_receipt_gap_is_not_inferred(tmp_path: Path) -> None:
    configuration = _configuration()
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        for dataset, source_id in (
            ("funding_history", configuration.funding_source_id),
            ("dvol_daily", configuration.dvol_source_id),
        ):
            _save_receipt(
                storage,
                dataset=dataset,
                source_id=source_id,
                start=_START,
                end=_START + timedelta(days=1),
            )
            _save_receipt(
                storage,
                dataset=dataset,
                source_id=source_id,
                start=_START + timedelta(days=4),
                end=_START + timedelta(days=5),
            )
        plan = CryptoDerivativesPlanner(storage, configuration=configuration).plan(
            _START,
            _START + timedelta(days=5),
            refresh_mode=CryptoDerivativesRefreshMode.AUTO,
        )

        assert plan.funding.mode is CryptoDerivativesPlanMode.ALREADY_CURRENT
        assert plan.dvol.mode is CryptoDerivativesPlanMode.ALREADY_CURRENT
