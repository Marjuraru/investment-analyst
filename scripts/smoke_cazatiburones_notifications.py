#!/usr/bin/env python3
"""Run the Cazatiburones notification flow using only temporary local artifacts."""

import json
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

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
from investment_analyst.application.cazatiburones_notifications import (
    CazatiburonesNotificationsApplication,
)
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.storage import StoragePaths
from investment_analyst.workspace.service import WorkspaceService

_ASSET_ID = "equity:us:aapl"
_ACTIVITY_POLICY = "cazatiburones-persisted-activity-events-v1"
_INSTITUTIONAL_POLICY = "cazatiburones-persisted-institutional-events-v1"
_METRIC_ALGORITHM = "cazatiburones-institutional-metrics-v1"
_MANAGER_CIK = "0001350694"
_T0 = datetime(2026, 1, 2, 12, tzinfo=UTC)


def _identifier(label: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"investment-analyst:smoke:{label}")


def _activity_snapshot(snapshot_label: str, recorded_at: datetime) -> ActivityEventSnapshot:
    event = ActivityEvent(
        event_id=_identifier("activity-event"),
        asset_id=_ASSET_ID,
        rule_id="insider-holding-increased",
        metric_result_id=_identifier("activity-metric-result"),
        metric_key="cazatiburones.insider.holding_delta_ratio",
        unit="ratio",
        value=Decimal("1.250000000000000001"),
        available_at=_T0,
        input_observation_ids=(
            _identifier("activity-observation-1"),
            _identifier("activity-observation-2"),
        ),
        parameters={
            "algorithm_version": _ACTIVITY_POLICY,
            "participant_cik": "0000000001",
        },
    )
    candidate = ActivityCandidate(
        candidate_id=_identifier("activity-candidate"),
        event_id=event.event_id,
        status="eligible",
    )
    return ActivityEventSnapshot(
        snapshot_id=_identifier(snapshot_label),
        asset_id=_ASSET_ID,
        known_at=_T0 + timedelta(days=2),
        recorded_at=recorded_at,
        policy_version=_ACTIVITY_POLICY,
        evaluations=(),
        events=(event,),
        candidates=(candidate,),
    )


def _institutional_snapshot(recorded_at: datetime) -> InstitutionalEventSnapshot:
    eligible_event = InstitutionalEvent(
        event_id=_identifier("institutional-event"),
        asset_id=_ASSET_ID,
        manager_cik=_MANAGER_CIK,
        report_period="2025-12-31",
        prior_report_period="2025-09-30",
        cusip="037833100",
        title_of_class="COM",
        put_call=None,
        rule_id="institutional-delta-reported-shares-increased",
        metric_result_id=_identifier("institutional-metric-result"),
        metric_key="cazatiburones.institutional.delta_reported_shares",
        algorithm_version=_METRIC_ALGORITHM,
        unit="shares",
        value=Decimal("2500.000000000000000001"),
        available_at=_T0 + timedelta(hours=1),
        input_observation_ids=(
            _identifier("institutional-observation-1"),
            _identifier("institutional-observation-2"),
        ),
        parameters={
            "manager_cik": _MANAGER_CIK,
            "report_period": "2025-12-31",
            "prior_report_period": "2025-09-30",
            "cusip": "037833100",
            "title_of_class": "COM",
            "put_call": None,
        },
    )
    suppressed_event = InstitutionalEvent(
        event_id=_identifier("institutional-suppressed-event"),
        asset_id=_ASSET_ID,
        manager_cik=_MANAGER_CIK,
        report_period="2026-03-31",
        prior_report_period="2025-12-31",
        cusip="037833100",
        title_of_class="COM",
        put_call=None,
        rule_id="institutional-delta-reported-shares-increased",
        metric_result_id=_identifier("institutional-suppressed-metric-result"),
        metric_key="cazatiburones.institutional.delta_reported_shares",
        algorithm_version=_METRIC_ALGORITHM,
        unit="shares",
        value=Decimal("2600"),
        available_at=_T0 + timedelta(hours=2),
        input_observation_ids=(
            _identifier("institutional-suppressed-observation-1"),
            _identifier("institutional-suppressed-observation-2"),
        ),
        parameters={
            "manager_cik": _MANAGER_CIK,
            "report_period": "2026-03-31",
            "prior_report_period": "2025-12-31",
            "cusip": "037833100",
            "title_of_class": "COM",
            "put_call": None,
        },
    )
    eligible_candidate = InstitutionalCandidate(
        candidate_id=_identifier("institutional-candidate"),
        event_id=eligible_event.event_id,
        status="eligible",
    )
    suppressed_candidate = InstitutionalCandidate(
        candidate_id=_identifier("institutional-suppressed-candidate"),
        event_id=suppressed_event.event_id,
        status="suppressed",
        cooldown_until=eligible_event.available_at + timedelta(days=1),
        suppressed_by_event_id=eligible_event.event_id,
    )
    return InstitutionalEventSnapshot(
        snapshot_id=_identifier("institutional-snapshot"),
        asset_id=_ASSET_ID,
        manager_cik=_MANAGER_CIK,
        known_at=_T0 + timedelta(days=2),
        recorded_at=recorded_at,
        policy_version=_INSTITUTIONAL_POLICY,
        evaluations=(),
        events=(eligible_event, suppressed_event),
        candidates=(eligible_candidate, suppressed_candidate),
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cazatiburones-notifications-smoke-") as temporary:
        root = Path(temporary)
        workspace = WorkspaceService(environ={}, home=root / "home").initialize(root / "workspace")
        processed_dir = StoragePaths.from_root(workspace.paths.storage_root).processed_dir
        ActivityEventRepository(processed_dir, read_only=False).save(
            _activity_snapshot("activity-snapshot-1", _T0 + timedelta(minutes=1))
        )
        ActivityEventRepository(processed_dir, read_only=False).save(
            _activity_snapshot("activity-snapshot-2", _T0 + timedelta(minutes=2))
        )
        InstitutionalEventRepository(processed_dir, read_only=False).save(
            _institutional_snapshot(_T0 + timedelta(minutes=3))
        )

        outbox_state = root / "outbox" / "cazatiburones-notification-state.json"
        analytical_outbox = root / "outbox" / "candidate-notification-state.json"
        application = CazatiburonesNotificationsApplication(ApplicationRuntime.create_default())
        location = StorageLocationRequest(workspace=workspace.paths.root)

        first = application.reconcile(location=location, outbox_state=outbox_state)
        second = application.reconcile(location=location, outbox_state=outbox_state)
        state = application.query(location=location, outbox_state=outbox_state)

        assert first.projected_items == 2
        assert first.created_items == 2
        assert first.reused_items == 0
        assert second.projected_items == 2
        assert second.created_items == 0
        assert second.reused_items == 2
        assert len(state.items) == 2
        assert {item.family for item in state.items} == {"activity", "institutional"}
        assert all(isinstance(item.value, Decimal) for item in state.items)
        assert all(item.created_at == item.available_at for item in state.items)
        assert not analytical_outbox.exists()

        activity_item = next(item for item in state.items if item.family == "activity")
        first_acknowledgement = application.acknowledge(
            location=location,
            outbox_state=outbox_state,
            notification_id=activity_item.notification_id,
            recorded_at=_T0 + timedelta(days=3),
        )
        second_acknowledgement = application.acknowledge(
            location=location,
            outbox_state=outbox_state,
            notification_id=activity_item.notification_id,
            recorded_at=_T0 + timedelta(days=4),
        )
        final_state = application.query(location=location, outbox_state=outbox_state)
        assert first_acknowledgement.created is True
        assert second_acknowledgement.created is False
        assert (
            first_acknowledgement.acknowledgement.acknowledgement_id
            == second_acknowledgement.acknowledgement.acknowledgement_id
        )
        assert len(final_state.items) == 2
        assert len(final_state.acknowledgements) == 1
        assert (
            first_acknowledgement.acknowledgement.notification_id == activity_item.notification_id
        )

        print(
            json.dumps(
                {
                    "status": "PASS",
                    "schema_version": "cazatiburones-notification-smoke-v1",
                    "workspace_temporary": True,
                    "outbox_state_explicit": True,
                    "first_reconcile": first.model_dump(mode="json"),
                    "second_reconcile": second.model_dump(mode="json"),
                    "items": len(final_state.items),
                    "acknowledgements": len(final_state.acknowledgements),
                    "traceability_verified": True,
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
