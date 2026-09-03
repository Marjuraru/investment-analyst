"""Pure projection and append-only storage for Cazatiburones notifications."""

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import UUID, uuid4

from investment_analyst.alerts.cazatiburones_notification_models import (
    CazatiburonesNotification,
    CazatiburonesNotificationAcknowledgement,
    CazatiburonesNotificationState,
    NotificationFamily,
    acknowledgement_id,
    notification_id,
)
from investment_analyst.analytics.cazatiburones.activity_event_models import (
    ActivityEvent,
    ActivityEventSnapshot,
)
from investment_analyst.analytics.cazatiburones.institutional_event_models import (
    InstitutionalEvent,
    InstitutionalEventSnapshot,
)


class CazatiburonesNotificationError(RuntimeError):
    """Base error for the isolated Cazatiburones notification boundary."""


class CazatiburonesNotificationStateError(CazatiburonesNotificationError):
    """Raised when outbox state is malformed or semantically conflicting."""


class CazatiburonesNotificationReconciliationError(CazatiburonesNotificationError):
    """Raised when persisted event evidence cannot be projected safely."""


class CazatiburonesNotificationStore:
    """Persist local notification items and acknowledgements atomically."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve(strict=False)
        self._lock = RLock()

    def load(self) -> CazatiburonesNotificationState:
        """Load and validate state without repairing malformed files."""
        with self._lock:
            if not self._path.exists():
                return CazatiburonesNotificationState()
            try:
                return CazatiburonesNotificationState.model_validate_json(
                    self._path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, ValueError) as error:
                raise CazatiburonesNotificationStateError(
                    "cazatiburones notification outbox state is malformed"
                ) from error

    def reconciliation(self) -> "CazatiburonesNotificationReconciliation":
        """Load one validated state for a deterministic enqueue pass."""
        return CazatiburonesNotificationReconciliation(self, self.load())

    def enqueue(self, item: CazatiburonesNotification) -> tuple[CazatiburonesNotification, bool]:
        """Append an item, or reuse an equivalent existing identity."""
        with self._lock:
            result, _, _ = self._enqueue(self.load(), item)
            return result

    def _enqueue(
        self,
        state: CazatiburonesNotificationState,
        item: CazatiburonesNotification,
    ) -> tuple[
        tuple[CazatiburonesNotification, bool],
        CazatiburonesNotificationState,
        bool,
    ]:
        existing = next(
            (value for value in state.items if value.notification_id == item.notification_id),
            None,
        )
        if existing is not None:
            if not _same_notification_semantics(existing, item):
                raise CazatiburonesNotificationStateError("notification identity changed semantics")
            return (existing, False), state, False

        snapshot = CazatiburonesNotificationState(
            items=tuple(
                sorted(
                    (*state.items, item),
                    key=lambda value: (value.created_at, str(value.notification_id)),
                )
            ),
            acknowledgements=state.acknowledgements,
        )
        self._write(snapshot)
        return (item, True), snapshot, True

    def acknowledge(
        self,
        identifier: UUID,
        *,
        recorded_at: datetime,
    ) -> tuple[CazatiburonesNotification, bool]:
        """Append one acknowledgement; repeat calls are idempotent."""
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        recorded_at_utc = recorded_at.astimezone(UTC)
        with self._lock:
            state = self.load()
            item = next(
                (value for value in state.items if value.notification_id == identifier),
                None,
            )
            if item is None:
                raise CazatiburonesNotificationStateError("notification does not exist")
            if any(value.notification_id == identifier for value in state.acknowledgements):
                return item, False

            acknowledgement = CazatiburonesNotificationAcknowledgement(
                acknowledgement_id=acknowledgement_id(identifier, recorded_at_utc),
                notification_id=identifier,
                recorded_at=recorded_at_utc,
            )
            snapshot = CazatiburonesNotificationState(
                items=state.items,
                acknowledgements=tuple(
                    sorted(
                        (*state.acknowledgements, acknowledgement),
                        key=lambda value: (
                            value.recorded_at,
                            str(value.acknowledgement_id),
                        ),
                    )
                ),
            )
            self._write(snapshot)
            return item, True

    def _write(self, state: CazatiburonesNotificationState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    json.dumps(
                        state.model_dump(mode="json"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)


class CazatiburonesNotificationReconciliation:
    """In-memory identity index for one append-only reconciliation pass."""

    def __init__(
        self,
        store: CazatiburonesNotificationStore,
        state: CazatiburonesNotificationState,
    ) -> None:
        self._store = store
        self._state = state
        self._notification_ids = {item.notification_id for item in state.items}

    def contains(self, identifier: UUID) -> bool:
        """Return whether an item identity is already present in this pass."""
        return identifier in self._notification_ids

    def enqueue(self, item: CazatiburonesNotification) -> tuple[CazatiburonesNotification, bool]:
        """Append an item while retaining the loaded identity index."""
        with self._store._lock:
            if type(self._store).enqueue is not CazatiburonesNotificationStore.enqueue:
                result, created = self._store.enqueue(item)
                if created:
                    self._state = CazatiburonesNotificationState(
                        items=tuple(
                            sorted(
                                (*self._state.items, result),
                                key=lambda value: (
                                    value.created_at,
                                    str(value.notification_id),
                                ),
                            )
                        ),
                        acknowledgements=self._state.acknowledgements,
                    )
                    self._notification_ids.add(result.notification_id)
                return result, created

            result, self._state, created = self._store._enqueue(self._state, item)
            if created:
                self._notification_ids.add(result[0].notification_id)
            return result


def project_cazatiburones_notifications(
    activity_snapshots: Sequence[ActivityEventSnapshot],
    institutional_snapshots: Sequence[InstitutionalEventSnapshot],
) -> tuple[CazatiburonesNotification, ...]:
    """Project only eligible persisted candidates into deterministic local items."""
    projected: dict[tuple[NotificationFamily, UUID], CazatiburonesNotification] = {}
    for snapshot in sorted(
        activity_snapshots,
        key=lambda item: (item.known_at, str(item.snapshot_id)),
    ):
        events = {item.event_id: item for item in snapshot.events}
        for candidate in snapshot.candidates:
            if candidate.status != "eligible":
                continue
            event = events.get(candidate.event_id)
            if event is None:
                raise CazatiburonesNotificationReconciliationError(
                    "activity candidate references a missing event"
                )
            _validate_activity_evidence(snapshot, event)
            item = _activity_notification(snapshot, event, candidate.candidate_id)
            _register(projected, item)

    for snapshot in sorted(
        institutional_snapshots,
        key=lambda item: (item.known_at, str(item.snapshot_id)),
    ):
        events = {item.event_id: item for item in snapshot.events}
        for candidate in snapshot.candidates:
            if candidate.status != "eligible":
                continue
            event = events.get(candidate.event_id)
            if event is None:
                raise CazatiburonesNotificationReconciliationError(
                    "institutional candidate references a missing event"
                )
            _validate_institutional_evidence(snapshot, event)
            item = _institutional_notification(snapshot, event, candidate.candidate_id)
            _register(projected, item)

    return tuple(
        sorted(
            projected.values(),
            key=lambda item: (item.created_at, str(item.notification_id)),
        )
    )


def project_notifications(
    activity_snapshots: Sequence[ActivityEventSnapshot],
    institutional_snapshots: Sequence[InstitutionalEventSnapshot],
) -> tuple[CazatiburonesNotification, ...]:
    """Short alias for the pure Cazatiburones notification projection."""
    return project_cazatiburones_notifications(activity_snapshots, institutional_snapshots)


class CazatiburonesNotificationReconciler:
    """Stateless facade for the pure notification projection."""

    @staticmethod
    def project(
        activity_snapshots: Sequence[ActivityEventSnapshot],
        institutional_snapshots: Sequence[InstitutionalEventSnapshot],
    ) -> tuple[CazatiburonesNotification, ...]:
        return project_cazatiburones_notifications(activity_snapshots, institutional_snapshots)


def _activity_notification(
    snapshot: ActivityEventSnapshot,
    event: ActivityEvent,
    candidate_id: UUID,
) -> CazatiburonesNotification:
    algorithm_version = snapshot.policy_version
    parameter_version = event.parameters.get("algorithm_version")
    if isinstance(parameter_version, str) and parameter_version.strip():
        algorithm_version = parameter_version
    return CazatiburonesNotification(
        family="activity",
        notification_id=notification_id("activity", candidate_id),
        candidate_id=candidate_id,
        event_id=event.event_id,
        metric_result_id=event.metric_result_id,
        snapshot_id=snapshot.snapshot_id,
        asset_id=event.asset_id,
        rule_id=event.rule_id,
        metric_key=event.metric_key,
        algorithm_version=algorithm_version,
        unit=event.unit,
        value=event.value,
        available_at=event.available_at,
        created_at=event.available_at,
        input_observation_ids=event.input_observation_ids,
    )


def _institutional_notification(
    snapshot: InstitutionalEventSnapshot,
    event: InstitutionalEvent,
    candidate_id: UUID,
) -> CazatiburonesNotification:
    return CazatiburonesNotification(
        family="institutional",
        notification_id=notification_id("institutional", candidate_id),
        candidate_id=candidate_id,
        event_id=event.event_id,
        metric_result_id=event.metric_result_id,
        snapshot_id=snapshot.snapshot_id,
        asset_id=event.asset_id,
        rule_id=event.rule_id,
        metric_key=event.metric_key,
        algorithm_version=event.algorithm_version,
        unit=event.unit,
        value=event.value,
        available_at=event.available_at,
        created_at=event.available_at,
        input_observation_ids=event.input_observation_ids,
        manager_cik=event.manager_cik,
        report_period=event.report_period,
        prior_report_period=event.prior_report_period,
        cusip=event.cusip,
        title_of_class=event.title_of_class,
        put_call=event.put_call,
    )


def _register(
    projected: dict[tuple[NotificationFamily, UUID], CazatiburonesNotification],
    item: CazatiburonesNotification,
) -> None:
    key = (item.family, item.candidate_id)
    existing = projected.get(key)
    if existing is not None and not _same_notification_semantics(existing, item):
        raise CazatiburonesNotificationReconciliationError(
            "candidate identity changed semantics across snapshots"
        )
    if existing is None:
        projected[key] = item


def _same_notification_semantics(
    left: CazatiburonesNotification,
    right: CazatiburonesNotification,
) -> bool:
    """Compare a candidate's delivery meaning while allowing snapshot provenance to advance."""
    return left.model_dump(exclude={"snapshot_id"}) == right.model_dump(exclude={"snapshot_id"})


def _validate_activity_evidence(
    snapshot: ActivityEventSnapshot,
    event: ActivityEvent,
) -> None:
    if event.asset_id != snapshot.asset_id:
        raise CazatiburonesNotificationReconciliationError(
            "activity event asset differs from its snapshot"
        )
    if event.available_at > snapshot.known_at:
        raise CazatiburonesNotificationReconciliationError(
            "activity event is not available at its snapshot cut"
        )


def _validate_institutional_evidence(
    snapshot: InstitutionalEventSnapshot,
    event: InstitutionalEvent,
) -> None:
    if event.asset_id != snapshot.asset_id or event.manager_cik != snapshot.manager_cik:
        raise CazatiburonesNotificationReconciliationError(
            "institutional event provenance differs from its snapshot"
        )
    if event.available_at > snapshot.known_at:
        raise CazatiburonesNotificationReconciliationError(
            "institutional event is not available at its snapshot cut"
        )
