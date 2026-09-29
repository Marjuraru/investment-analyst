"""Integration tests for metric v2 staging with shared hourly lineage."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest

from investment_analyst.analytics.evidence_set import build_evidence_segments, build_evidence_set
from investment_analyst.analytics.metric_identity_v2 import metric_result_id_from_model_v2
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.storage.metric_v2 import MetricV2Error
from investment_analyst.storage.raw_v2 import RawV2Staging

_BASE = datetime(2026, 8, 1, tzinfo=UTC)
_FUNDING_SOURCE = "deribit:funding"


def _staging(tmp_path: Path, name: str) -> RawV2Staging:
    destination = (tmp_path / name).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(destination / "metric-v2-index.duckdb"))
    return RawV2Staging(destination, connection)


def _seed_hourly(
    staging: RawV2Staging, count: int, *, asset: str = "crypto:btc-usd"
) -> list[NormalizedObservation]:
    observations: list[NormalizedObservation] = []
    for index in range(count):
        moment = _BASE + timedelta(hours=index)
        raw = RawRecord(
            record_id=uuid4(),
            asset_id=asset,
            source=SourceReference(
                source_id=_FUNDING_SOURCE, record_key=f"m-{asset}-{index}", retrieved_at=moment
            ),
            event_time=moment,
            available_at=moment,
            received_at=moment,
            payload={"v": str(index)},
            schema_version="metric-v2-integration",
        )
        staging.save(raw)
        observations.append(
            NormalizedObservation(
                observation_id=uuid4(),
                raw_record_id=raw.record_id,
                asset_id=asset,
                field_name="funding_rate",
                value=Decimal(f"0.00{index % 10}"),
                unit="rate",
                frequency=DataFrequency.HOUR_1,
                observed_at=moment,
                available_at=moment,
                normalized_at=moment,
                source=raw.source,
                quality=DataQuality.VALID,
                transformation_version="1.0.0",
            )
        )
    receipt = staging.save_observations(observations)
    assert receipt.created_count == count
    return observations


def _metric(
    observations: list[NormalizedObservation],
    evidence_set_id: object,
    *,
    key: str,
    value: Decimal,
    as_of: datetime,
    available_at: datetime,
    dependencies: list[MetricResult] | None = None,
    asset: str = "crypto:btc-usd",
) -> MetricResult:
    candidate = MetricResult(
        result_id=uuid4(),
        asset_id=asset,
        metric_key=key,
        value=value,
        unit="rate",
        as_of=as_of,
        available_at=available_at,
        computed_at=available_at,
        parameters={"window": len(observations), "evidence_set_id": str(evidence_set_id)},
        input_observation_ids=[item.observation_id for item in observations],
        input_metric_result_ids=[item.result_id for item in (dependencies or [])],
        algorithm_version="metric-v2-integration",
        quality=DataQuality.VALID,
    )
    return candidate.model_copy(update={"result_id": metric_result_id_from_model_v2(candidate)})


def test_market_metric_pit_revisions_and_dependencies(tmp_path: Path) -> None:
    staging = _staging(tmp_path, "staging")
    with staging:
        observations = _seed_hourly(staging, 48)
        early = [item for item in observations if item.observed_at < _BASE + timedelta(hours=24)]
        daily = []
        for index in range(3):
            moment = _BASE + timedelta(days=index)
            raw = RawRecord(
                record_id=uuid4(),
                asset_id="equity:us:aapl",
                source=SourceReference(
                    source_id="alpaca:bars", record_key=f"d-{index}", retrieved_at=moment
                ),
                event_time=moment,
                available_at=moment,
                received_at=moment,
                payload={"close": "210.50"},
                schema_version="metric-v2-integration",
            )
            staging.save(raw)
            daily.append(
                NormalizedObservation(
                    observation_id=uuid4(),
                    raw_record_id=raw.record_id,
                    asset_id="equity:us:aapl",
                    field_name="close",
                    value=Decimal(f"{210 + index}.50"),
                    unit="USD",
                    frequency=DataFrequency.DAY_1,
                    observed_at=moment,
                    available_at=moment,
                    normalized_at=moment,
                    source=raw.source,
                    quality=DataQuality.VALID,
                    transformation_version="1.0.0",
                )
            )
        assert staging.save_observations(daily).created_count == 3
        segments = build_evidence_segments(observations)
        assert staging.save_evidence_segments(segments) == 2
        evidence_set = build_evidence_set(observations, segments=segments)
        assert staging.save_evidence_set(evidence_set) is True
        window_end = _BASE + timedelta(hours=47)
        first = _metric(
            observations,
            evidence_set.evidence_set_id,
            key="funding.sum_1h",
            value=Decimal("1.5"),
            as_of=window_end,
            available_at=evidence_set.available_at,
        )
        receipt = staging.save_metrics([first])
        assert receipt.created_count == 1
        revision = _metric(
            observations,
            evidence_set.evidence_set_id,
            key="funding.mean_1h",
            value=Decimal("0.03"),
            as_of=window_end,
            available_at=evidence_set.available_at,
            dependencies=[first],
        )
        assert staging.save_metrics([revision]).created_count == 1
        hydrated = staging.get_metrics([first.result_id, revision.result_id])
        assert hydrated[first.result_id] == first
        assert list(hydrated[revision.result_id].input_metric_result_ids) == [first.result_id]
        early_cut = staging.list_metrics(
            asset_id="crypto:btc-usd", available_to=_BASE + timedelta(hours=25)
        )
        assert early_cut == []
        full_cut = staging.list_metrics(
            asset_id="crypto:btc-usd", available_to=evidence_set.available_at
        )
        assert {item.result_id for item in full_cut} == {first.result_id, revision.result_id}
        day_metric = _metric(
            daily,
            evidence_set.evidence_set_id,
            key="market.close.mean",
            value=Decimal("211.50"),
            as_of=_BASE + timedelta(days=2),
            available_at=daily[-1].available_at,
            asset="equity:us:aapl",
        )
        day_metric = day_metric.model_copy(
            update={"parameters": {"window": len(daily), "note": "daily-explicit-links"}}
        )
        day_metric = day_metric.model_copy(
            update={"result_id": metric_result_id_from_model_v2(day_metric)}
        )
        assert staging.save_metrics([day_metric]).created_count == 1
        rerun = first.model_copy(update={"computed_at": first.computed_at + timedelta(hours=1)})
        rerun = rerun.model_copy(update={"result_id": metric_result_id_from_model_v2(rerun)})
        assert rerun.result_id == first.result_id
        repeated = staging.save_metrics([rerun])
        assert repeated.created_count == 0
        assert repeated.reused_count == 1
        assert staging.get_metrics([first.result_id])[first.result_id] == first
        assert len(early) == 24
    staging.close()


def test_funding_sum_mean_share_verified_lineage(tmp_path: Path) -> None:
    staging = _staging(tmp_path, "staging")
    with staging:
        observations = _seed_hourly(staging, 72)
        segments = build_evidence_segments(observations)
        assert staging.save_evidence_segments(segments) == 3
        evidence_set = build_evidence_set(observations, segments=segments)
        assert staging.save_evidence_set(evidence_set) is True
        assert evidence_set.input_count == 72
        window_end = _BASE + timedelta(hours=71)
        total = _metric(
            observations,
            evidence_set.evidence_set_id,
            key="funding.sum_1h",
            value=Decimal("2.5"),
            as_of=window_end,
            available_at=evidence_set.available_at,
        )
        mean = _metric(
            observations,
            evidence_set.evidence_set_id,
            key="funding.mean_1h",
            value=Decimal("0.05"),
            as_of=window_end,
            available_at=evidence_set.available_at,
        )
        receipt = staging.save_metrics([total, mean])
        assert receipt.created_count == 2
        both = staging.get_metrics([total.result_id, mean.result_id])
        assert {str(item.parameters["evidence_set_id"]) for item in both.values()} == {
            str(evidence_set.evidence_set_id)
        }
        stored = staging.get_evidence_set(evidence_set.evidence_set_id)
        assert stored == evidence_set
        assert staging.resolve_metric_lineage(both[total.result_id]) == total
    staging.close()


def test_missing_future_or_foreign_input_fails_closed(tmp_path: Path) -> None:
    staging = _staging(tmp_path, "staging")
    with staging:
        observations = _seed_hourly(staging, 24)
        segments = build_evidence_segments(observations)
        staging.save_evidence_segments(segments)
        evidence_set = build_evidence_set(observations, segments=segments)
        staging.save_evidence_set(evidence_set)
        ghost = _metric(
            observations,
            evidence_set.evidence_set_id,
            key="funding.sum_1h",
            value=Decimal("9.0"),
            as_of=_BASE + timedelta(hours=23),
            available_at=evidence_set.available_at,
        ).model_copy(update={"input_observation_ids": [uuid4()]})
        ghost = ghost.model_copy(update={"result_id": metric_result_id_from_model_v2(ghost)})
        with pytest.raises(MetricV2Error, match="missing observation"):
            staging.save_metrics([ghost])
        foreign = _metric(
            observations,
            evidence_set.evidence_set_id,
            key="funding.sum_1h",
            value=Decimal("9.0"),
            as_of=_BASE + timedelta(hours=23),
            available_at=evidence_set.available_at,
            asset="equity:us:aapl",
        )
        with pytest.raises(MetricV2Error, match="foreign|missing"):
            staging.save_metrics([foreign])
        assert staging.list_metrics(asset_id="crypto:btc-usd") == []
    staging.close()
