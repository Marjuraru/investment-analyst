"""Isolated v1/v2 operational state cutover integration coverage."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.alerts.candidate_notifications import (
    CandidateNotification,
    CandidateNotificationPayload,
    CandidateNotificationStore,
)
from investment_analyst.alerts.candidate_notifications import (
    notification_id as candidate_notification_id,
)
from investment_analyst.alerts.cazatiburones_notification_models import (
    CazatiburonesNotification,
)
from investment_analyst.alerts.cazatiburones_notification_models import (
    notification_id as caz_notification_id,
)
from investment_analyst.alerts.cazatiburones_notifications import CazatiburonesNotificationStore
from investment_analyst.analytics.cazatiburones.activity_event_models import (
    ActivityCandidate,
    ActivityEvent,
    ActivityEventSnapshot,
)
from investment_analyst.analytics.cazatiburones.activity_event_repository import (
    ActivityEventRepository,
)
from investment_analyst.analytics.cazatiburones.institutional_event_models import (
    InstitutionalCandidate,
    InstitutionalEvent,
    InstitutionalEventSnapshot,
)
from investment_analyst.analytics.cazatiburones.institutional_event_repository import (
    InstitutionalEventRepository,
)
from investment_analyst.application.manual_operations import (
    ManualOperationKind,
    ManualOperationRequest,
    ManualOperationState,
    ManualOperationStateStore,
    ManualOperationStatus,
)
from investment_analyst.application.multi_asset_scheduler import (
    MultiAssetScheduleState,
    MultiAssetScheduleStateStore,
    ScheduledJobAttempt,
    ScheduledJobAttemptStatus,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobExecution,
)
from investment_analyst.application.operational_models import (
    AaplDailyRunState,
    AaplDailyRunStatus,
    OperationalRefreshRequestSnapshot,
)
from investment_analyst.application.operational_state import AaplDailyRunStateStore
from investment_analyst.application.sec_institutional_cycle_state import (
    SecInstitutionalCycleState,
    SecInstitutionalCycleStateStore,
)
from investment_analyst.application.storage_observability import (
    StorageObservabilityDurations,
    StorageObservabilityRecord,
    parse_storage_observability_state,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_cutover import WorkspaceV2CutoverService
from investment_analyst.workspace.workspace_v2_migration import migrate_workspace_v1_to_v2
from investment_analyst.workspace.workspace_v2_operational_state import (
    OperationalStateTransferError,
    compare_operational_models,
    operational_model_projection,
)

_MOMENT = datetime(2026, 10, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    "fault", ["foreign_workspace", "foreign_source", "malformed_control", "unknown_entry"]
)
def test_destination_controls_require_models_and_actual_identity(
    tmp_path: Path, fault: str
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source, destination = tmp_path / "source", tmp_path / "destination"
    service.initialize(source, format_version=1)
    migrate_workspace_v1_to_v2(
        service=service,
        source_root=source,
        destination_root=destination,
        host_free_bytes=lambda: 30_000_000_000,
        clock=lambda: _MOMENT,
    )
    state = destination / "state"
    if fault == "foreign_workspace":
        path = state / "workspace-v2-migration-identity.json"
        document = json.loads(path.read_bytes())
        document["destination_workspace_id"] = str(uuid4())
        payload = json.dumps(document).encode()
    elif fault == "foreign_source":
        path = state / "direct-historical-import-state.json"
        document = json.loads(path.read_bytes())
        document["source_workspace_id"] = str(uuid4())
        payload = json.dumps(document).encode()
    elif fault == "malformed_control":
        path = state / "direct-historical-import-state.json"
        payload = b"{}\n"
    else:
        path = state / ".unique-destination-state"
        payload = b"unique local history\n"
    path.write_bytes(payload)
    with pytest.raises(OperationalStateTransferError):
        operational_model_projection(destination)
    assert path.read_bytes() == payload


def _seed_operational_fixture(root: Path) -> None:
    state = root / "state"
    processed = root / "storage/data/processed"
    AaplDailyRunStateStore(state / "aapl_daily_run_state.json").write(
        AaplDailyRunState(
            run_id=uuid4(),
            status=AaplDailyRunStatus.RUNNING,
            workspace_root=root,
            request=OperationalRefreshRequestSnapshot(
                asset_id="equity:us:aapl",
                market_start=date(2026, 10, 1),
                market_end=date(2026, 10, 14),
                fundamental_frequency=DataFrequency.ANNUAL,
                refresh_mode="auto",
                require_complete=False,
            ),
            started_at=_MOMENT,
        )
    )
    definition = ScheduledJobDefinition(
        job_id="fixture:market",
        asset_id="equity:us:aapl",
        provider="fixture",
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
        timezone="UTC",
        run_at=time(0),
    )
    attempts = tuple(
        ScheduledJobAttempt(
            attempt_id=uuid4(),
            definition=definition,
            local_date=_MOMENT.date(),
            scheduled_for=_MOMENT,
            attempt_number=number + 1,
            status=ScheduledJobAttemptStatus.SUCCEEDED,
            started_at=_MOMENT + timedelta(seconds=number),
            completed_at=_MOMENT + timedelta(seconds=number + 1),
            execution=ScheduledJobExecution(
                job_id=definition.job_id,
                effective_known_at=_MOMENT,
                evidence_changed=False,
                source_ids=("fixture:source",),
                created_count=0,
                reused_count=1,
            ),
        )
        for number in range(3)
    )
    scheduler_path = state / "multi_asset_schedule_state_v1.json"
    scheduler_path.write_text(MultiAssetScheduleState(attempts=(attempts[0],)).model_dump_json())
    scheduler = MultiAssetScheduleStateStore(scheduler_path, max_segment_records=1)
    for attempt in attempts[1:]:
        scheduler.write_attempt(attempt)
    (scheduler.journal_dir / ".legacy_v1_preserved").write_text("legacy marker\n")
    candidates = CandidateNotificationStore(state / "candidate_notification_outbox_state_v1.json")
    caz = CazatiburonesNotificationStore(state / "cazatiburones_notification_outbox_state_v1.json")
    events: list[ActivityEvent] = []
    event_candidates: list[ActivityCandidate] = []
    snapshot_id = uuid4()
    for number in range(2):
        moment = _MOMENT + timedelta(seconds=number)
        candidate_id = uuid4()
        event = ActivityEvent(
            event_id=uuid4(),
            asset_id="equity:us:aapl",
            rule_id="fixture:activity",
            metric_result_id=uuid4(),
            metric_key="fixture:activity",
            unit="ratio",
            value=Decimal("1.000000000000000001"),
            available_at=moment,
            input_observation_ids=(uuid4(), uuid4()),
            parameters={"algorithm_version": "fixture-v1"},
        )
        events.append(event)
        event_candidates.append(
            ActivityCandidate(candidate_id=candidate_id, event_id=event.event_id, status="eligible")
        )
        candidate = CandidateNotification(
            notification_id=candidate_notification_id(candidate_id),
            candidate_id=candidate_id,
            activation_result_id=event.metric_result_id,
            asset_id=event.asset_id,
            rule_id=event.rule_id,
            as_of=moment,
            created_at=moment,
            payload=CandidateNotificationPayload(
                rule_name_es="Prueba",
                explanation_es="Evidencia sintética.",
                conditions=(
                    {
                        "condition_id": "fixture:activity",
                        "state": "met",
                        "metric_key": "fixture:activity",
                        "unit": "ratio",
                        "explanation_es": "Condición sintética.",
                    },
                ),
            ),
        )
        candidates.enqueue(candidate)
        item = CazatiburonesNotification(
            family="activity",
            notification_id=caz_notification_id("activity", candidate_id),
            candidate_id=candidate_id,
            event_id=event.event_id,
            metric_result_id=event.metric_result_id,
            snapshot_id=snapshot_id,
            asset_id=event.asset_id,
            rule_id=event.rule_id,
            metric_key=event.metric_key,
            algorithm_version="fixture-v1",
            unit=event.unit,
            value=event.value,
            available_at=moment,
            created_at=moment,
            input_observation_ids=event.input_observation_ids,
        )
        caz.enqueue(item)
        if number == 0:
            candidates.acknowledge(
                candidate.notification_id, recorded_at=_MOMENT + timedelta(seconds=10)
            )
            caz.acknowledge(item.notification_id, recorded_at=_MOMENT + timedelta(seconds=10))
    ActivityEventRepository(processed, read_only=False).save(
        ActivityEventSnapshot(
            snapshot_id=snapshot_id,
            asset_id="equity:us:aapl",
            known_at=_MOMENT + timedelta(seconds=2),
            recorded_at=_MOMENT + timedelta(seconds=3),
            policy_version="fixture-v1",
            evaluations=(),
            events=tuple(events),
            candidates=tuple(event_candidates),
        )
    )
    institutional = InstitutionalEvent(
        event_id=uuid4(),
        asset_id="equity:us:aapl",
        manager_cik="0001067983",
        report_period="2026-06-30",
        prior_report_period="2026-03-31",
        cusip="037833100",
        title_of_class="COM",
        rule_id="fixture:institutional",
        metric_result_id=uuid4(),
        metric_key="fixture:institutional",
        algorithm_version="fixture-v1",
        unit="shares",
        value=Decimal("1"),
        available_at=_MOMENT,
        input_observation_ids=(uuid4(),),
        parameters={},
    )
    institutional_id = uuid4()
    InstitutionalEventRepository(processed).save(
        InstitutionalEventSnapshot(
            snapshot_id=uuid4(),
            asset_id=institutional.asset_id,
            manager_cik=institutional.manager_cik,
            known_at=_MOMENT,
            recorded_at=_MOMENT + timedelta(seconds=1),
            policy_version="fixture-v1",
            evaluations=(),
            events=(institutional,),
            candidates=(
                InstitutionalCandidate(
                    candidate_id=uuid4(), event_id=institutional.event_id, status="eligible"
                ),
            ),
        )
    )
    SecInstitutionalCycleStateStore(state / "sec_institutional_cycle_state_v1.json").write(
        SecInstitutionalCycleState(
            updated_at=_MOMENT,
            dataset_period_start=date(2026, 4, 1),
            dataset_period_end=date(2026, 6, 30),
            dataset_url="https://www.sec.gov/files/fixture.zip",
            dataset_sha256="a" * 64,
            dataset_last_validated_at=_MOMENT,
            snapshot_id=institutional_id,
            manager_cursor=1,
            total_managers=2,
            cycle_count=1,
            last_processed_cik=institutional.manager_cik,
            last_status="success",
        )
    )
    operations = ManualOperationStateStore(state / "manual_operation_state_v1.json")
    for number, status in enumerate((ManualOperationStatus.RUNNING, ManualOperationStatus.QUEUED)):
        request = ManualOperationRequest(
            operation_kind=ManualOperationKind.MARKET_DAILY,
            payload={
                "asset_id": "crypto:btc-usd",
                "market_start": "2026-10-01",
                "market_end": f"2026-10-{13 + number}",
                "refresh_mode": "auto",
                "requested_known_at": "2026-10-15T00:00:00Z",
            },
        )
        operations.write(
            ManualOperationState(
                operation_id=uuid4(),
                fingerprint=request.fingerprint,
                request=request,
                status=status,
                submitted_at=_MOMENT + timedelta(seconds=number),
                started_at=_MOMENT if status is ManualOperationStatus.RUNNING else None,
            )
        )
    telemetry = StorageObservabilityRecord(
        observed_at=_MOMENT,
        attempt_id=attempts[0].attempt_id,
        job_id=definition.job_id,
        attempt_number=1,
        local_date=_MOMENT.date(),
        attempt_status="succeeded",
        evidence_changed=False,
        rows_created=0,
        rows_reused=1,
        database_bytes_before=100,
        database_bytes_after=100,
        wal_bytes_before=0,
        wal_bytes_after=0,
        durations=StorageObservabilityDurations(
            total_ms=10,
            job_execution_ms=7,
            query_ms=1,
            collector_unattributed_ms=1,
            persistence_ms=1,
            verification_ms=0,
        ),
    )
    (state / "storage_observability_v1.jsonl").write_text(telemetry.model_dump_json() + "\n")


def test_nonempty_cut_preserves_models_journal_outboxes_and_historic_roots(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source, destination = tmp_path / "source", tmp_path / "destination"
    service.initialize(source, format_version=1)
    migrate_workspace_v1_to_v2(
        service=service,
        source_root=source,
        destination_root=destination,
        host_free_bytes=lambda: 30_000_000_000,
        clock=lambda: _MOMENT,
    )
    _seed_operational_fixture(source)
    cutover = WorkspaceV2CutoverService(service, clock=lambda: _MOMENT + timedelta(hours=1))
    cut = cutover.freeze(source, operation_id=uuid4(), writer_closed=True)
    receipt = cutover.complete_operational_state(
        cut,
        destination,
        inventory_path=tmp_path / "inventory.jsonl",
        seal_path=tmp_path / "seal.json",
        checkpoint_path=tmp_path / "checkpoint.json",
        receipt_path=tmp_path / "receipt.json",
        host_free_bytes=lambda: 30_000_000_000,
        reserve_bytes=5_000_000_000,
    )
    assert receipt.artifact_count > 10
    assert len(compare_operational_models(source, destination)) == 15
    loaders = (
        ("aapl_daily_run_state.json", AaplDailyRunStateStore),
        ("multi_asset_schedule_state_v1.json", MultiAssetScheduleStateStore),
        ("candidate_notification_outbox_state_v1.json", CandidateNotificationStore),
        ("cazatiburones_notification_outbox_state_v1.json", CazatiburonesNotificationStore),
        ("sec_institutional_cycle_state_v1.json", SecInstitutionalCycleStateStore),
        ("manual_operation_state_v1.json", ManualOperationStateStore),
    )
    for relative, store in loaders:
        assert (
            store(source / "state" / relative).load()
            == store(destination / "state" / relative).load()
        )
    daily = AaplDailyRunStateStore(destination / "state/aapl_daily_run_state.json").load()
    assert daily is not None and daily.workspace_root == source
    scheduler = MultiAssetScheduleStateStore(
        destination / "state/multi_asset_schedule_state_v1.json"
    )
    assert len(scheduler.load().attempts) == 3
    assert any(path.name.startswith(".") for path in scheduler.journal_dir.iterdir())
    for repository in (ActivityEventRepository, InstitutionalEventRepository):
        assert (
            repository(source / "storage/data/processed", read_only=True).list_snapshots()
            == repository(destination / "storage/data/processed", read_only=True).list_snapshots()
        )
    assert parse_storage_observability_state(
        (source / "state/storage_observability_v1.jsonl").read_text()
    ) == parse_storage_observability_state(
        (destination / "state/storage_observability_v1.jsonl").read_text()
    )
    outbox = CandidateNotificationStore(
        destination / "state/candidate_notification_outbox_state_v1.json"
    )
    original = outbox.load()
    for item in original.items:
        assert outbox.enqueue(item)[1] is False
    assert outbox.load() == original


def test_isolated_cutover_seals_and_transfers_an_empty_operational_partition(
    tmp_path: Path,
) -> None:
    """A v1/v2 fixture has explicit absences and needs no provider or financial replay."""
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    service.initialize(source, format_version=1)
    migrate_workspace_v1_to_v2(
        service=service,
        source_root=source,
        destination_root=destination,
        page_limit=1,
        host_free_bytes=lambda: 30_000_000_000,
        clock=lambda: _MOMENT,
    )
    cutover = WorkspaceV2CutoverService(service, clock=lambda: _MOMENT)
    frozen = cutover.freeze(source, operation_id=uuid4(), writer_closed=True)

    receipt = cutover.complete_operational_state(
        frozen,
        destination,
        inventory_path=tmp_path / "operational-inventory.jsonl",
        seal_path=tmp_path / "operational-seal.json",
        checkpoint_path=tmp_path / "operational-checkpoint.json",
        receipt_path=tmp_path / "operational-receipt.json",
        host_free_bytes=lambda: 10_000_000,
        reserve_bytes=1,
    )

    assert receipt.artifact_count == 0
    assert receipt.completed_families == ()
    assert (
        cutover.complete_operational_state(
            frozen,
            destination,
            inventory_path=tmp_path / "operational-inventory.jsonl",
            seal_path=tmp_path / "operational-seal.json",
            checkpoint_path=tmp_path / "operational-checkpoint.json",
            receipt_path=tmp_path / "operational-receipt.json",
            host_free_bytes=lambda: 10_000_000,
            reserve_bytes=1,
        )
        == receipt
    )
