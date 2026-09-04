"""Application boundary for the isolated Cazatiburones notification outbox."""

from datetime import datetime
from pathlib import Path
from uuid import UUID

from investment_analyst.alerts.cazatiburones_notification_models import (
    CazatiburonesNotificationAcknowledgementResult,
    CazatiburonesNotificationReconciliationSummary,
    CazatiburonesNotificationState,
)
from investment_analyst.alerts.cazatiburones_notifications import (
    CazatiburonesNotificationStateError,
    CazatiburonesNotificationStore,
    project_cazatiburones_notifications,
)
from investment_analyst.analytics.cazatiburones.activity_event_repository import (
    ActivityEventRepository,
)
from investment_analyst.analytics.cazatiburones.institutional_event_repository import (
    InstitutionalEventRepository,
)
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.workspace.models import WorkspaceAccessMode


class CazatiburonesNotificationsApplication:
    """Compose read-only event evidence with one explicitly writable outbox path."""

    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def create_default(cls) -> "CazatiburonesNotificationsApplication":
        """Build the application boundary with the central runtime composition."""
        return cls(ApplicationRuntime.create_default())

    def reconcile(
        self,
        *,
        location: StorageLocationRequest,
        outbox_state: Path,
    ) -> CazatiburonesNotificationReconciliationSummary:
        """Project eligible persisted events and append only to the explicit outbox."""
        with self._runtime.open_storage(
            location, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            processed_dir = storage.paths.processed_dir
            activity_snapshots = ActivityEventRepository(
                processed_dir, read_only=True
            ).list_snapshots()
            institutional_snapshots = InstitutionalEventRepository(
                processed_dir, read_only=True
            ).list_snapshots()
            items = project_cazatiburones_notifications(
                activity_snapshots,
                institutional_snapshots,
            )

            reconciliation = CazatiburonesNotificationStore(outbox_state).reconciliation()
            created = 0
            reused = 0
            for item in items:
                _, was_created = reconciliation.enqueue(item)
                if was_created:
                    created += 1
                else:
                    reused += 1

            return CazatiburonesNotificationReconciliationSummary(
                activity_snapshots=len(activity_snapshots),
                institutional_snapshots=len(institutional_snapshots),
                projected_items=len(items),
                created_items=created,
                reused_items=reused,
            )

    def query(
        self,
        *,
        location: StorageLocationRequest,
        outbox_state: Path,
        notification_id: UUID | None = None,
    ) -> CazatiburonesNotificationState:
        """Read outbox state while opening evidence storage in read-only mode."""
        with self._runtime.open_storage(location, access_mode=WorkspaceAccessMode.READ_ONLY):
            state = CazatiburonesNotificationStore(outbox_state).load()
        if notification_id is None:
            return state
        item = next(
            (value for value in state.items if value.notification_id == notification_id),
            None,
        )
        if item is None:
            return CazatiburonesNotificationState()
        acknowledgements = tuple(
            value for value in state.acknowledgements if value.notification_id == notification_id
        )
        return CazatiburonesNotificationState(
            items=(item,),
            acknowledgements=acknowledgements,
        )

    def acknowledge(
        self,
        *,
        location: StorageLocationRequest,
        outbox_state: Path,
        notification_id: UUID,
        recorded_at: datetime,
    ) -> CazatiburonesNotificationAcknowledgementResult:
        """Append an explicit local acknowledgement without writing event evidence."""
        with self._runtime.open_storage(location, access_mode=WorkspaceAccessMode.READ_ONLY):
            store = CazatiburonesNotificationStore(outbox_state)
            item, created = store.acknowledge(notification_id, recorded_at=recorded_at)
            acknowledgement = next(
                (
                    value
                    for value in store.load().acknowledgements
                    if value.notification_id == notification_id
                ),
                None,
            )
            if acknowledgement is None:
                raise CazatiburonesNotificationStateError("acknowledgement was not persisted")
            return CazatiburonesNotificationAcknowledgementResult(
                item=item,
                acknowledgement=acknowledgement,
                created=created,
            )
