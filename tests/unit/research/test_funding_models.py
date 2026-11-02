"""Strict funding contracts, identities, gaps and admission rules."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

import pytest
from pydantic import ValidationError

from investment_analyst.core.models.enums import AssetClass
from investment_analyst.research.funding_archive import build_funding_archive
from investment_analyst.research.funding_models import (
    FUNDING_COLUMN_24H,
    FUNDING_COLUMN_168H,
    FundingAdmission,
    FundingAdmissionStatus,
    FundingArchiveCounts,
    FundingArchivePage,
    FundingArchiveRequest,
    FundingAssetIdentity,
    FundingFeatureEvidence,
    FundingGapCount,
    FundingPoint,
    FundingReason,
    FundingReasonCode,
    FundingSample,
    aggregate_funding_state,
    assumed_funding_available_instant,
    funding_admission,
    funding_archive_request,
    funding_counts,
    funding_feature_hash,
    funding_gap_counts,
    funding_point_id,
    funding_split_coverage,
)
from investment_analyst.research.historical_models import FeatureState, SampleSplit

_ASSET_ID = "crypto:btc-usd"
_SOURCE_ID = "deribit:btc-perpetual:funding-rate-history"
_DECISION = date(2024, 3, 4)
_RETRIEVED = datetime(2026, 11, 2, 0, 0, 30, tzinfo=UTC)


def _asset() -> FundingAssetIdentity:
    return FundingAssetIdentity(
        asset_id=_ASSET_ID,
        funding_source_id=_SOURCE_ID,
        currency="BTC",
        instrument_name="BTC-PERPETUAL",
        asset_class=AssetClass.CRYPTO,
        quote_currency="USD",
        crypto_profile="bitcoin",
    )


def _request(start: date = _DECISION, end: date = _DECISION) -> FundingArchiveRequest:
    return funding_archive_request(
        asset_id=_ASSET_ID,
        decision_start_date=start,
        decision_end_date=end,
    )


def _raw(timestamp: datetime, interest: str = "0.000001") -> dict[str, str]:
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
    page_index: int = 0,
) -> FundingPoint:
    raw = _raw(timestamp, interest)
    return FundingPoint(
        point_id=funding_point_id(
            funding_source_id=_SOURCE_ID,
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


def _archive(request: FundingArchiveRequest | None = None):
    request = request or _request()
    timestamps = tuple(
        request.requested_start + timedelta(hours=offset)
        for offset in range(request.expected_points)
    )
    points = tuple(_point(timestamp) for timestamp in timestamps)
    page = _page(request, points)
    return build_funding_archive(
        asset=_asset(),
        request=request,
        pages=(page,),
        points=points,
        retrieved_at=_RETRIEVED,
    )


def _page(request: FundingArchiveRequest, points: tuple[FundingPoint, ...]) -> FundingArchivePage:
    from investment_analyst.research.funding_archive import _page_bytes, _sha256_bytes

    payload = _page_bytes(points)
    return FundingArchivePage(
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


def _feature(
    column: str,
    *,
    state: FeatureState = FeatureState.AVAILABLE,
    value: Decimal | None = Decimal("0.000024"),
    observed: int | None = None,
    reason: FundingReason | None = None,
) -> FundingFeatureEvidence:
    window = 24 if column == FUNDING_COLUMN_24H else 168
    start, end = _window(column)
    if state is FeatureState.AVAILABLE:
        return FundingFeatureEvidence(
            column_name=column,
            state=state,
            value=value,
            window_start=start,
            window_end=end,
            expected_points=window,
            observed_points=window if observed is None else observed,
            input_digest="a" * 64,
            assumed_available_at=assumed_funding_available_instant(end),
        )
    return FundingFeatureEvidence(
        column_name=column,
        state=state,
        window_start=start,
        window_end=end,
        expected_points=window,
        observed_points=window - 1 if observed is None else observed,
        input_digest="a" * 64,
        assumed_available_at=(
            assumed_funding_available_instant(end)
            if state is FeatureState.NOT_YET_AVAILABLE
            else None
        ),
        reasons=(reason or FundingReason(code=FundingReasonCode.POINTS_ABSENT, detail="gap"),),
    )


def _window(column: str) -> tuple[datetime, datetime]:
    anchor = datetime(2024, 3, 4, 0, 0, tzinfo=UTC)
    window = 24 if column == FUNDING_COLUMN_24H else 168
    return anchor - timedelta(hours=window - 1), anchor


def _sample(
    *,
    features: tuple[FundingFeatureEvidence, ...] | None = None,
    eligibility_base: bool = True,
    split: SampleSplit = SampleSplit.TRAIN,
    purged: bool = False,
) -> FundingSample:
    features = features or (
        _feature(FUNDING_COLUMN_24H),
        _feature(FUNDING_COLUMN_168H, value=Decimal("0.000168")),
    )
    sample_key = uuid5(NAMESPACE_URL, "sample")
    decision_at = datetime(2024, 3, 4, 12, 0, tzinfo=UTC)
    return FundingSample(
        sample_key=sample_key,
        decision_at=decision_at,
        features=features,
        funding_state=aggregate_funding_state(features),
        eligibility_base=eligibility_base,
        eligibility_joint=eligibility_base
        and all(item.state is FeatureState.AVAILABLE for item in features),
        split=split,
        purged=purged,
        funding_feature_hash=funding_feature_hash(
            sample_key=sample_key,
            decision_at=decision_at,
            features=features,
        ),
    )


def test_asset_identity_rejects_non_crypto_quotes_and_source_mismatch() -> None:
    assert _asset().funding_venue == "DERIBIT"
    with pytest.raises(ValidationError):
        FundingAssetIdentity.model_validate(
            _asset().model_dump() | {"asset_class": AssetClass.EQUITY}
        )
    with pytest.raises(ValidationError):
        FundingAssetIdentity.model_validate(_asset().model_dump() | {"quote_currency": "EUR"})
    with pytest.raises(ValidationError):
        FundingAssetIdentity.model_validate(_asset().model_dump() | {"currency": "ETH"})
    with pytest.raises(ValidationError):
        FundingAssetIdentity.model_validate(
            _asset().model_dump()
            | {"funding_source_id": "deribit:eth-perpetual:funding-rate-history"}
        )


def test_archive_request_derives_fixed_168h_plus_one_hour_interval() -> None:
    request = funding_archive_request(
        asset_id=_ASSET_ID,
        decision_start_date=date(2020, 1, 1),
        decision_end_date=date(2025, 12, 25),
    )
    assert request.decision_count == 2186
    assert request.requested_start == datetime(2019, 12, 25, 1, 0, tzinfo=UTC)
    assert request.requested_end == datetime(2025, 12, 25, 1, 0, tzinfo=UTC)
    assert request.expected_points == 52_608
    assert request.decision_dates()[0] == date(2020, 1, 1)
    assert len(request.decision_dates()) == 2186

    with pytest.raises(ValidationError):
        FundingArchiveRequest.model_validate(
            request.model_dump() | {"decision_count": 2185, "decision_start_date": date(2020, 1, 1)}
        )
    with pytest.raises(ValidationError):
        FundingArchiveRequest.model_validate(request.model_dump() | {"expected_points": 52_609})
    with pytest.raises(ValidationError):
        funding_archive_request(
            asset_id=_ASSET_ID,
            decision_start_date=date(2020, 1, 1),
            decision_end_date=date(2026, 12, 31),
        )


def test_point_rejects_float_misalignment_and_payload_mismatch() -> None:
    timestamp = datetime(2024, 3, 4, 0, 0, tzinfo=UTC)
    point = _point(timestamp)
    with pytest.raises(ValidationError):
        FundingPoint.model_validate(point.model_dump() | {"interest_1h": 0.1})
    with pytest.raises(ValidationError):
        FundingPoint.model_validate(
            point.model_dump() | {"timestamp": timestamp + timedelta(minutes=30)}
        )
    with pytest.raises(ValidationError):
        FundingPoint.model_validate(
            point.model_dump() | {"raw_payload": point.raw_payload | {"timestamp": "1"}}
        )
    with pytest.raises(ValidationError):
        FundingPoint.model_validate(
            point.model_dump() | {"assumed_available_at": timestamp + timedelta(hours=2)}
        )
    with pytest.raises(ValidationError):
        FundingPoint.model_validate(
            point.model_dump() | {"raw_payload": {"timestamp": point.raw_payload["timestamp"]}}
        )


def test_archive_counts_and_page_continuity_fail_closed() -> None:
    valid = _archive()
    with pytest.raises(ValidationError):
        FundingArchiveCounts(
            requested_points=24,
            observed_points=23,
            missing_points=0,
            page_count=1,
        )
    with pytest.raises(ValidationError):
        FundingArchiveCounts(
            requested_points=24,
            observed_points=24,
            missing_points=0,
            page_count=73,
        )
    with pytest.raises(ValidationError):
        type(valid.manifest).model_validate(
            valid.manifest.model_dump()
            | {
                "counts": valid.manifest.counts.model_dump()
                | {"requested_points": 25, "missing_points": 1}
            }
        )
    with pytest.raises(ValidationError):
        type(valid.manifest).model_validate(
            valid.manifest.model_dump()
            | {
                "pages": (
                    valid.manifest.pages[0].model_dump()
                    | {
                        "requested_start": valid.manifest.pages[0].requested_start
                        + timedelta(hours=1)
                    },
                )
            }
        )


def test_archive_rejects_duplicate_reordered_and_tampered_points() -> None:
    valid = _archive()
    duplicate = (valid.points[0], valid.points[0])
    with pytest.raises(ValidationError):
        type(valid).model_validate(valid.model_dump() | {"points": duplicate})
    reordered = tuple(reversed(valid.points))
    with pytest.raises(ValidationError):
        type(valid).model_validate(valid.model_dump() | {"points": reordered})
    with pytest.raises(ValidationError):
        type(valid).model_validate(
            valid.model_dump()
            | {"points": (valid.points[0].model_dump() | {"point_id": valid.points[1].point_id},)}
        )
    with pytest.raises(ValidationError):
        type(valid).model_validate(
            valid.model_dump()
            | {"manifest": valid.manifest.model_dump() | {"semantic_sha256": "0" * 64}}
        )


def test_feature_evidence_requires_complete_available_window() -> None:
    available = _feature(FUNDING_COLUMN_24H)
    assert available.value == Decimal("0.000024")
    with pytest.raises(ValidationError):
        FundingFeatureEvidence.model_validate(available.model_dump() | {"observed_points": 23})
    with pytest.raises(ValidationError):
        FundingFeatureEvidence.model_validate(available.model_dump() | {"value": None})
    with pytest.raises(ValidationError):
        FundingFeatureEvidence.model_validate(
            available.model_dump()
            | {"reasons": (FundingReason(code=FundingReasonCode.POINTS_ABSENT, detail="x"),)}
        )
    missing = _feature(FUNDING_COLUMN_24H, state=FeatureState.MISSING)
    with pytest.raises(ValidationError):
        FundingFeatureEvidence.model_validate(missing.model_dump() | {"value": Decimal("0")})
    not_yet = _feature(
        FUNDING_COLUMN_24H,
        state=FeatureState.NOT_YET_AVAILABLE,
        observed=24,
    )
    with pytest.raises(ValidationError):
        FundingFeatureEvidence.model_validate(not_yet.model_dump() | {"observed_points": 23})
    with pytest.raises(ValidationError):
        FundingFeatureEvidence.model_validate(
            available.model_dump() | {"window_start": available.window_start + timedelta(hours=1)}
        )


def test_sample_derives_joint_eligibility_and_feature_hash() -> None:
    sample = _sample()
    assert sample.eligibility_joint is True
    assert sample.funding_state is FeatureState.AVAILABLE
    with pytest.raises(ValidationError):
        FundingSample.model_validate(sample.model_dump() | {"eligibility_base": False})
    with pytest.raises(ValidationError):
        FundingSample.model_validate(sample.model_dump() | {"funding_feature_hash": "0" * 32})
    with pytest.raises(ValidationError):
        _sample(purged=True)
    missing = _sample(
        features=(
            _feature(FUNDING_COLUMN_24H, state=FeatureState.MISSING),
            _feature(FUNDING_COLUMN_168H, value=Decimal("0.000168")),
        )
    )
    assert missing.funding_state is FeatureState.MISSING
    assert missing.eligibility_joint is False


def test_feature_hash_is_independent_from_base_label_metadata() -> None:
    features = (
        _feature(FUNDING_COLUMN_24H),
        _feature(FUNDING_COLUMN_168H, value=Decimal("0.000168")),
    )
    first = _sample(features=features, eligibility_base=True)
    second = _sample(features=features, eligibility_base=False)
    assert first.funding_feature_hash == second.funding_feature_hash
    changed = _feature(FUNDING_COLUMN_24H, value=Decimal("0.000025"))
    third = _sample(features=(changed, features[1]))
    assert first.funding_feature_hash != third.funding_feature_hash


def test_admission_floor_zero_denominator_and_exact_fraction() -> None:
    ready = FundingAdmission.from_counts(
        numerator=95,
        denominator=100,
        train_joint_eligible=150,
        validation_joint_eligible=40,
        holdout_joint_eligible=40,
    )
    assert ready.status is FundingAdmissionStatus.FUNDING_DATA_READY
    assert ready.coverage_fraction == Decimal("0.95")
    assert ready.unmet_requirements == ()

    insufficient = FundingAdmission.from_counts(
        numerator=94,
        denominator=100,
        train_joint_eligible=149,
        validation_joint_eligible=39,
        holdout_joint_eligible=39,
    )
    assert insufficient.status is FundingAdmissionStatus.INSUFFICIENT_FUNDING_COVERAGE
    assert len(insufficient.unmet_requirements) == 4

    empty = FundingAdmission.from_counts(
        numerator=0,
        denominator=0,
        train_joint_eligible=0,
        validation_joint_eligible=0,
        holdout_joint_eligible=0,
    )
    assert empty.coverage_fraction is None
    assert "coverage_denominator is zero" in empty.unmet_requirements
    with pytest.raises(ValidationError):
        FundingAdmission.model_validate(ready.model_dump() | {"coverage_fraction": Decimal("0.94")})
    with pytest.raises(ValidationError):
        FundingAdmission.model_validate(ready.model_dump() | {"status": "insufficient"})


def test_counts_gaps_and_split_coverage_add_up() -> None:
    rows = (
        _sample(),
        _sample(
            features=(
                _feature(FUNDING_COLUMN_24H, state=FeatureState.MISSING),
                _feature(FUNDING_COLUMN_168H, value=Decimal("0.000168")),
            ),
            split=SampleSplit.VALIDATION,
            eligibility_base=False,
        ),
    )
    counts = funding_counts(rows)
    assert counts.requested == 2
    assert counts.feature_24h_missing == 1
    assert counts.feature_168h_available == 2
    assert counts.base_eligible == 1
    assert counts.base_eligible_joint_available == 1
    splits = funding_split_coverage(rows)
    assert splits[0].requested == 1
    assert splits[1].requested == 1
    assert splits[2].requested == 0
    gaps = funding_gap_counts(rows)
    assert gaps == (
        FundingGapCount(
            split=SampleSplit.VALIDATION,
            column_name=FUNDING_COLUMN_24H,
            state=FeatureState.MISSING,
            reason_code=FundingReasonCode.POINTS_ABSENT,
            count=1,
        ),
    )
    admission = funding_admission(splits)
    assert admission.coverage_denominator == 1
    assert admission.coverage_numerator == 1
