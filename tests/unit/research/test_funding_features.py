"""Causal funding sums, gaps, hashes and bundle admission."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

import pytest

from investment_analyst.analytics.crypto.derivatives_engine import (
    FUNDING_SUM_KEY,
    CryptoDerivativesMetricEngine,
)
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    SourceReference,
)
from investment_analyst.core.models.enums import AssetClass
from investment_analyst.research.funding_archive import (
    _page_bytes,
    _sha256_bytes,
    build_funding_archive,
)
from investment_analyst.research.funding_features import (
    FundingBaseDecision,
    FundingFeatureRequestError,
    _window_evidence,
    build_funding_bundle,
)
from investment_analyst.research.funding_models import (
    FUNDING_COLUMN_24H,
    FUNDING_COLUMN_168H,
    FundingArchivePage,
    FundingArchiveRequest,
    FundingAssetIdentity,
    FundingBaseDatasetReference,
    FundingFeatureEvidence,
    FundingPoint,
    FundingReasonCode,
    assumed_funding_available_instant,
    funding_archive_request,
    funding_point_id,
)
from investment_analyst.research.historical_models import FeatureState, SampleSplit

_ASSET_ID = "crypto:btc-usd"
_SOURCE_ID = "deribit:btc-perpetual:funding-rate-history"
_DECISION = date(2024, 3, 4)
_ANCHOR = datetime(2024, 3, 4, 0, 0, tzinfo=UTC)
_RETRIEVED = datetime(2026, 11, 2, 0, 0, 30, tzinfo=UTC)
_KNOWN = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)


def _asset(
    *,
    asset_id: str = _ASSET_ID,
    currency: str = "BTC",
    source_id: str = _SOURCE_ID,
) -> FundingAssetIdentity:
    return FundingAssetIdentity(
        asset_id=asset_id,
        funding_source_id=source_id,
        currency=currency,
        instrument_name=f"{currency}-PERPETUAL",
        asset_class=AssetClass.CRYPTO,
        quote_currency="USD",
        crypto_profile="bitcoin" if currency == "BTC" else "ethereum",
    )


def _request(
    start: date = _DECISION,
    end: date = _DECISION,
    *,
    asset_id: str = _ASSET_ID,
) -> FundingArchiveRequest:
    return funding_archive_request(
        asset_id=asset_id,
        decision_start_date=start,
        decision_end_date=end,
    )


def _raw(timestamp: datetime, interest: str) -> dict[str, str]:
    return {
        "timestamp": str(int(timestamp.timestamp() * 1_000)),
        "index_price": "100.5",
        "prev_index_price": "100.0",
        "interest_1h": interest,
        "interest_8h": "0.000008",
    }


def _point(
    timestamp: datetime,
    *,
    interest: str = "0.000001",
    source_id: str = _SOURCE_ID,
) -> FundingPoint:
    raw = _raw(timestamp, interest)
    return FundingPoint(
        point_id=funding_point_id(
            funding_source_id=source_id,
            timestamp=timestamp,
            index_price=Decimal(raw["index_price"]),
            prev_index_price=Decimal(raw["prev_index_price"]),
            interest_1h=Decimal(raw["interest_1h"]),
            interest_8h=Decimal(raw["interest_8h"]),
            raw_payload=raw,
        ),
        page_index=0,
        timestamp=timestamp,
        index_price=Decimal(raw["index_price"]),
        prev_index_price=Decimal(raw["prev_index_price"]),
        interest_1h=Decimal(raw["interest_1h"]),
        interest_8h=Decimal(raw["interest_8h"]),
        raw_payload=raw,
        assumed_available_at=assumed_funding_available_instant(timestamp),
    )


def _archive(
    request: FundingArchiveRequest,
    points: tuple[FundingPoint, ...],
    *,
    currency: str = "BTC",
    source_id: str = _SOURCE_ID,
):
    payload = _page_bytes(points)
    page = FundingArchivePage(
        page_index=0,
        requested_start=request.requested_start,
        requested_end=request.requested_end,
        request_url="https://example.invalid",
        started_at=datetime(2026, 11, 2, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 11, 2, 0, 1, tzinfo=UTC),
        elapsed_milliseconds=60_000,
        retrieved_at=_RETRIEVED,
        point_count=len(points),
        points_sha256=_sha256_bytes(payload),
        points_bytes=len(payload),
    )
    return build_funding_archive(
        asset=_asset(
            asset_id=request.asset_id,
            currency=currency,
            source_id=source_id,
        ),
        request=request,
        pages=(page,),
        points=points,
        retrieved_at=_RETRIEVED,
    )


def _points(
    request: FundingArchiveRequest,
    *,
    interest_for: object = None,
    skip: datetime | None = None,
) -> tuple[FundingPoint, ...]:
    points: list[FundingPoint] = []
    offset = 0
    while offset < request.expected_points:
        timestamp = request.requested_start + timedelta(hours=offset)
        if timestamp != skip:
            interest = "0.000001" if interest_for is None else interest_for(timestamp)
            points.append(_point(timestamp, interest=interest))
        offset += 1
    return tuple(points)


def _reference(
    start: date = _DECISION,
    end: date = _DECISION,
    *,
    asset_id: str = _ASSET_ID,
    dataset_hash: str = "1" * 64,
    known_at: datetime = _KNOWN,
) -> FundingBaseDatasetReference:
    return FundingBaseDatasetReference(
        dataset_id=uuid5(NAMESPACE_URL, f"base:{asset_id}:{start}:{end}:{dataset_hash}"),
        dataset_content_sha256=dataset_hash,
        feature_set_version="crypto-market-reconstructed-3-v1",
        experiment_spec_id="crypto-daily-positive-return-5d-historical-v1",
        target_version="crypto-forward-close-return-5d-v1",
        asset_id=asset_id,
        start_date=start,
        end_date=end,
        known_at=known_at,
        row_count=(end - start).days + 1,
        base_archive_id=uuid5(NAMESPACE_URL, "base-archive"),
        base_archive_semantic_sha256="2" * 64,
    )


def _decisions(
    start: date = _DECISION,
    end: date = _DECISION,
    *,
    eligible: bool = True,
    split: SampleSplit = SampleSplit.TRAIN,
    purged: bool = False,
    gap: bool = False,
) -> tuple[FundingBaseDecision, ...]:
    count = (end - start).days + 1
    return tuple(
        FundingBaseDecision(
            sample_key=uuid5(
                NAMESPACE_URL,
                f"sample:{start + timedelta(days=index)}",
            ),
            decision_at=datetime.combine(
                start + timedelta(days=index),
                datetime.min.time().replace(hour=12),
                tzinfo=UTC,
            ),
            eligible=eligible,
            split=split,
            purged=purged,
        )
        for index in range(count)
        if not gap or index != 1
    )


def _bundle(
    request: FundingArchiveRequest,
    points: tuple[FundingPoint, ...],
    *,
    eligible: bool = True,
    split: SampleSplit = SampleSplit.TRAIN,
    dataset_hash: str = "1" * 64,
):
    archive = _archive(request, points)
    return build_funding_bundle(
        base_dataset=_reference(
            request.decision_start_date,
            request.decision_end_date,
            asset_id=request.asset_id,
            dataset_hash=dataset_hash,
        ),
        decisions=_decisions(
            request.decision_start_date,
            request.decision_end_date,
            eligible=eligible,
            split=split,
        ),
        funding_asset=archive.manifest.asset,
        archive=archive,
        known_at=_KNOWN,
    )


def _engine_sum(points: tuple[FundingPoint, ...], window: int) -> Decimal:
    available_at = points[-1].timestamp + timedelta(hours=2)
    observations = tuple(
        NormalizedObservation(
            observation_id=uuid5(NAMESPACE_URL, f"obs:{point.timestamp.isoformat()}"),
            raw_record_id=point.point_id,
            asset_id=_ASSET_ID,
            field_name="funding_interest_1h",
            value=point.interest_1h,
            unit="ratio",
            frequency=DataFrequency.HOUR_1,
            observed_at=point.timestamp,
            available_at=point.assumed_available_at,
            normalized_at=available_at,
            source=SourceReference(
                source_id=_SOURCE_ID,
                retrieved_at=point.assumed_available_at,
            ),
            quality=DataQuality.VALID,
            transformation_version="funding-equivalence-v1",
        )
        for point in points
    )
    computation = CryptoDerivativesMetricEngine().compute(
        observations,
        asset_id=_ASSET_ID,
        funding_source_id=_SOURCE_ID,
        dvol_source_id="deribit:btc:dvol:daily",
        summary_source_id="deribit:btc-perpetual:book-summary",
        known_at=available_at,
        computed_at=available_at,
    )
    matches = tuple(
        result
        for result in computation.results
        if result.metric_key == FUNDING_SUM_KEY and result.parameters["window"] == window
    )
    assert matches
    return max(matches, key=lambda result: result.as_of).value


def test_exact_sums_match_the_current_derivatives_engine() -> None:
    request = _request()
    points = _points(
        request,
        interest_for=lambda timestamp: str(Decimal("0.000001") * Decimal((timestamp.hour % 7) + 1)),
    )
    bundle = _bundle(request, points)
    row = bundle.rows[0]
    assert row.features[0].state is FeatureState.AVAILABLE
    assert row.features[1].state is FeatureState.AVAILABLE
    assert row.features[0].value == _engine_sum(points, 24)
    assert row.features[1].value == _engine_sum(points, 168)
    assert row.features[0].assumed_available_at == _ANCHOR + timedelta(hours=1)
    assert row.features[1].assumed_available_at == _ANCHOR + timedelta(hours=1)
    assert row.features[0].window_end == _ANCHOR
    assert row.features[1].window_end == _ANCHOR
    assert row.features[0].expected_points == 24
    assert row.features[1].expected_points == 168


def test_zero_interest_is_available_zero_while_a_gap_remains_missing() -> None:
    zero_request = _request()
    zero_points = _points(zero_request, interest_for=lambda _timestamp: "0")
    zero_bundle = _bundle(zero_request, zero_points)
    zero_row = zero_bundle.rows[0]
    assert zero_row.features[0].state is FeatureState.AVAILABLE
    assert zero_row.features[1].state is FeatureState.AVAILABLE
    assert zero_row.features[0].value == Decimal(0)
    assert zero_row.features[1].value == Decimal(0)

    gap_request = _request()
    gap_points = _points(gap_request, skip=_ANCHOR - timedelta(hours=30))
    gap_bundle = _bundle(gap_request, gap_points)
    gap_row = gap_bundle.rows[0]
    assert gap_row.features[0].state is FeatureState.AVAILABLE
    assert gap_row.features[0].observed_points == 24
    assert gap_row.features[1].state is FeatureState.MISSING
    assert gap_row.features[1].value is None
    assert gap_row.features[1].observed_points == 167
    assert gap_row.features[1].expected_points == 168
    assert gap_row.features[1].reasons[0].code is FundingReasonCode.POINTS_ABSENT
    assert gap_row.funding_state is FeatureState.MISSING
    assert gap_row.eligibility_joint is False
    assert any(
        gap.column_name == FUNDING_COLUMN_168H
        and gap.state is FeatureState.MISSING
        and gap.count == 1
        for gap in gap_bundle.manifest.gaps
    )


def test_not_yet_available_never_exposes_a_numeric_value() -> None:
    request = _request()
    points = _points(request)
    by_time = {point.timestamp: point for point in points}
    evidence = _window_evidence(
        points=by_time,
        funding_source_id=_SOURCE_ID,
        decision_at=_ANCHOR,
        column_name=FUNDING_COLUMN_24H,
        window=24,
    )
    assert evidence.state is FeatureState.NOT_YET_AVAILABLE
    assert evidence.value is None
    assert evidence.observed_points == 24
    assert evidence.assumed_available_at == _ANCHOR + timedelta(hours=1)
    assert evidence.reasons[0].code is FundingReasonCode.NOT_YET_AVAILABLE


def test_feature_hash_ignores_base_labels_but_changes_with_funding_inputs() -> None:
    request = _request()
    points = _points(request)
    first = _bundle(request, points, eligible=True, dataset_hash="1" * 64)
    second = _bundle(request, points, eligible=False, dataset_hash="3" * 64)
    assert first.rows[0].funding_feature_hash == second.rows[0].funding_feature_hash
    assert first.manifest.bundle_id != second.manifest.bundle_id
    assert first.manifest.bundle_content_sha256 != second.manifest.bundle_content_sha256

    changed_points = _points(request, skip=_ANCHOR - timedelta(hours=5))
    third = _bundle(request, changed_points)
    assert first.rows[0].funding_feature_hash != third.rows[0].funding_feature_hash


def test_bundle_joins_original_sample_keys_grid_and_splits() -> None:
    start = date(2024, 3, 1)
    end = date(2024, 3, 3)
    request = _request(start, end)
    points = _points(request)
    decisions = (
        _decisions(start, end)[:1] + _decisions(start, end, split=SampleSplit.VALIDATION)[1:2]
    )
    decisions = decisions + tuple(
        FundingBaseDecision(
            sample_key=uuid5(NAMESPACE_URL, "holdout"),
            decision_at=datetime(2024, 3, 3, 12, 0, tzinfo=UTC),
            eligible=True,
            split=SampleSplit.HOLDOUT,
            purged=False,
        )
        for _ in (0,)
    )
    archive = _archive(request, points)
    bundle = build_funding_bundle(
        base_dataset=_reference(start, end),
        decisions=decisions,
        funding_asset=archive.manifest.asset,
        archive=archive,
        known_at=_KNOWN,
    )
    assert tuple(row.sample_key for row in bundle.rows) == tuple(
        decision.sample_key for decision in decisions
    )
    assert tuple(row.decision_at for row in bundle.rows) == tuple(
        decision.decision_at for decision in decisions
    )
    assert tuple(row.split for row in bundle.rows) == (
        SampleSplit.TRAIN,
        SampleSplit.VALIDATION,
        SampleSplit.HOLDOUT,
    )
    assert bundle.manifest.splits[0].requested == 1
    assert bundle.manifest.splits[1].requested == 1
    assert bundle.manifest.splits[2].requested == 1
    row_payload = json.dumps(bundle.rows[0].model_dump(mode="json"), sort_keys=True)
    for forbidden in ("label", "return_5d", "entry_close", "exit_close", "close_return_1d"):
        assert forbidden not in row_payload


def test_two_assets_use_the_same_engine_without_asset_branching() -> None:
    eth_asset_id = "crypto:eth-usd"
    eth_source = "deribit:eth-perpetual:funding-rate-history"
    request = _request(asset_id=eth_asset_id)
    points = tuple(
        _point(
            request.requested_start + timedelta(hours=offset),
            source_id=eth_source,
        )
        for offset in range(request.expected_points)
    )
    archive = _archive(
        request,
        points,
        currency="ETH",
        source_id=eth_source,
    )
    bundle = build_funding_bundle(
        base_dataset=_reference(asset_id=eth_asset_id),
        decisions=_decisions(),
        funding_asset=archive.manifest.asset,
        archive=archive,
        known_at=_KNOWN,
    )
    assert bundle.manifest.funding_asset.asset_id == eth_asset_id
    assert bundle.manifest.funding_asset.funding_source_id == eth_source
    assert bundle.rows[0].features[0].value == Decimal("0.000024")


@pytest.mark.parametrize(
    "mutator",
    [
        lambda reference: reference.model_copy(update={"known_at": _KNOWN + timedelta(hours=1)}),
        lambda reference: reference.model_copy(update={"asset_id": "crypto:eth-usd"}),
    ],
)
def test_bundle_rejects_known_at_or_asset_mismatch(mutator) -> None:
    request = _request()
    archive = _archive(request, _points(request))
    reference = mutator(_reference())
    with pytest.raises((FundingFeatureRequestError, ValueError)):
        build_funding_bundle(
            base_dataset=reference,
            decisions=_decisions(),
            funding_asset=archive.manifest.asset,
            archive=archive,
            known_at=_KNOWN,
        )


def test_bundle_rejects_non_contiguous_or_mismatched_decision_grid() -> None:
    start = date(2024, 3, 1)
    end = date(2024, 3, 3)
    request = _request(start, end)
    archive = _archive(request, _points(request))
    with pytest.raises(FundingFeatureRequestError):
        build_funding_bundle(
            base_dataset=_reference(start, end),
            decisions=_decisions(start, end, gap=True),
            funding_asset=archive.manifest.asset,
            archive=archive,
            known_at=_KNOWN,
        )
    with pytest.raises(FundingFeatureRequestError):
        build_funding_bundle(
            base_dataset=_reference(start, date(2024, 3, 4)),
            decisions=_decisions(start, end),
            funding_asset=archive.manifest.asset,
            archive=archive,
            known_at=_KNOWN,
        )


def test_feature_evidence_requires_fixed_window_and_lineage() -> None:
    request = _request()
    bundle = _bundle(request, _points(request))
    evidence = bundle.rows[0].features[0]
    assert evidence.unit == "ratio"
    assert evidence.input_digest is not None
    assert len(evidence.input_digest) == 64
    assert isinstance(evidence, FundingFeatureEvidence)
    with pytest.raises(ValueError):
        FundingFeatureEvidence.model_validate(
            evidence.model_dump() | {"window_start": evidence.window_start + timedelta(hours=1)}
        )
