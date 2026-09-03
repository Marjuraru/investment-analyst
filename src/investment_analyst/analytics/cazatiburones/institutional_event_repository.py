"""Atomic filesystem persistence for institutional event snapshots."""

import os
from pathlib import Path
from uuid import UUID

from investment_analyst.analytics.cazatiburones.institutional_event_models import (
    InstitutionalEventSnapshot,
)

SNAPSHOT_DIRECTORY = "cazatiburones_institutional_events_v1"


class InstitutionalEventRepositoryError(RuntimeError):
    """Raised when repository cannot persist or read snapshots safely."""


class InstitutionalEventRepository:
    def __init__(self, storage_processed_dir: Path, *, read_only: bool = False) -> None:
        self._processed_dir = storage_processed_dir
        self._target_dir = self._processed_dir / SNAPSHOT_DIRECTORY
        self._read_only = read_only

    def _path(self, *, asset_id: str, manager_cik: str, known_at: str, snapshot_id: UUID) -> Path:
        clean_asset = asset_id.replace(":", "_").replace("/", "_")
        clean_mgr = manager_cik.replace(":", "_").replace("/", "_")
        clean_known = known_at.replace(":", "-")
        return self._target_dir / clean_asset / clean_mgr / f"{clean_known}_{snapshot_id}.json"

    def save(self, snapshot: InstitutionalEventSnapshot) -> bool:
        if self._read_only:
            raise InstitutionalEventRepositoryError(
                "institutional event snapshots require writable storage"
            )

        file_path = self._path(
            asset_id=snapshot.asset_id,
            manager_cik=snapshot.manager_cik,
            known_at=snapshot.known_at.isoformat(),
            snapshot_id=snapshot.snapshot_id,
        )
        file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = self._load(file_path)
            if existing.model_dump(exclude={"recorded_at"}) != snapshot.model_dump(
                exclude={"recorded_at"}
            ):
                raise InstitutionalEventRepositoryError(
                    "snapshot identity conflicts with existing content"
                ) from None
            return False

        document = snapshot.model_dump_json(indent=2).encode("utf-8")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            file_path.unlink(missing_ok=True)
            raise
        return True

    def _load(self, path: Path) -> InstitutionalEventSnapshot:
        try:
            return InstitutionalEventSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise InstitutionalEventRepositoryError(
                f"institutional event snapshot is malformed: {path}"
            ) from error

    def get(
        self, *, asset_id: str, manager_cik: str, known_at: str, snapshot_id: UUID
    ) -> InstitutionalEventSnapshot | None:
        file_path = self._path(
            asset_id=asset_id,
            manager_cik=manager_cik,
            known_at=known_at,
            snapshot_id=snapshot_id,
        )
        if not file_path.is_file():
            return None
        return self._load(file_path)

    def verify(self) -> int:
        if not self._target_dir.is_dir():
            return 0
        count = 0
        for path in sorted(self._target_dir.rglob("*.json")):
            self._load(path)
            count += 1
        return count
