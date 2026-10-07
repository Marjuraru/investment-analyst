"""Service for materializing and querying persisted institutional 13F events."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from investment_analyst.analytics.cazatiburones.institutional_event_definitions import (
    POLICY_VERSION,
)
from investment_analyst.analytics.cazatiburones.institutional_event_engine import (
    project_institutional_events,
)
from investment_analyst.analytics.cazatiburones.institutional_event_identity import snapshot_id
from investment_analyst.analytics.cazatiburones.institutional_event_models import (
    InstitutionalEventMaterializationSummary,
    InstitutionalEventSnapshot,
)
from investment_analyst.analytics.cazatiburones.institutional_event_repository import (
    InstitutionalEventRepository,
)
from investment_analyst.analytics.cazatiburones.institutional_metric_definitions import (
    ALGORITHM_VERSION as INSTITUTIONAL_METRIC_ALGORITHM_VERSION,
)
from investment_analyst.analytics.cazatiburones.institutional_metric_definitions import (
    INSTITUTIONAL_METRIC_DEFINITIONS,
)
from investment_analyst.core.models.metric import MetricResult
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.storage import LocalStorage, StorageError

_INSTITUTIONAL_METRIC_KEYS = tuple(
    definition.metric_key for definition in INSTITUTIONAL_METRIC_DEFINITIONS
)
_EVENT_METRIC_BATCH_SIZE = 256


class InstitutionalEventService:
    def __init__(
        self,
        storage: LocalStorage,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._storage = storage
        self._clock = clock
        self._repository = InstitutionalEventRepository(
            self._storage.paths.processed_dir,
            read_only=self._storage.read_only,
        )

    def materialize(
        self,
        *,
        asset_id: str,
        manager_cik: str,
        known_at: datetime,
    ) -> InstitutionalEventMaterializationSummary:
        if self._storage.read_only:
            raise StorageError("institutional event materialization requires writable storage")

        normalized_mgr = normalize_cik(manager_cik)
        recorded_at = self._clock()

        filtered_metrics = self._select_family_metrics(
            asset_id=asset_id, known_at=known_at, manager_cik=normalized_mgr
        )

        evaluations, events, candidates = project_institutional_events(filtered_metrics)

        # Snapshot identity is strictly deterministic
        snap_id = snapshot_id(
            {
                "asset_id": asset_id,
                "event_ids": [str(e.event_id) for e in events],
                "known_at": known_at,
                "manager_cik": normalized_mgr,
                "metric_result_ids": [str(m.result_id) for m in filtered_metrics],
                "policy_version": POLICY_VERSION,
            }
        )

        snapshot = InstitutionalEventSnapshot(
            snapshot_id=snap_id,
            asset_id=asset_id,
            manager_cik=normalized_mgr,
            known_at=known_at,
            recorded_at=recorded_at,
            policy_version=POLICY_VERSION,
            evaluations=evaluations,
            events=events,
            candidates=candidates,
            omissions=(),
        )

        created = self._repository.save(snapshot)

        return InstitutionalEventMaterializationSummary(
            asset_id=asset_id,
            manager_cik=normalized_mgr,
            known_at=known_at,
            snapshot_id=snap_id,
            created=created,
            events=len(events),
            candidates=len(candidates),
        )

    def _select_family_metrics(
        self, *, asset_id: str, known_at: datetime, manager_cik: str
    ) -> list[MetricResult]:
        """Select the versioned metric family before hydrating any document.

        The closed-set ``metric_keys`` projection, asset and PIT cut are pushed
        into storage as ID selection; each selected candidate is then validated
        for version, manager and cut before parsing its document into a model,
        so market metrics are never hydrated.
        """
        from investment_analyst.storage.errors import RecordNotFoundError

        candidate_ids = self._storage.metric_results.list_ids(
            asset_id=asset_id,
            metric_keys=_INSTITUTIONAL_METRIC_KEYS,
            available_to=known_at,
            parameter_equals={"manager_cik": manager_cik},
        )
        filtered: list[MetricResult] = []
        for offset in range(0, len(candidate_ids), _EVENT_METRIC_BATCH_SIZE):
            batch_ids = candidate_ids[offset : offset + _EVENT_METRIC_BATCH_SIZE]
            try:
                indexed = self._storage.metric_results.get_many(batch_ids)
            except RecordNotFoundError as error:
                raise StorageError("selected institutional metric is absent") from error
            missing = [result_id for result_id in batch_ids if result_id not in indexed]
            if missing:
                raise StorageError("selected institutional metric is absent")
            for result_id in batch_ids:
                item = indexed[result_id]
                if item.algorithm_version != INSTITUTIONAL_METRIC_ALGORITHM_VERSION:
                    continue
                item_mgr = item.parameters.get("manager_cik")
                if item_mgr is None:
                    continue
                try:
                    matches = normalize_cik(str(item_mgr)) == manager_cik
                except ValueError:
                    matches = str(item_mgr) == manager_cik
                if not matches:
                    continue
                if item.available_at > known_at:
                    continue
                filtered.append(item)
        filtered.sort(key=lambda item: (item.available_at, item.result_id))
        return filtered

    def query(
        self,
        *,
        asset_id: str,
        manager_cik: str,
        known_at: datetime,
        snapshot_id_value: UUID,
    ) -> InstitutionalEventSnapshot | None:
        normalized_mgr = normalize_cik(manager_cik)
        return self._repository.get(
            asset_id=asset_id,
            manager_cik=normalized_mgr,
            known_at=known_at.isoformat(),
            snapshot_id=snapshot_id_value,
        )
