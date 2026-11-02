"""Atomic funding-complement export, verification and offline training view."""

from __future__ import annotations

import tempfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from investment_analyst.core.models.enums import AssetClass
from investment_analyst.research.funding_archive import build_funding_archive
from investment_analyst.research.funding_export import (
    FundingExportConflictError,
    FundingExportPathError,
    export_funding_bundle,
    load_base_dataset,
    read_funding_bundle,
    read_funding_training_view,
)
from investment_analyst.research.funding_features import build_funding_bundle
from investment_analyst.research.funding_models import (
    FUNDING_COLUMN_24H,
    FUNDING_COLUMN_168H,
    FUNDING_FEATURES_FILE_NAME,
    FundingArchivePage,
    FundingAssetIdentity,
    FundingFeatureManifest,
    FundingPoint,
    assumed_funding_available_instant,
    funding_archive_request,
    funding_point_id,
)
from investment_analyst.research.historical_archive import build_historical_archive
from investment_analyst.research.historical_dataset import build_historical_dataset
from investment_analyst.research.historical_export import export_historical_dataset
from investment_analyst.research.historical_models import (
    HistoricalArchiveRequest,
    HistoricalAssetIdentity,
    HistoricalCandle,
    HistoricalDecisionRequest,
    SampleSplit,
    assumed_available_instant,
    bar_start_instant,
    historical_bar_content_id,
)

_ASSET_ID = "crypto:btc-usd"
_FUNDING_SOURCE = "deribit:btc-perpetual:funding-rate-history"
_SPOT_SOURCE = "coinbase-exchange:btc-usd:daily-candles"
_DECISION_START = date(2024, 1, 1)
_DECISION_END = date(2024, 2, 29)
_HISTORICAL_RETRIEVED = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
_HISTORICAL_KNOWN = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
_FUNDING_RETRIEVED = datetime(2026, 11, 1, 0, 5, tzinfo=UTC)
_KNOWN = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)


def _historical_asset() -> HistoricalAssetIdentity:
    return HistoricalAssetIdentity(
        asset_id=_ASSET_ID,
        source_id=_SPOT_SOURCE,
        product_id="BTC-USD",
        asset_class=AssetClass.CRYPTO,
        quote_currency="USD",
        crypto_profile="bitcoin",
        exchange="COINBASE",
        granularity_seconds=86_400,
    )


def _candle(day: date, close: Decimal) -> HistoricalCandle:
    start = bar_start_instant(day)
    end = start + timedelta(days=1)
    raw_values = (
        str(int(start.timestamp())),
        str(close),
        str(close + 1),
        str(close - 1),
        str(close),
        "10",
    )
    return HistoricalCandle(
        bar_content_id=historical_bar_content_id(
            asset_id=_ASSET_ID,
            source_id=_SPOT_SOURCE,
            bar_start=start,
            open_value=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=Decimal("10"),
            raw_values=raw_values,
        ),
        bar_start=start,
        bar_end=end,
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=Decimal("10"),
        raw_values=raw_values,
        assumed_available_at=assumed_available_instant(end),
        retrieved_at=_HISTORICAL_RETRIEVED,
    )


def _build_base_dataset(root: Path, *, start: date = _DECISION_START) -> Path:
    decision_end = _DECISION_END
    archive_start = start - timedelta(days=2)
    archive_end = decision_end + timedelta(days=6)
    archive_request = HistoricalArchiveRequest(
        asset_id=_ASSET_ID,
        start_date=archive_start,
        end_date=archive_end,
    )
    days = (archive_end - archive_start).days + 1
    candles = tuple(
        _candle(
            archive_start + timedelta(days=offset),
            Decimal(100 + offset),
        )
        for offset in range(days)
    )
    archive = build_historical_archive(
        _historical_asset(),
        archive_request,
        request_urls=("https://example.invalid/candles",),
        retrieved_at=_HISTORICAL_RETRIEVED,
        candles=candles,
    )
    decision_request = HistoricalDecisionRequest(
        asset_id=_ASSET_ID,
        start_date=start,
        end_date=decision_end,
        known_at=_HISTORICAL_KNOWN,
    )
    dataset = build_historical_dataset(decision_request, archive)
    receipt = export_historical_dataset(
        dataset,
        archive=archive,
        output_dir=root / "base",
        forbidden_roots=(),
    )
    return Path(receipt.dataset_dir)


def _funding_asset() -> FundingAssetIdentity:
    return FundingAssetIdentity(
        asset_id=_ASSET_ID,
        funding_source_id=_FUNDING_SOURCE,
        currency="BTC",
        instrument_name="BTC-PERPETUAL",
        asset_class=AssetClass.CRYPTO,
        quote_currency="USD",
        crypto_profile="bitcoin",
    )


def _raw(timestamp: datetime) -> dict[str, str]:
    return {
        "timestamp": str(int(timestamp.timestamp() * 1_000)),
        "index_price": "100.5",
        "prev_index_price": "100",
        "interest_1h": "0.000001",
        "interest_8h": "0.000008",
    }


def _point(timestamp: datetime, page_index: int) -> FundingPoint:
    raw = _raw(timestamp)
    return FundingPoint(
        point_id=funding_point_id(
            funding_source_id=_FUNDING_SOURCE,
            timestamp=timestamp,
            index_price=Decimal(raw["index_price"]),
            prev_index_price=Decimal(raw["prev_index_price"]),
            interest_1h=Decimal(raw["interest_1h"]),
            interest_8h=Decimal(raw["interest_8h"]),
            raw_payload=raw,
        ),
        page_index=page_index,
        timestamp=timestamp,
        index_price=Decimal(raw["index_price"]),
        prev_index_price=Decimal(raw["prev_index_price"]),
        interest_1h=Decimal(raw["interest_1h"]),
        interest_8h=Decimal(raw["interest_8h"]),
        raw_payload=raw,
        assumed_available_at=assumed_funding_available_instant(timestamp),
    )


def _build_funding_archive(base_dataset_dir: Path):
    from investment_analyst.research.funding_archive import _page_bytes, _sha256_bytes

    loaded = load_base_dataset(base_dataset_dir)
    request = funding_archive_request(
        asset_id=_ASSET_ID,
        decision_start_date=loaded.reference.start_date,
        decision_end_date=loaded.reference.end_date,
    )
    pages = []
    points = []
    cursor = request.requested_start
    page_index = 0
    while cursor < request.requested_end:
        page_end = min(cursor + timedelta(days=31), request.requested_end)
        page_points = tuple(
            _point(cursor + timedelta(hours=offset), page_index)
            for offset in range(int((page_end - cursor).total_seconds() // 3_600))
        )
        payload = _page_bytes(page_points)
        pages.append(
            FundingArchivePage(
                page_index=page_index,
                requested_start=cursor,
                requested_end=page_end,
                request_url=f"https://example.invalid/funding/{page_index}",
                started_at=datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
                finished_at=datetime(2026, 11, 1, 0, 10, tzinfo=UTC),
                elapsed_milliseconds=10_000,
                retrieved_at=_FUNDING_RETRIEVED,
                point_count=len(page_points),
                points_sha256=_sha256_bytes(payload),
                points_bytes=len(payload),
            )
        )
        points.extend(page_points)
        cursor = page_end
        page_index += 1
    return build_funding_archive(
        asset=_funding_asset(),
        request=request,
        pages=tuple(pages),
        points=tuple(points),
        retrieved_at=_FUNDING_RETRIEVED,
    )


def _bundle(base_dataset_dir: Path):
    loaded = load_base_dataset(base_dataset_dir)
    archive = _build_funding_archive(base_dataset_dir)
    return (
        build_funding_bundle(
            base_dataset=loaded.reference,
            decisions=loaded.decisions,
            funding_asset=archive.manifest.asset,
            archive=archive,
            known_at=_KNOWN,
        ),
        archive,
    )


def _export(root: Path, *, bundle, archive) -> Path:
    receipt = export_funding_bundle(
        bundle,
        archive=archive,
        output_dir=root / "bundles",
        forbidden_roots=(root / "base",),
        exported_at=_KNOWN,
    )
    return Path(receipt.bundle_dir)


def _fixture(root: Path):
    base_dir = _build_base_dataset(root)
    bundle, archive = _bundle(base_dir)
    bundle_dir = _export(root, bundle=bundle, archive=archive)
    return base_dir, bundle, archive, bundle_dir


def test_export_readback_reuse_and_training_view_excludes_holdout(tmp_path: Path) -> None:
    base_dir, bundle, archive, bundle_dir = _fixture(tmp_path)
    reloaded = read_funding_bundle(bundle_dir)
    assert reloaded.manifest.bundle_id == bundle.manifest.bundle_id
    assert reloaded.manifest.bundle_content_sha256 == bundle.manifest.bundle_content_sha256
    assert reloaded.rows == bundle.rows
    assert reloaded.manifest.admission.status.value == "insufficient_funding_coverage"
    assert reloaded.manifest.admission.unmet_requirements

    rows_path = bundle_dir / FUNDING_FEATURES_FILE_NAME
    original_bytes = rows_path.read_bytes()
    original_manifest = (bundle_dir / "manifest.json").read_bytes()
    second = export_funding_bundle(
        bundle,
        archive=archive,
        output_dir=tmp_path / "bundles",
        forbidden_roots=(tmp_path / "base",),
        exported_at=_KNOWN,
    )
    assert second.status == "reused"
    assert rows_path.read_bytes() == original_bytes
    assert (bundle_dir / "manifest.json").read_bytes() == original_manifest

    view = read_funding_training_view(bundle_dir, base_dir)
    expected_rows = sum(
        item.requested
        for item in bundle.manifest.splits
        if item.split in {SampleSplit.TRAIN, SampleSplit.VALIDATION}
    )
    assert len(view.features) == expected_rows
    assert len(view.targets) == expected_rows
    assert len(view.metadata) == expected_rows
    holdout_keys = {str(row.sample_key) for row in bundle.rows if row.split is SampleSplit.HOLDOUT}
    assert holdout_keys
    assert not holdout_keys & {row.sample_key for row in view.features}
    assert not holdout_keys & {row.sample_key for row in view.targets}

    base_loaded = load_base_dataset(base_dir)
    base_by_key = {str(sample.sample_key): sample for sample in base_loaded.samples}
    funding_by_key = {str(row.sample_key): row for row in bundle.rows}
    feature_row = view.features[0]
    base_sample = base_by_key[feature_row.sample_key]
    funding_row = funding_by_key[feature_row.sample_key]
    base_features = {item.column_name: item.value for item in base_sample.features}
    assert feature_row.close_return_1d == base_features["close_return_1d"]
    assert feature_row.rsi_14_fraction == base_features["rsi_14_fraction"]
    assert (
        feature_row.macd_12_26_9_histogram_close_ratio
        == base_features["macd_12_26_9_histogram_close_ratio"]
    )
    funding_features = {item.column_name: item.value for item in funding_row.features}
    assert feature_row.funding_sum_1h_24h == funding_features[FUNDING_COLUMN_24H]
    assert feature_row.funding_sum_1h_168h == funding_features[FUNDING_COLUMN_168H]
    assert not hasattr(feature_row, "label")
    assert hasattr(view.targets[0], "label")
    assert view.metadata[0].split in {"train", "validation"}


def test_bundle_rejects_tamper_and_ignores_a_foreign_corpus(tmp_path: Path) -> None:
    _, bundle, _, bundle_dir = _fixture(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    for index in range(10):
        (foreign / f"foreign-{index}.bin").write_bytes(bytes([index]) * 256)
    assert read_funding_bundle(bundle_dir).manifest.bundle_id == bundle.manifest.bundle_id

    rows_path = bundle_dir / FUNDING_FEATURES_FILE_NAME
    original = rows_path.read_bytes()
    rows_path.write_bytes(original + b"tamper")
    with pytest.raises(FundingExportConflictError, match="differs from its manifest"):
        read_funding_bundle(bundle_dir)
    rows_path.write_bytes(original)

    manifest_path = bundle_dir / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest_path.write_bytes(manifest_bytes + b"\n")
    with pytest.raises(FundingExportConflictError, match="canonical encoding"):
        read_funding_bundle(bundle_dir)


def test_bundle_manifest_rejects_reordered_duplicate_missing_or_wrong_source() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        base_dir = _build_base_dataset(root)
        bundle, _ = _bundle(base_dir)
        payload = bundle.manifest.model_dump(mode="json")
        rows = payload["rows"]
        with pytest.raises(ValidationError):
            FundingFeatureManifest.model_validate(payload | {"rows": list(reversed(rows))})
        with pytest.raises(ValidationError):
            FundingFeatureManifest.model_validate(payload | {"rows": [rows[0], rows[0]]})
        with pytest.raises(ValidationError):
            FundingFeatureManifest.model_validate(payload | {"rows": rows[:-1]})
        wrong_source = payload | {
            "funding_asset": payload["funding_asset"]
            | {
                "asset_id": "crypto:eth-usd",
                "currency": "ETH",
                "instrument_name": "ETH-PERPETUAL",
                "funding_source_id": "deribit:eth-perpetual:funding-rate-history",
            }
        }
        with pytest.raises(ValidationError):
            FundingFeatureManifest.model_validate(wrong_source)


def test_export_path_safety_rejects_relative_repo_base_and_symlink(tmp_path: Path) -> None:
    base_dir, bundle, archive, _ = _fixture(tmp_path)
    with pytest.raises(FundingExportPathError, match="absolute"):
        export_funding_bundle(
            bundle,
            archive=archive,
            output_dir=Path("relative-bundles"),
            forbidden_roots=(),
        )
    with pytest.raises(FundingExportPathError, match="protected root"):
        export_funding_bundle(
            bundle,
            archive=archive,
            output_dir=Path(__file__).resolve().parents[3] / "bundles",
            forbidden_roots=(Path(__file__).resolve().parents[3],),
        )
    with pytest.raises(FundingExportPathError, match="protected root"):
        export_funding_bundle(
            bundle,
            archive=archive,
            output_dir=base_dir / "bundles",
            forbidden_roots=(base_dir,),
        )
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(FundingExportPathError, match="symbolic link"):
        export_funding_bundle(
            bundle,
            archive=archive,
            output_dir=link / "bundles",
            forbidden_roots=(),
        )


def test_training_view_rejects_a_different_base_dataset(tmp_path: Path) -> None:
    _, _, _, bundle_dir = _fixture(tmp_path)
    other_base = _build_base_dataset(tmp_path / "other", start=date(2024, 1, 2))
    with pytest.raises(FundingExportConflictError, match="base dataset reference"):
        read_funding_training_view(bundle_dir, other_base)


def test_funding_path_does_not_import_ml_or_open_the_workspace() -> None:
    root = Path(__file__).resolve().parents[3]
    paths = (
        root / "src/investment_analyst/research/funding_models.py",
        root / "src/investment_analyst/research/funding_features.py",
        root / "src/investment_analyst/research/funding_archive.py",
        root / "src/investment_analyst/research/funding_export.py",
        root / "scripts/build_funding_research_dataset.py",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            "research.ml_",
            "import sklearn",
            "import numpy",
            "ApplicationRuntime",
            "WorkspaceService",
        ):
            assert forbidden not in text, (path, forbidden)
