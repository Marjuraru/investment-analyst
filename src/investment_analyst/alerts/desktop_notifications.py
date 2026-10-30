"""Bounded local Windows toast delivery with explicit transport evidence."""

import hashlib
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from investment_analyst.alerts.analytical_models import (
    AnalyticalRuleState,
    AnalyticalScreeningResult,
)
from investment_analyst.alerts.analytical_rule_registry import (
    AnalyticalRuleConfiguration,
    AnalyticalRuleRegistrySnapshot,
)
from investment_analyst.alerts.analytical_state import (
    AnalyticalCandidateEvent,
    AnalyticalCandidateStatus,
    AnalyticalScreeningState,
)
from investment_analyst.alerts.candidate_notifications import (
    CandidateNotification,
    CandidateNotificationState,
)
from investment_analyst.alerts.desktop_notification_models import (
    MAX_BATCH_DELIVERIES,
    MAX_RETAINED_DELIVERIES,
    MAX_SEND_ATTEMPTS,
    MAX_STATE_BYTES,
    MIN_BATCH_INTERVAL_SECONDS,
    RETRY_DELAYS_SECONDS,
    DesktopNotificationCapability,
    DesktopNotificationChannelConfig,
    DesktopNotificationDelivery,
    DesktopNotificationDeliveryStatus,
    DesktopNotificationReason,
    DesktopNotificationStatus,
    DesktopNotificationTestResponse,
    desktop_notification_delivery_id,
)
from investment_analyst.alerts.windows_notifications import (
    ToastMessage,
    ToastSendStatus,
    WindowsCapability,
    WindowsNotificationError,
    WindowsToastAdapter,
)
from investment_analyst.application.bounded_journal import (
    BoundedOperationalJournal,
    JournalManifest,
    atomic_write,
)
from investment_analyst.application.operational_state import AaplOperationalStateError

_ALLOWED_TRANSITIONS: dict[
    DesktopNotificationDeliveryStatus, frozenset[DesktopNotificationDeliveryStatus]
] = {
    DesktopNotificationDeliveryStatus.PENDING: frozenset(
        {
            DesktopNotificationDeliveryStatus.ATTEMPTING,
            DesktopNotificationDeliveryStatus.CANCELLED,
        }
    ),
    DesktopNotificationDeliveryStatus.ATTEMPTING: frozenset(
        {
            DesktopNotificationDeliveryStatus.ACCEPTED,
            DesktopNotificationDeliveryStatus.RETRY_WAIT,
            DesktopNotificationDeliveryStatus.UNKNOWN,
            DesktopNotificationDeliveryStatus.FAILED,
        }
    ),
    DesktopNotificationDeliveryStatus.RETRY_WAIT: frozenset(
        {
            DesktopNotificationDeliveryStatus.ATTEMPTING,
            DesktopNotificationDeliveryStatus.CANCELLED,
        }
    ),
    DesktopNotificationDeliveryStatus.ACCEPTED: frozenset(),
    DesktopNotificationDeliveryStatus.UNKNOWN: frozenset(),
    DesktopNotificationDeliveryStatus.CANCELLED: frozenset(),
    DesktopNotificationDeliveryStatus.FAILED: frozenset(),
}


class DesktopNotificationError(AaplOperationalStateError):
    """Raised when persisted delivery evidence is malformed or inconsistent."""


class DesktopNotificationBudgetError(DesktopNotificationError):
    """Raised when admission would exceed the declared physical budget."""


class DesktopNotificationChannelError(AaplOperationalStateError):
    """Raised when the fixed Windows bridge is unavailable for an explicit action."""

    def __init__(self, reason: DesktopNotificationReason) -> None:
        self.reason = reason
        super().__init__(f"desktop notification channel unavailable: {reason.value}")


class DesktopNotificationRateLimitError(AaplOperationalStateError):
    """Raised when an explicit transport test is requested too soon."""


def candidate_notification_fingerprint(notification: CandidateNotification) -> str:
    """Hash the immutable outbox payload that a delivery must reproduce."""
    payload = notification.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _reduce_delivery_records(
    records: Sequence[dict[str, object]],
) -> Sequence[dict[str, object]]:
    by_id: dict[str, dict[str, object]] = {}
    for record in records:
        item = record.get("item")
        if not isinstance(item, dict):
            continue
        identifier = item.get("delivery_id")
        if isinstance(identifier, str):
            by_id[identifier] = record

    def _sort_key(record: dict[str, object]) -> tuple[str, str]:
        item = record.get("item")
        if not isinstance(item, dict):
            return ("", "")
        return (str(item.get("created_at", "")), str(item.get("delivery_id", "")))

    return tuple(sorted(by_id.values(), key=_sort_key))


class DesktopNotificationStore:
    """Persist channel configuration and compact append-only delivery evidence."""

    def __init__(
        self,
        state_dir: Path,
        *,
        workspace_uuid: UUID,
        max_state_bytes: int = MAX_STATE_BYTES,
        max_retained: int = MAX_RETAINED_DELIVERIES,
        journal_max_segment_bytes: int = 512 * 1024,
        journal_max_segment_records: int = 5_000,
    ) -> None:
        if max_state_bytes <= 0 or max_retained <= 0:
            raise ValueError("desktop notification budgets must be positive")
        self._state_dir = state_dir.expanduser().resolve(strict=False)
        self._workspace_uuid = workspace_uuid
        self._max_state_bytes = max_state_bytes
        self._max_retained = max_retained
        self._config_path = self._state_dir / "config.json"
        self._journal_dir = self._state_dir / "journal"
        self._journal = BoundedOperationalJournal(
            directory=self._journal_dir,
            state_root=self._state_dir,
            max_segment_bytes=journal_max_segment_bytes,
            max_segment_records=journal_max_segment_records,
            journal_id="desktop-notifications",
            reducer=_reduce_delivery_records,
        )
        self._lock = threading.RLock()
        self._deliveries: dict[UUID, DesktopNotificationDelivery] | None = None
        self._ordered: tuple[DesktopNotificationDelivery, ...] | None = None

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    @property
    def max_state_bytes(self) -> int:
        return self._max_state_bytes

    @property
    def max_retained(self) -> int:
        return self._max_retained

    def load_config(self) -> DesktopNotificationChannelConfig:
        """Load configuration without creating a missing file."""
        with self._lock:
            if not self._config_path.exists():
                return DesktopNotificationChannelConfig()
            try:
                return DesktopNotificationChannelConfig.model_validate_json(
                    self._config_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, ValueError) as error:
                raise DesktopNotificationError(
                    "desktop notification configuration is malformed"
                ) from error

    def _write_config(self, config: DesktopNotificationChannelConfig) -> None:
        payload = (
            json.dumps(
                config.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        try:
            atomic_write(self._config_path, payload)
        except OSError as error:
            raise DesktopNotificationError(
                "desktop notification configuration could not be written"
            ) from error

    def configure(
        self,
        *,
        enabled: bool,
        at: datetime,
        capability: WindowsCapability | None = None,
    ) -> DesktopNotificationChannelConfig:
        """Persist one explicit activation or deactivation."""
        if enabled and capability is not None and not capability.aumid_registered:
            raise DesktopNotificationError("activation requires a registered identity")
        with self._lock:
            current = self.load_config()
            updated = current.model_copy(
                update={
                    "enabled": enabled,
                    "enabled_at": at.astimezone(UTC) if enabled else None,
                    "updated_at": at.astimezone(UTC),
                    "last_probe_at": (
                        at.astimezone(UTC) if capability is not None else current.last_probe_at
                    ),
                    "last_probe_available": (
                        capability.available
                        if capability is not None
                        else current.last_probe_available
                    ),
                    "last_probe_reason": (
                        capability.reason if capability is not None else current.last_probe_reason
                    ),
                }
            )
            self._write_config(updated)
            if not enabled:
                self._cancel_pending(DesktopNotificationReason.CHANNEL_DISABLED, at)
            return updated

    def record_probe(self, capability: WindowsCapability, at: datetime) -> None:
        """Persist the last explicit capability observation."""
        with self._lock:
            current = self.load_config()
            self._write_config(
                current.model_copy(
                    update={
                        "updated_at": at.astimezone(UTC),
                        "last_probe_at": at.astimezone(UTC),
                        "last_probe_available": capability.available,
                        "last_probe_reason": capability.reason,
                    }
                )
            )

    def record_test(
        self,
        *,
        accepted: bool,
        reason: DesktopNotificationReason | None,
        at: datetime,
    ) -> None:
        """Persist one explicit transport test result."""
        with self._lock:
            current = self.load_config()
            self._write_config(
                current.model_copy(
                    update={
                        "updated_at": at.astimezone(UTC),
                        "last_test_at": at.astimezone(UTC),
                        "last_test_accepted": accepted,
                        "last_test_reason": reason,
                    }
                )
            )

    def mark_batch(self, at: datetime) -> None:
        """Persist the last bounded delivery batch attempt time."""
        with self._lock:
            current = self.load_config()
            self._write_config(
                current.model_copy(
                    update={
                        "updated_at": at.astimezone(UTC),
                        "last_batch_at": at.astimezone(UTC),
                    }
                )
            )

    def load_deliveries(self) -> tuple[DesktopNotificationDelivery, ...]:
        """Load, validate, and project every persisted delivery transition once."""
        with self._lock:
            if self._ordered is not None:
                return self._ordered
            try:
                records = self._journal.read_entries()
            except AaplOperationalStateError as error:
                raise DesktopNotificationError(
                    "desktop notification journal is malformed"
                ) from error
            latest: dict[UUID, DesktopNotificationDelivery] = {}
            for record in records:
                item = record.get("item")
                if not isinstance(item, dict):
                    raise DesktopNotificationError(
                        "desktop notification journal contains an invalid record"
                    )
                try:
                    delivery = DesktopNotificationDelivery.model_validate(item)
                except (TypeError, ValueError) as error:
                    raise DesktopNotificationError(
                        "desktop notification journal contains invalid evidence"
                    ) from error
                previous = latest.get(delivery.delivery_id)
                if previous is not None:
                    self._validate_transition(previous, delivery)
                latest[delivery.delivery_id] = delivery
            self._deliveries = latest
            self._ordered = tuple(
                sorted(
                    latest.values(),
                    key=lambda item: (item.created_at, str(item.delivery_id)),
                )
            )
            return self._ordered

    @staticmethod
    def _validate_transition(
        previous: DesktopNotificationDelivery,
        current: DesktopNotificationDelivery,
    ) -> None:
        if previous == current:
            return
        if current.updated_at < previous.updated_at:
            raise DesktopNotificationError("desktop notification history is out of order")
        if current.status not in _ALLOWED_TRANSITIONS[previous.status]:
            raise DesktopNotificationError(
                "desktop notification lifecycle transition is not allowed"
            )
        if (
            current.notification_id != previous.notification_id
            or current.fingerprint != previous.fingerprint
            or current.windows_tag != previous.windows_tag
            or current.windows_group != previous.windows_group
        ):
            raise DesktopNotificationError(
                "desktop notification identity changed across its lifecycle"
            )

    def _append(self, delivery: DesktopNotificationDelivery) -> None:
        try:
            self._journal.append({"item": delivery.model_dump(mode="json")})
        except AaplOperationalStateError as error:
            raise DesktopNotificationError(
                "desktop notification evidence could not be appended"
            ) from error
        if self._deliveries is not None:
            self._deliveries[delivery.delivery_id] = delivery
            self._ordered = None

    def state_bytes(self) -> int:
        """Return the persisted channel footprint from manifest counters and stats."""
        with self._lock:
            total = 0
            if self._config_path.exists():
                try:
                    total += self._config_path.stat().st_size
                except OSError as error:
                    raise DesktopNotificationError(
                        "desktop notification configuration could not be measured"
                    ) from error
            manifest_path = self._journal_dir / "manifest.json"
            if not manifest_path.exists():
                return total
            try:
                total += manifest_path.stat().st_size
                manifest = JournalManifest.model_validate_json(
                    manifest_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, ValueError) as error:
                raise DesktopNotificationError(
                    "desktop notification journal manifest is malformed"
                ) from error
            total += sum(segment.byte_count for segment in manifest.closed_segments)
            if manifest.snapshot_filename is not None:
                try:
                    total += (self._journal_dir / manifest.snapshot_filename).stat().st_size
                except OSError as error:
                    raise DesktopNotificationError(
                        "desktop notification snapshot could not be measured"
                    ) from error
            if manifest.open_segment_name is not None:
                open_path = self._journal_dir / manifest.open_segment_name
                if open_path.exists():
                    try:
                        total += open_path.stat().st_size
                    except OSError as error:
                        raise DesktopNotificationError(
                            "desktop notification journal could not be measured"
                        ) from error
            return total

    def delivery_count(self) -> int:
        self.load_deliveries()
        return len(self._deliveries or {})

    def budget_exceeded(self) -> bool:
        return (
            self.delivery_count() >= self._max_retained
            or self.state_bytes() >= self._max_state_bytes
        )

    def admit(
        self,
        notification: CandidateNotification,
        *,
        title: str,
        body: str,
        at: datetime,
    ) -> tuple[DesktopNotificationDelivery, bool]:
        """Persist one pending delivery only when the declared budget allows it."""
        with self._lock:
            delivery_id = desktop_notification_delivery_id(
                self._workspace_uuid,
                notification.notification_id,
            )
            self.load_deliveries()
            existing = (self._deliveries or {}).get(delivery_id)
            fingerprint = candidate_notification_fingerprint(notification)
            if existing is not None:
                if (
                    existing.notification_id != notification.notification_id
                    or existing.candidate_id != notification.candidate_id
                    or existing.activation_result_id != notification.activation_result_id
                    or existing.fingerprint != fingerprint
                ):
                    raise DesktopNotificationError(
                        "desktop notification delivery identity changed semantics"
                    )
                return existing, False
            if self.budget_exceeded():
                raise DesktopNotificationBudgetError("desktop notification budget is exhausted")
            delivery = DesktopNotificationDelivery(
                delivery_id=delivery_id,
                workspace_uuid=self._workspace_uuid,
                notification_id=notification.notification_id,
                candidate_id=notification.candidate_id,
                activation_result_id=notification.activation_result_id,
                rule_id=notification.rule_id,
                asset_id=notification.asset_id,
                title_es=title,
                body_es=body,
                notification_created_at=notification.created_at,
                fingerprint=fingerprint,
                status=DesktopNotificationDeliveryStatus.PENDING,
                attempt_count=0,
                created_at=at.astimezone(UTC),
                updated_at=at.astimezone(UTC),
                windows_tag=delivery_id.hex[:16],
                windows_group=delivery_id.hex[16:32],
            )
            self._append(delivery)
            return delivery, True

    def begin_attempt(self, delivery_id: UUID, at: datetime) -> DesktopNotificationDelivery:
        """Persist delivery intent before any subprocess invocation."""
        with self._lock:
            current = self._delivery(delivery_id)
            if current.status not in {
                DesktopNotificationDeliveryStatus.PENDING,
                DesktopNotificationDeliveryStatus.RETRY_WAIT,
            }:
                raise DesktopNotificationError("delivery is not eligible for an attempt")
            if current.attempt_count >= MAX_SEND_ATTEMPTS:
                raise DesktopNotificationError("delivery exhausted its attempt budget")
            updated = current.model_copy(
                update={
                    "status": DesktopNotificationDeliveryStatus.ATTEMPTING,
                    "attempt_count": current.attempt_count + 1,
                    "updated_at": at.astimezone(UTC),
                    "next_attempt_at": None,
                    "reason": None,
                }
            )
            self._append(updated)
            return updated

    def record_outcome(
        self,
        delivery_id: UUID,
        *,
        status: DesktopNotificationDeliveryStatus,
        at: datetime,
        reason: DesktopNotificationReason | None = None,
    ) -> DesktopNotificationDelivery:
        """Persist one accepted, retryable, unknown, or failed transport outcome."""
        if status not in {
            DesktopNotificationDeliveryStatus.ACCEPTED,
            DesktopNotificationDeliveryStatus.RETRY_WAIT,
            DesktopNotificationDeliveryStatus.UNKNOWN,
            DesktopNotificationDeliveryStatus.FAILED,
        }:
            raise ValueError("unsupported delivery outcome")
        with self._lock:
            current = self._delivery(delivery_id)
            if current.status is not DesktopNotificationDeliveryStatus.ATTEMPTING:
                raise DesktopNotificationError("delivery outcome lacks persisted intent")
            if status is DesktopNotificationDeliveryStatus.ACCEPTED:
                updated = current.model_copy(
                    update={
                        "status": status,
                        "updated_at": at.astimezone(UTC),
                        "next_attempt_at": None,
                        "reason": None,
                    }
                )
            elif status is DesktopNotificationDeliveryStatus.RETRY_WAIT:
                if current.attempt_count >= MAX_SEND_ATTEMPTS or reason is None:
                    raise DesktopNotificationError("retry_wait requires a remaining attempt")
                delay = RETRY_DELAYS_SECONDS[current.attempt_count - 1]
                updated = current.model_copy(
                    update={
                        "status": status,
                        "updated_at": at.astimezone(UTC),
                        "next_attempt_at": at.astimezone(UTC) + timedelta(seconds=delay),
                        "reason": reason,
                    }
                )
            else:
                if reason is None:
                    raise DesktopNotificationError("terminal transport outcome requires a reason")
                updated = current.model_copy(
                    update={
                        "status": status,
                        "updated_at": at.astimezone(UTC),
                        "next_attempt_at": None,
                        "reason": reason,
                    }
                )
            self._append(updated)
            return updated

    def cancel(
        self,
        delivery_id: UUID,
        *,
        reason: DesktopNotificationReason,
        at: datetime,
    ) -> DesktopNotificationDelivery:
        """Cancel a still-pending delivery without deleting its evidence."""
        with self._lock:
            current = self._delivery(delivery_id)
            if current.status in {
                DesktopNotificationDeliveryStatus.ACCEPTED,
                DesktopNotificationDeliveryStatus.UNKNOWN,
                DesktopNotificationDeliveryStatus.CANCELLED,
                DesktopNotificationDeliveryStatus.FAILED,
            }:
                return current
            if current.status is DesktopNotificationDeliveryStatus.ATTEMPTING:
                raise DesktopNotificationError("an in-flight delivery cannot be cancelled")
            updated = current.model_copy(
                update={
                    "status": DesktopNotificationDeliveryStatus.CANCELLED,
                    "updated_at": at.astimezone(UTC),
                    "next_attempt_at": None,
                    "reason": reason,
                }
            )
            self._append(updated)
            return updated

    def _cancel_pending(self, reason: DesktopNotificationReason, at: datetime) -> int:
        cancelled = 0
        for delivery in self.load_deliveries():
            if delivery.status in {
                DesktopNotificationDeliveryStatus.PENDING,
                DesktopNotificationDeliveryStatus.RETRY_WAIT,
            }:
                self.cancel(delivery.delivery_id, reason=reason, at=at)
                cancelled += 1
        return cancelled

    def recover(self, at: datetime) -> int:
        """Resolve interrupted attempts without ever resending them."""
        recovered = 0
        for delivery in self.load_deliveries():
            if delivery.status is DesktopNotificationDeliveryStatus.ATTEMPTING:
                self.record_outcome(
                    delivery.delivery_id,
                    status=DesktopNotificationDeliveryStatus.UNKNOWN,
                    at=at,
                    reason=DesktopNotificationReason.INTERRUPTED_AFTER_INVOCATION,
                )
                recovered += 1
        return recovered

    def _delivery(self, delivery_id: UUID) -> DesktopNotificationDelivery:
        self.load_deliveries()
        delivery = (self._deliveries or {}).get(delivery_id)
        if delivery is None:
            raise DesktopNotificationError("desktop notification delivery does not exist")
        return delivery


class DesktopNotificationService:
    """Admit, validate, and deliver at most one bounded batch per eligible tick."""

    def __init__(
        self,
        store: DesktopNotificationStore,
        adapter: WindowsToastAdapter,
        *,
        rules: Callable[[], AnalyticalRuleRegistrySnapshot],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._rules = rules
        self._clock = clock
        self._lock = threading.RLock()
        self._known_notifications: set[UUID] | None = None

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("desktop notification clock must be timezone-aware")
        return value.astimezone(UTC)

    def recover(self) -> int:
        """Resolve interrupted attempts before the first delivery batch."""
        with self._lock:
            return self._store.recover(self._now())

    def status(self, *, offset: int = 0, limit: int = 50) -> DesktopNotificationStatus:
        """Return a bounded read-only summary without probing or writing."""
        if isinstance(offset, bool) or isinstance(limit, bool):
            raise ValueError("desktop notification pagination must be integers")
        if not isinstance(offset, int) or not isinstance(limit, int):
            raise ValueError("desktop notification pagination must be integers")
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("desktop notification pagination is out of range")
        with self._lock:
            config = self._store.load_config()
            deliveries = self._store.load_deliveries()
            ordered = tuple(
                sorted(
                    deliveries,
                    key=lambda item: (item.created_at, str(item.delivery_id)),
                    reverse=True,
                )
            )
            page = ordered[offset : offset + limit]
            state_bytes = self._store.state_bytes()
            budget_exceeded = (
                state_bytes >= self._store.max_state_bytes
                or len(ordered) >= self._store.max_retained
            )
            retry_times = tuple(
                item.next_attempt_at
                for item in ordered
                if item.status is DesktopNotificationDeliveryStatus.RETRY_WAIT
                and item.next_attempt_at is not None
            )
            return DesktopNotificationStatus(
                enabled=config.enabled,
                enabled_at=config.enabled_at,
                updated_at=config.updated_at,
                capability=DesktopNotificationCapability(
                    available=config.last_probe_available,
                    reason=config.last_probe_reason,
                    checked_at=config.last_probe_at,
                ),
                last_test_at=config.last_test_at,
                last_test_accepted=config.last_test_accepted,
                last_test_reason=config.last_test_reason,
                total=len(ordered),
                pending_count=sum(
                    item.status is DesktopNotificationDeliveryStatus.PENDING for item in ordered
                ),
                attempting_count=sum(
                    item.status is DesktopNotificationDeliveryStatus.ATTEMPTING for item in ordered
                ),
                accepted_count=sum(
                    item.status is DesktopNotificationDeliveryStatus.ACCEPTED for item in ordered
                ),
                retry_wait_count=sum(
                    item.status is DesktopNotificationDeliveryStatus.RETRY_WAIT for item in ordered
                ),
                unknown_count=sum(
                    item.status is DesktopNotificationDeliveryStatus.UNKNOWN for item in ordered
                ),
                cancelled_count=sum(
                    item.status is DesktopNotificationDeliveryStatus.CANCELLED for item in ordered
                ),
                failed_count=sum(
                    item.status is DesktopNotificationDeliveryStatus.FAILED for item in ordered
                ),
                state_bytes=state_bytes,
                budget_bytes=self._store.max_state_bytes,
                retained_limit=self._store.max_retained,
                budget_exceeded=budget_exceeded,
                next_attempt_at=min(retry_times) if retry_times else None,
                offset=offset,
                limit=limit,
                returned=len(page),
                truncated=offset + len(page) < len(ordered),
                deliveries=page,
            )

    def has_due_work(self) -> bool:
        """Return whether one bounded batch is eligible without writing or probing."""
        with self._lock:
            config = self._store.load_config()
            if not config.enabled or config.enabled_at is None:
                return False
            if self._store.budget_exceeded():
                return False
            now = self._now()
            if (
                config.last_batch_at is not None
                and (now - config.last_batch_at).total_seconds() < MIN_BATCH_INTERVAL_SECONDS
            ):
                return False
            return any(
                delivery.status is DesktopNotificationDeliveryStatus.PENDING
                or (
                    delivery.status is DesktopNotificationDeliveryStatus.RETRY_WAIT
                    and delivery.next_attempt_at is not None
                    and delivery.next_attempt_at <= now
                )
                for delivery in self._store.load_deliveries()
            )

    def configure(self, *, enabled: bool) -> DesktopNotificationStatus:
        """Enable with verified registration or disable and cancel pending work."""
        with self._lock:
            now = self._now()
            capability: WindowsCapability | None = None
            if enabled:
                try:
                    capability = self._adapter.register()
                except WindowsNotificationError as error:
                    raise DesktopNotificationChannelError(error.reason) from error
                if not capability.aumid_registered:
                    raise DesktopNotificationChannelError(
                        DesktopNotificationReason.REGISTRATION_FAILED
                    )
            self._store.configure(enabled=enabled, at=now, capability=capability)
            return self.status()

    def test(self) -> DesktopNotificationTestResponse:
        """Send one explicitly labelled transport test at most once per minute."""
        with self._lock:
            config = self._store.load_config()
            if not config.enabled:
                raise DesktopNotificationChannelError(DesktopNotificationReason.CHANNEL_DISABLED)
            now = self._now()
            if (
                config.last_test_at is not None
                and (now - config.last_test_at).total_seconds() < MIN_BATCH_INTERVAL_SECONDS
            ):
                raise DesktopNotificationRateLimitError(
                    "a desktop notification test was already requested in the last minute"
                )
            capability = self._adapter.probe()
            self._store.record_probe(capability, now)
            if not capability.available:
                reason = capability.reason or DesktopNotificationReason.PROBE_FAILED
                self._store.record_test(accepted=False, reason=reason, at=now)
                return DesktopNotificationTestResponse(
                    accepted=False,
                    reason=reason,
                    checked_at=now,
                    capability=DesktopNotificationCapability(
                        available=capability.available,
                        reason=capability.reason,
                        checked_at=now,
                    ),
                )
            message = ToastMessage(
                tag="0000000000000000",
                group="0000000000000000",
                title="PRUEBA — sin señal de inversión",
                body="Prueba de transporte local. No es una señal ni un candidato financiero.",
            )
            outcome = self._adapter.send((message,))[message.tag]
            accepted = outcome.status is ToastSendStatus.ACCEPTED
            reason = None if accepted else outcome.reason
            self._store.record_test(accepted=accepted, reason=reason, at=now)
            return DesktopNotificationTestResponse(
                accepted=accepted,
                reason=reason,
                checked_at=now,
                capability=DesktopNotificationCapability(
                    available=capability.available,
                    reason=capability.reason,
                    checked_at=now,
                ),
            )

    def observe_batch(
        self,
        analytical_state: AnalyticalScreeningState,
        notification_state: CandidateNotificationState,
    ) -> int:
        """Admit eligible outbox items and deliver at most one bounded batch."""
        with self._lock:
            config = self._store.load_config()
            if not config.enabled or config.enabled_at is None:
                return 0
            if self._store.budget_exceeded():
                return 0
            self._admit_batch(analytical_state, notification_state, config)
            return self._deliver_batch(analytical_state, notification_state, config)

    def _admit_batch(
        self,
        analytical_state: AnalyticalScreeningState,
        notification_state: CandidateNotificationState,
        config: DesktopNotificationChannelConfig,
    ) -> None:
        rules = {item.rule.rule_id: item for item in self._rules().configurations}
        candidates = {item.candidate_id: item for item in analytical_state.candidates}
        results = {item.result_id: item for item in analytical_state.results}
        acknowledged = {item.notification_id for item in notification_state.transitions}
        if self._known_notifications is None:
            self._known_notifications = {
                item.notification_id for item in self._store.load_deliveries()
            }
        enabled_at = config.enabled_at
        assert enabled_at is not None
        admitted_at = self._now()
        for notification in notification_state.items:
            if notification.notification_id in self._known_notifications:
                continue
            if notification.created_at <= enabled_at:
                continue
            if notification.notification_id in acknowledged:
                continue
            candidate = candidates.get(notification.candidate_id)
            if candidate is None or candidate.status is not AnalyticalCandidateStatus.NEW:
                continue
            if candidate.activation_result_id != notification.activation_result_id:
                continue
            result = results.get(notification.activation_result_id)
            if result is None or result.rule.state is not AnalyticalRuleState.ACTIVE:
                continue
            rule_configuration = rules.get(notification.rule_id)
            if (
                rule_configuration is None
                or rule_configuration.rule.state is not AnalyticalRuleState.ACTIVE
            ):
                continue
            if rule_configuration.fingerprint != result.rule.semantic_fingerprint():
                continue
            if self._store.budget_exceeded():
                return
            try:
                self._store.admit(
                    notification,
                    title=notification.payload.rule_name_es,
                    body=_compact_body(notification),
                    at=admitted_at,
                )
            except DesktopNotificationBudgetError:
                return
            self._known_notifications.add(notification.notification_id)

    def _deliver_batch(
        self,
        analytical_state: AnalyticalScreeningState,
        notification_state: CandidateNotificationState,
        config: DesktopNotificationChannelConfig,
    ) -> int:
        now = self._now()
        if (
            config.last_batch_at is not None
            and (now - config.last_batch_at).total_seconds() < MIN_BATCH_INTERVAL_SECONDS
        ):
            return 0
        due = tuple(
            delivery
            for delivery in self._store.load_deliveries()
            if delivery.status is DesktopNotificationDeliveryStatus.PENDING
            or (
                delivery.status is DesktopNotificationDeliveryStatus.RETRY_WAIT
                and delivery.next_attempt_at is not None
                and delivery.next_attempt_at <= now
            )
        )
        if not due:
            return 0
        self._store.mark_batch(now)
        notifications = {item.notification_id: item for item in notification_state.items}
        acknowledged = {item.notification_id for item in notification_state.transitions}
        candidates = {item.candidate_id: item for item in analytical_state.candidates}
        results = {item.result_id: item for item in analytical_state.results}
        rules = {item.rule.rule_id: item for item in self._rules().configurations}
        eligible: list[DesktopNotificationDelivery] = []
        for delivery in due:
            reason = _ineligibility(
                delivery,
                config=config,
                notifications=notifications,
                acknowledged=acknowledged,
                candidates=candidates,
                results=results,
                rules=rules,
            )
            if reason is not None:
                self._store.cancel(delivery.delivery_id, reason=reason, at=now)
                continue
            if len(eligible) >= MAX_BATCH_DELIVERIES:
                break
            eligible.append(delivery)
        if not eligible:
            return 0
        attempting = tuple(
            self._store.begin_attempt(delivery.delivery_id, now) for delivery in eligible
        )
        messages = tuple(
            ToastMessage(
                tag=delivery.windows_tag,
                group=delivery.windows_group,
                title=delivery.title_es,
                body=delivery.body_es,
            )
            for delivery in attempting
        )
        try:
            outcomes = self._adapter.send(messages)
        except Exception:  # noqa: BLE001
            outcomes = {}
        completed_at = self._now()
        for delivery in attempting:
            outcome = outcomes.get(delivery.windows_tag)
            if outcome is None:
                self._store.record_outcome(
                    delivery.delivery_id,
                    status=DesktopNotificationDeliveryStatus.UNKNOWN,
                    at=completed_at,
                    reason=DesktopNotificationReason.SEND_ERROR,
                )
            elif outcome.status is ToastSendStatus.ACCEPTED:
                self._store.record_outcome(
                    delivery.delivery_id,
                    status=DesktopNotificationDeliveryStatus.ACCEPTED,
                    at=completed_at,
                )
            elif outcome.status is ToastSendStatus.UNKNOWN:
                self._store.record_outcome(
                    delivery.delivery_id,
                    status=DesktopNotificationDeliveryStatus.UNKNOWN,
                    at=completed_at,
                    reason=outcome.reason or DesktopNotificationReason.INVALID_RESULT,
                )
            elif delivery.attempt_count >= MAX_SEND_ATTEMPTS:
                self._store.record_outcome(
                    delivery.delivery_id,
                    status=DesktopNotificationDeliveryStatus.FAILED,
                    at=completed_at,
                    reason=outcome.reason or DesktopNotificationReason.SEND_ERROR,
                )
            else:
                self._store.record_outcome(
                    delivery.delivery_id,
                    status=DesktopNotificationDeliveryStatus.RETRY_WAIT,
                    at=completed_at,
                    reason=outcome.reason or DesktopNotificationReason.SEND_ERROR,
                )
        return len(attempting)


class DesktopNotificationMonitor:
    """Adapt the existing outbox batch without disturbing the scheduler."""

    def __init__(
        self,
        service: DesktopNotificationService,
        *,
        batch_loader: (
            Callable[[], tuple[AnalyticalScreeningState, CandidateNotificationState]] | None
        ) = None,
    ) -> None:
        self._service = service
        self._batch_loader = batch_loader

    def observe_batch(
        self,
        analytical_state: AnalyticalScreeningState,
        notification_state: CandidateNotificationState,
    ) -> None:
        try:
            self._service.observe_batch(analytical_state, notification_state)
        except Exception:  # noqa: BLE001
            return

    def observe_tick(self) -> None:
        """Drain one due batch on the existing scheduler tick, loading state only then."""
        try:
            if not self._service.has_due_work() or self._batch_loader is None:
                return
            analytical_state, notification_state = self._batch_loader()
            self._service.observe_batch(analytical_state, notification_state)
        except Exception:  # noqa: BLE001
            return


def _compact_body(notification: CandidateNotification) -> str:
    body = f"{notification.asset_id} · {notification.payload.explanation_es}"
    return body[:240]


def _ineligibility(
    delivery: DesktopNotificationDelivery,
    *,
    config: DesktopNotificationChannelConfig,
    notifications: Mapping[UUID, CandidateNotification],
    acknowledged: set[UUID],
    candidates: Mapping[UUID, AnalyticalCandidateEvent],
    results: Mapping[UUID, AnalyticalScreeningResult],
    rules: Mapping[str, AnalyticalRuleConfiguration],
) -> DesktopNotificationReason | None:
    notification = notifications.get(delivery.notification_id)
    if notification is None:
        return DesktopNotificationReason.NOTIFICATION_MISSING
    if notification.notification_id in acknowledged:
        return DesktopNotificationReason.CANDIDATE_ACKNOWLEDGED
    if (
        notification.candidate_id != delivery.candidate_id
        or notification.activation_result_id != delivery.activation_result_id
        or notification.rule_id != delivery.rule_id
        or notification.created_at != delivery.notification_created_at
    ):
        return DesktopNotificationReason.NOTIFICATION_EVIDENCE_MISMATCH
    if config.enabled_at is None or notification.created_at <= config.enabled_at:
        return DesktopNotificationReason.NOTIFICATION_BEFORE_ACTIVATION
    candidate = candidates.get(delivery.candidate_id)
    if candidate is None:
        return DesktopNotificationReason.CANDIDATE_MISSING
    if (
        candidate.activation_result_id != delivery.activation_result_id
        or candidate.rule_id != delivery.rule_id
    ):
        return DesktopNotificationReason.CANDIDATE_EVIDENCE_MISMATCH
    if candidate.status is not AnalyticalCandidateStatus.NEW:
        return DesktopNotificationReason.CANDIDATE_NOT_NEW
    result = results.get(delivery.activation_result_id)
    if result is None:
        return DesktopNotificationReason.RESULT_MISSING
    if result.rule.state is not AnalyticalRuleState.ACTIVE:
        return DesktopNotificationReason.RULE_NOT_ACTIVE_AT_ACTIVATION
    if result.rule.semantic_fingerprint() != candidate.rule_fingerprint:
        return DesktopNotificationReason.CANDIDATE_EVIDENCE_MISMATCH
    rule_configuration = rules.get(delivery.rule_id)
    if rule_configuration is None:
        return DesktopNotificationReason.RULE_CONFIGURATION_MISSING
    if rule_configuration.rule.state is not AnalyticalRuleState.ACTIVE:
        return DesktopNotificationReason.RULE_NOT_ACTIVE_NOW
    if rule_configuration.fingerprint != result.rule.semantic_fingerprint():
        return DesktopNotificationReason.RULE_FINGERPRINT_CHANGED
    return None


__all__ = [
    "DesktopNotificationBudgetError",
    "DesktopNotificationChannelError",
    "DesktopNotificationError",
    "DesktopNotificationMonitor",
    "DesktopNotificationRateLimitError",
    "DesktopNotificationService",
    "DesktopNotificationStore",
    "candidate_notification_fingerprint",
]
