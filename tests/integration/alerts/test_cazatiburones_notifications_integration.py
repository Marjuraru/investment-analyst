"""Integration coverage for the isolated Cazatiburones notification boundary."""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

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
from investment_analyst.storage import StoragePaths
from investment_analyst.workspace.service import WorkspaceService

_ROOT = Path(__file__).parents[3]
_T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)
_ASSET_ID = "equity:us:aapl"


def _seed_workspace(tmp_path: Path) -> tuple[Path, object]:
    workspace = (
        WorkspaceService(environ={}, home=tmp_path / "home")
        .initialize(tmp_path / "workspace")
        .paths
    )
    processed_dir = StoragePaths.from_root(workspace.storage_root).processed_dir

    activity_event = ActivityEvent(
        event_id=uuid4(),
        asset_id=_ASSET_ID,
        rule_id="insider-holding-increased",
        metric_result_id=uuid4(),
        metric_key="cazatiburones.insider.holding_delta_ratio",
        unit="ratio",
        value=Decimal("1.250000000000000001"),
        available_at=_T0,
        input_observation_ids=(uuid4(), uuid4()),
        parameters={
            "algorithm_version": "cazatiburones-activity-metrics-v1",
            "participant_cik": "0000000001",
        },
    )
    activity_candidate = ActivityCandidate(
        candidate_id=uuid4(),
        event_id=activity_event.event_id,
        status="eligible",
    )
    ActivityEventRepository(processed_dir, read_only=False).save(
        ActivityEventSnapshot(
            snapshot_id=uuid4(),
            asset_id=_ASSET_ID,
            known_at=_T0 + timedelta(days=1),
            recorded_at=_T0 + timedelta(minutes=1),
            policy_version="cazatiburones-persisted-activity-events-v1",
            evaluations=(),
            events=(activity_event,),
            candidates=(activity_candidate,),
        )
    )

    institutional_event = InstitutionalEvent(
        event_id=uuid4(),
        asset_id=_ASSET_ID,
        manager_cik="0001350694",
        report_period="2025-12-31",
        prior_report_period="2025-09-30",
        cusip="037833100",
        title_of_class="COM",
        put_call=None,
        rule_id="institutional-delta-reported-shares-increased",
        metric_result_id=uuid4(),
        metric_key="cazatiburones.institutional.delta_reported_shares",
        algorithm_version="cazatiburones-institutional-metrics-v1",
        unit="shares",
        value=Decimal("2500.000000000000000001"),
        available_at=_T0 + timedelta(hours=1),
        input_observation_ids=(uuid4(), uuid4()),
        parameters={
            "manager_cik": "0001350694",
            "report_period": "2025-12-31",
            "prior_report_period": "2025-09-30",
            "cusip": "037833100",
            "title_of_class": "COM",
            "put_call": None,
        },
    )
    InstitutionalEventRepository(processed_dir, read_only=False).save(
        InstitutionalEventSnapshot(
            snapshot_id=uuid4(),
            asset_id=_ASSET_ID,
            manager_cik="0001350694",
            known_at=_T0 + timedelta(days=1),
            recorded_at=_T0 + timedelta(minutes=2),
            policy_version="cazatiburones-persisted-institutional-events-v1",
            evaluations=(),
            events=(institutional_event,),
            candidates=(
                InstitutionalCandidate(
                    candidate_id=uuid4(),
                    event_id=institutional_event.event_id,
                    status="eligible",
                ),
            ),
        )
    )
    return workspace.root, activity_candidate


def _run(script_name: str, arguments: list[str], *, cwd: Path, env: dict[str, str]):
    return subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / script_name), *arguments],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def test_cli_requires_explicit_workspace_and_outbox_state_and_is_cwd_independent(
    tmp_path: Path,
) -> None:
    workspace, _ = _seed_workspace(tmp_path)
    outbox = tmp_path / "outbox" / "cazatiburones.json"
    external_cwd = tmp_path / "external-cwd"
    external_cwd.mkdir()
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": str(_ROOT / "src"),
    }

    first = _run(
        "reconcile_cazatiburones_notifications.py",
        ["--workspace", str(workspace), "--outbox-state", str(outbox)],
        cwd=external_cwd,
        env=environment,
    )
    assert first.returncode == 0, first.stderr
    first_summary = json.loads(first.stdout)
    assert first_summary["projected_items"] == 2
    assert first_summary["created_items"] == 2

    second = _run(
        "reconcile_cazatiburones_notifications.py",
        ["--workspace", str(workspace), "--outbox-state", str(outbox)],
        cwd=external_cwd,
        env=environment,
    )
    assert second.returncode == 0, second.stderr
    second_summary = json.loads(second.stdout)
    assert second_summary["created_items"] == 0
    assert second_summary["reused_items"] == 2

    query = _run(
        "query_cazatiburones_notifications.py",
        ["--workspace", str(workspace), "--outbox-state", str(outbox)],
        cwd=external_cwd,
        env=environment,
    )
    assert query.returncode == 0, query.stderr
    state = json.loads(query.stdout)
    assert len(state["items"]) == 2
    assert {item["family"] for item in state["items"]} == {"activity", "institutional"}
    assert "recommendation" not in query.stdout.casefold()
    activity_item = next(item for item in state["items"] if item["family"] == "activity")

    acknowledgement = _run(
        "acknowledge_cazatiburones_notification.py",
        [
            "--workspace",
            str(workspace),
            "--outbox-state",
            str(outbox),
            "--notification-id",
            activity_item["notification_id"],
            "--recorded-at",
            "2026-01-03T12:00:00Z",
        ],
        cwd=external_cwd,
        env=environment,
    )
    assert acknowledgement.returncode == 0, acknowledgement.stderr
    assert json.loads(acknowledgement.stdout)["created"] is True

    repeated = _run(
        "acknowledge_cazatiburones_notification.py",
        [
            "--workspace",
            str(workspace),
            "--outbox-state",
            str(outbox),
            "--notification-id",
            activity_item["notification_id"],
            "--recorded-at",
            "2026-01-04T12:00:00Z",
        ],
        cwd=external_cwd,
        env=environment,
    )
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["created"] is False

    filtered = _run(
        "query_cazatiburones_notifications.py",
        [
            "--workspace",
            str(workspace),
            "--outbox-state",
            str(outbox),
            "--notification-id",
            activity_item["notification_id"],
        ],
        cwd=external_cwd,
        env=environment,
    )
    assert filtered.returncode == 0, filtered.stderr
    filtered_state = json.loads(filtered.stdout)
    assert len(filtered_state["items"]) == 1
    assert len(filtered_state["acknowledgements"]) == 1

    missing_workspace = _run(
        "query_cazatiburones_notifications.py",
        ["--outbox-state", str(outbox)],
        cwd=external_cwd,
        env=environment,
    )
    assert missing_workspace.returncode == 2
    missing_outbox = _run(
        "query_cazatiburones_notifications.py",
        ["--workspace", str(workspace)],
        cwd=external_cwd,
        env=environment,
    )
    assert missing_outbox.returncode == 2
