"""Unit coverage for the sealed SEC provenance repair contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import ValidationError

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.providers.asset_config import AssetClass, SecAssetConfiguration
from investment_analyst.storage.raw_v2 import RawV2TemporalStamp
from investment_analyst.workspace.sec_provenance_repair import (
    SecProvenanceRepairEntry,
    SecProvenanceRepairPlan,
    SecProvenanceRepairScopeError,
    SecProvenanceRepairService,
    corrected_record,
    load_plan,
    stamp_of,
    write_plan,
)

BASE = datetime(2026, 8, 1, 4, 6, 18, 471994, tzinfo=UTC)
LATER = datetime(2026, 10, 21, 20, 49, 52, 509067, tzinfo=UTC)
EXECUTOR = "0123456789abcdef0123456789abcdef01234567"


def _configuration(asset_id: str = "equity:us:aapl") -> SecAssetConfiguration:
    ticker = asset_id.rsplit(":", 1)[-1].upper()
    return SecAssetConfiguration(
        asset_id=asset_id,
        cik="0000320193",
        ticker=ticker,
        submissions_source_id=f"sec-edgar:{ticker.casefold()}:submissions",
        companyfacts_source_id=f"sec-edgar:{ticker.casefold()}:companyfacts",
        name="Fixture Issuer",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )


def _raw(retrieved_at: datetime) -> RawRecord:
    return RawRecord(
        record_id=uuid5(NAMESPACE_URL, "sec-provenance-repair-raw"),
        asset_id="equity:us:aapl",
        source=SourceReference(
            source_id="sec-edgar:aapl:companyfacts",
            record_key="CIK0000320193:company_facts:0123456789abcdef",
            retrieved_at=retrieved_at,
            raw_uri="https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
            checksum_sha256="a" * 64,
        ),
        event_time=LATER,
        available_at=LATER,
        received_at=LATER,
        payload={"body_sha256": "a" * 64, "document": {"facts": {}}},
        schema_version="sec-edgar-companyfacts-snapshot-v1",
    )


def _stamp(retrieved_at: datetime) -> RawV2TemporalStamp:
    return RawV2TemporalStamp(
        available_at=LATER,
        received_at=LATER,
        source_retrieved_at=retrieved_at,
    )


def _entry() -> SecProvenanceRepairEntry:
    return SecProvenanceRepairEntry(
        raw_record_id=uuid5(NAMESPACE_URL, "sec-provenance-repair-raw"),
        asset_id="equity:us:aapl",
        source_id="sec-edgar:aapl:companyfacts",
        schema_version="sec-edgar-companyfacts-snapshot-v1",
        payload_checksum_sha256="a" * 64,
        event_time=LATER,
        before_checksum_sha256="b" * 64,
        after_checksum_sha256="c" * 64,
        before=_stamp(LATER),
        after=_stamp(BASE),
        dependent_observations=2,
        dependent_clocks=(BASE,),
        dependent_digest="d" * 64,
    )


def _plan() -> SecProvenanceRepairPlan:
    return SecProvenanceRepairPlan(
        generated_at=BASE,
        executor_sha=EXECUTOR,
        destination_workspace_id=UUID("70c64668-1738-4dfe-ba1f-7e72026bb7fe"),
        source_workspace_id=UUID("19cf83b5-8df3-4663-9369-4b61f86eceea"),
        catalog_assets=("equity:us:aapl",),
        entries=(_entry(),),
    )


def test_corrected_record_changes_only_the_authorized_timestamp() -> None:
    recorded = _raw(LATER)
    original = _raw(BASE)
    after = _stamp(original.source.retrieved_at)
    assert stamp_of(recorded) != after
    corrected = corrected_record(recorded, after)
    assert corrected.available_at == recorded.available_at
    assert corrected.received_at == recorded.received_at
    assert corrected.event_time == recorded.event_time
    assert corrected.payload == recorded.payload
    assert corrected.source.checksum_sha256 == recorded.source.checksum_sha256
    assert corrected.source.retrieved_at == original.source.retrieved_at


def test_plan_digest_binds_the_sealed_bytes(tmp_path: Path) -> None:
    plan = _plan()
    document = plan.to_json_dict()
    assert document["plan_digest"] == plan.digest()
    path = write_plan(tmp_path / "plan.json", plan)
    loaded = load_plan(path)
    assert loaded == plan
    assert loaded.digest() == plan.digest()
    with pytest.raises(SecProvenanceRepairScopeError, match="must be new"):
        write_plan(path, plan)
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["entries"][0]["after"]["source_retrieved_at"] = LATER.isoformat()
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(SecProvenanceRepairScopeError, match="digest"):
        load_plan(path)


def test_entry_binds_the_exact_repair_authorization() -> None:
    entry = _entry()
    repair = entry.as_repair()
    assert repair.record_id == entry.raw_record_id
    assert repair.before_checksum_sha256 == entry.before_checksum_sha256
    assert repair.after_checksum_sha256 == entry.after_checksum_sha256
    assert repair.before == entry.before
    assert repair.after == entry.after
    with pytest.raises(ValidationError, match="must change at least one timestamp"):
        entry.model_copy(update={"after": entry.before}).as_repair()
    with pytest.raises(ValidationError, match="must change the record bytes"):
        entry.model_copy(update={"after_checksum_sha256": entry.before_checksum_sha256}).as_repair()


def test_service_rejects_an_invalid_scope(tmp_path: Path) -> None:
    destination = _storage(tmp_path / "destination", format_version=2)
    source = _storage(tmp_path / "source", format_version=1)
    with pytest.raises(SecProvenanceRepairScopeError, match="at least one SEC issuer"):
        SecProvenanceRepairService(
            destination,
            source=source,
            configurations=(),
            destination_workspace_id=UUID(int=1),
            source_workspace_id=UUID(int=2),
            executor_sha=EXECUTOR,
        )
    with pytest.raises(SecProvenanceRepairScopeError, match="executor SHA"):
        SecProvenanceRepairService(
            destination,
            source=source,
            configurations=(_configuration(),),
            destination_workspace_id=UUID(int=1),
            source_workspace_id=UUID(int=2),
            executor_sha="ABC",
        )
    with pytest.raises(SecProvenanceRepairScopeError, match="must be unique"):
        SecProvenanceRepairService(
            destination,
            source=source,
            configurations=(_configuration(), _configuration()),
            destination_workspace_id=UUID(int=1),
            source_workspace_id=UUID(int=2),
            executor_sha=EXECUTOR,
        )


def _storage(root: Path, *, format_version: int):
    from investment_analyst.workspace.models import WorkspaceAccessMode
    from investment_analyst.workspace.service import WorkspaceService

    service = WorkspaceService()
    service.initialize(root, format_version=format_version)
    return service.open_storage(service.resolve(root), WorkspaceAccessMode.READ_ONLY)
