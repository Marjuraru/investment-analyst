"""Append-only persistence, receipts, revisions, and late-failure coverage."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from investment_analyst.catalog.provider_configuration import resolve_deribit_configuration
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.providers.crypto.deribit import DeribitClient, DeribitError
from investment_analyst.providers.crypto.deribit_pipeline import (
    DeribitEvidencePipeline,
    list_deribit_receipts,
)
from investment_analyst.providers.http import HttpResponse
from investment_analyst.storage import LocalStorage, StoragePaths

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "deribit"
_START = datetime(2026, 8, 1, tzinfo=UTC)
_END = datetime(2026, 8, 3, tzinfo=UTC)
_RECEIVED = datetime(2026, 8, 4, tzinfo=UTC)


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
        if not self._bodies:
            raise AssertionError("unexpected Deribit request")
        return HttpResponse(status_code=200, body=self._bodies.pop(0), headers={}, url=url)


def _configuration():
    return resolve_deribit_configuration(
        ProviderAssetContextResolver(AssetCatalogService.load_default()),
        asset_id="crypto:btc-usd",
    )


def _pipeline(
    storage: LocalStorage,
    *bodies: bytes,
    received_at: datetime = _RECEIVED,
) -> DeribitEvidencePipeline:
    return DeribitEvidencePipeline(
        storage,
        DeribitClient(
            _Transport(*bodies),
            sleep=lambda _: None,
            clock=lambda: received_at,
        ),
        configuration=_configuration(),
        clock=lambda: received_at,
    )


def test_complete_stages_round_trip_and_rerun_reuses_first_availability(
    tmp_path: Path,
) -> None:
    funding_body = (_FIXTURES / "btc_funding_history.json").read_bytes()
    dvol_body = (_FIXTURES / "btc_dvol_daily.json").read_bytes()
    summary_body = (_FIXTURES / "btc_perpetual_summary.json").read_bytes()
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        first = _pipeline(storage, funding_body, dvol_body, summary_body)
        funding = first.import_funding(_START, _END)
        dvol = first.import_dvol(_START, _END)
        summary = first.capture_summary()
        first_records = tuple(storage.raw_records.list())
        first_observations = tuple(storage.observations.list(asset_id="crypto:btc-usd"))

        second = _pipeline(
            storage,
            funding_body,
            dvol_body,
            summary_body,
            received_at=_RECEIVED + timedelta(days=1),
        )
        funding_again = second.import_funding(_START, _END)
        dvol_again = second.import_dvol(_START, _END)
        summary_again = second.capture_summary()

        assert funding.receipt_created and dvol.receipt_created
        assert funding.receipt_id == UUID("d58ac53d-7d4c-54fd-9971-52758d76fb70")
        assert not funding_again.receipt_created and not dvol_again.receipt_created
        assert funding_again.raw_records_created == dvol_again.raw_records_created == 0
        assert summary_again.raw_records_created == 0
        assert funding_again.observations_created == dvol_again.observations_created == 0
        assert summary_again.observations_created == 0
        assert tuple(storage.raw_records.list()) == first_records
        assert tuple(storage.observations.list(asset_id="crypto:btc-usd")) == first_observations
        assert all(record.available_at == record.received_at for record in first_records)
        receipts = (
            *list_deribit_receipts(
                storage,
                source_id=_configuration().funding_source_id,
                dataset="funding_history",
            ),
            *list_deribit_receipts(
                storage,
                source_id=_configuration().dvol_source_id,
                dataset="dvol_daily",
            ),
        )
        assert len(receipts) == 2
        receipt_ids = {item.receipt_id for item in receipts}
        assert not any(item.raw_record_id in receipt_ids for item in first_observations)
        assert funding.traceability_verified and dvol.traceability_verified
        assert summary.traceability_verified


def test_same_event_correction_creates_append_only_revision(tmp_path: Path) -> None:
    original = json.loads((_FIXTURES / "btc_funding_history.json").read_text())
    corrected = json.loads(json.dumps(original))
    corrected["result"][0]["interest_1h"] = 0.000009
    original_body = json.dumps(original).encode()
    corrected_body = json.dumps(corrected).encode()
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        first = _pipeline(storage, original_body).import_funding(_START, _END)
        second = _pipeline(
            storage,
            corrected_body,
            received_at=_RECEIVED + timedelta(hours=1),
        ).import_funding(_START, _END)

        assert first.raw_records_created == 2
        assert second.raw_records_created == 1
        assert second.raw_records_reused == 1
        observations = storage.observations.list(
            asset_id="crypto:btc-usd",
            frequency=None,
        )
        revisions = [
            item
            for item in observations
            if item.field_name == "funding_interest_1h" and item.observed_at == _START
        ]
        assert len(revisions) == 2
        assert {str(item.value) for item in revisions} == {"0.000001", "0.000009"}


def test_dvol_page_failure_writes_no_partial_rows_or_receipt(tmp_path: Path) -> None:
    continuation = 1785628800000
    first_page = json.dumps(
        {
            "jsonrpc": "2.0",
            "result": {
                "data": [[1785628800000, 56, 58, 55, 57]],
                "continuation": continuation,
            },
        }
    ).encode()
    invalid_second_page = b'{"jsonrpc":"2.0","result":{"data":'
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        with pytest.raises(DeribitError, match="invalid JSON"):
            _pipeline(storage, first_page, invalid_second_page).import_dvol(_START, _END)

        assert storage.raw_records.list(source_id=_configuration().dvol_source_id) == []
        assert storage.observations.list(asset_id="crypto:btc-usd") == []


def test_late_stage_failure_preserves_completed_funding_stage(tmp_path: Path) -> None:
    funding_body = (_FIXTURES / "btc_funding_history.json").read_bytes()
    invalid_dvol = b'{"jsonrpc":"2.0","result":'
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        pipeline = _pipeline(storage, funding_body, invalid_dvol)
        funding = pipeline.import_funding(_START, _END)
        with pytest.raises(DeribitError):
            pipeline.import_dvol(_START, _END)

        assert funding.receipt_id is not None
        receipts = list_deribit_receipts(
            storage,
            source_id=_configuration().funding_source_id,
            dataset="funding_history",
        )
        assert len(receipts) == 1
        assert len(storage.observations.list(asset_id="crypto:btc-usd")) == 8


def test_empty_completed_interval_creates_only_reusable_receipt(tmp_path: Path) -> None:
    empty = json.dumps({"jsonrpc": "2.0", "result": []}).encode()
    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        first = _pipeline(storage, empty).import_funding(_START, _END)
        second = _pipeline(
            storage,
            empty,
            received_at=_RECEIVED + timedelta(days=1),
        ).import_funding(_START, _END)

        assert first.rows_received == 0
        assert first.receipt_created is True
        assert second.receipt_created is False
        assert second.receipt_id == first.receipt_id
        assert storage.observations.list(asset_id="crypto:btc-usd") == []
        assert len(storage.raw_records.list(source_id=_configuration().funding_source_id)) == 1
