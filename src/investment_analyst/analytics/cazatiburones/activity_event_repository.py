"""Append-only JSON snapshot repository outside the evidence and DuckDB schemas."""

import hashlib
import os
from pathlib import Path
from uuid import UUID

from investment_analyst.analytics.cazatiburones.activity_event_models import ActivityEventSnapshot


class ActivityEventRepositoryError(RuntimeError):
    pass


class ActivityEventRepository:
    def __init__(self, processed_dir: Path, *, read_only: bool) -> None:
        self._root = processed_dir / "cazatiburones_activity_events_v1"
        self._read_only = read_only

    def save(self, snapshot: ActivityEventSnapshot) -> bool:
        if self._read_only:
            raise ActivityEventRepositoryError("activity event snapshots require writable storage")
        path = self._path(snapshot.asset_id, snapshot.known_at.isoformat(), snapshot.snapshot_id)
        document = snapshot.model_dump_json(indent=None).encode("utf-8") + b"\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = self._load(path)
            if existing.model_dump(exclude={"recorded_at"}) != snapshot.model_dump(
                exclude={"recorded_at"}
            ):
                raise ActivityEventRepositoryError(
                    "snapshot identity conflicts with existing content"
                ) from None
            return False
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return True

    def get(
        self, *, asset_id: str, known_at: str, snapshot_id: UUID
    ) -> ActivityEventSnapshot | None:
        path = self._path(asset_id, known_at, snapshot_id)
        return self._load(path) if path.is_file() else None

    def verify(self) -> int:
        """Validate every stored snapshot without creating directories or touching evidence."""
        if not self._root.exists():
            return 0
        count = 0
        for path in sorted(self._root.rglob("*.json")):
            self._load(path)
            count += 1
        return count

    def _path(self, asset_id: str, known_at: str, snapshot_id: UUID) -> Path:
        digest = hashlib.sha256(asset_id.encode("utf-8")).hexdigest()
        return self._root / digest / known_at.replace(":", "_") / f"{snapshot_id}.json"

    @staticmethod
    def _load(path: Path) -> ActivityEventSnapshot:
        try:
            return ActivityEventSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ActivityEventRepositoryError("activity event snapshot is malformed") from error
