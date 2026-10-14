"""Streaming, hash-bound inventory for a complete workspace-v2 backup."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path

from pydantic import ConfigDict, Field

from investment_analyst.core.models.base import ContractModel, NonEmptyStr


class WorkspaceV2BackupInventoryError(RuntimeError):
    """Raised when a backup inventory contains an unsafe or changed path."""


class WorkspaceV2BackupInventorySummary(ContractModel):
    """Bounded summary for a JSONL inventory whose entries remain streamable."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-backup-inventory-v1"
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    digest_sha256: NonEmptyStr


class WorkspaceV2BackupInventoryTrailer(ContractModel):
    """Terminal, hash-bound record for a streaming inventory."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-backup-inventory-trailer-v1"
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    digest_sha256: NonEmptyStr


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entries(root: Path) -> Iterator[dict[str, object]]:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise WorkspaceV2BackupInventoryError("backup inventory root is unsafe")
    for parent, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        directories.sort()
        filenames.sort()
        directory = Path(parent)
        if directory.is_symlink() or any((directory / name).is_symlink() for name in directories):
            raise WorkspaceV2BackupInventoryError("backup inventory cannot contain symbolic links")
        for name in filenames:
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise WorkspaceV2BackupInventoryError(
                    "backup inventory cannot contain symbolic links"
                )
            relative = path.relative_to(root)
            if any(part in {"", ".", ".."} for part in relative.parts):
                raise WorkspaceV2BackupInventoryError("backup inventory path is unsafe")
            stat = path.stat()
            yield {
                "path": relative.as_posix(),
                "sha256": _hash_file(path),
                "size_bytes": stat.st_size,
            }


def write_inventory(root: Path, output: Path) -> WorkspaceV2BackupInventorySummary:
    """Write a new JSONL inventory and return its deterministic trailer summary."""
    if output.exists() or output.is_symlink() or not output.is_absolute():
        raise WorkspaceV2BackupInventoryError("backup inventory output must be a new absolute path")
    output.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    count = 0
    total_bytes = 0
    candidates = tuple(
        path
        for path in output.parent.glob(f".{output.name}.*.tmp")
        if path.is_file() and not path.is_symlink()
    )
    if len(candidates) > 1:
        raise WorkspaceV2BackupInventoryError("backup inventory has ambiguous partial progress")
    temporary = (
        candidates[0] if candidates else output.with_name(f".{output.name}.{os.getpid()}.tmp")
    )
    try:
        if candidates:
            with temporary.open("r", encoding="utf-8") as prior:
                for line in prior:
                    try:
                        entry = json.loads(line)
                    except ValueError as error:
                        raise WorkspaceV2BackupInventoryError(
                            "backup inventory partial progress is malformed"
                        ) from error
                    if not isinstance(entry, dict) or "path" not in entry:
                        raise WorkspaceV2BackupInventoryError(
                            "backup inventory partial progress is malformed"
                        )
                    digest.update(line.encode("utf-8"))
                    count += 1
                    total_bytes += int(entry["size_bytes"])
        mode = "a" if candidates else "x"
        with temporary.open(mode, encoding="utf-8") as stream:
            for ordinal, entry in enumerate(_entries(root)):
                if ordinal < count:
                    continue
                line = json.dumps(entry, separators=(",", ":"), sort_keys=True)
                encoded = (line + "\n").encode("utf-8")
                stream.write(line + "\n")
                digest.update(encoded)
                count += 1
                total_bytes += int(entry["size_bytes"])
                if count % 256 == 0:
                    stream.flush()
                    os.fsync(stream.fileno())
            trailer = WorkspaceV2BackupInventoryTrailer(
                file_count=count,
                total_bytes=total_bytes,
                digest_sha256=digest.hexdigest(),
            )
            stream.write(json.dumps(trailer.model_dump(mode="json"), separators=(",", ":")))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return WorkspaceV2BackupInventorySummary(
        file_count=count,
        total_bytes=total_bytes,
        digest_sha256=digest.hexdigest(),
    )


def _sealed_trailer(path: Path) -> WorkspaceV2BackupInventoryTrailer:
    if path.is_symlink() or not path.is_file() or not path.is_absolute():
        raise WorkspaceV2BackupInventoryError("backup inventory is unavailable or unsafe")
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 8192))
            lines = stream.read().splitlines()
    except (OSError, UnicodeError) as error:
        raise WorkspaceV2BackupInventoryError(
            "backup inventory is unavailable or unsafe"
        ) from error
    if not lines:
        raise WorkspaceV2BackupInventoryError("backup inventory is missing its trailer")
    try:
        return WorkspaceV2BackupInventoryTrailer.model_validate_json(lines[-1])
    except ValueError as error:
        raise WorkspaceV2BackupInventoryError("backup inventory trailer is malformed") from error


def iter_inventory(path: Path) -> Iterator[dict[str, object]]:
    """Yield a sealed inventory once while validating its trailer incrementally."""
    trailer = _sealed_trailer(path)
    digest = hashlib.sha256()
    count = 0
    total_bytes = 0
    try:
        stream = path.open("r", encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise WorkspaceV2BackupInventoryError(
            "backup inventory is unavailable or unsafe"
        ) from error
    with stream:
        for line in stream:
            if count == trailer.file_count:
                expected_trailer = json.dumps(
                    trailer.model_dump(mode="json"), separators=(",", ":")
                )
                if line != expected_trailer + "\n":
                    raise WorkspaceV2BackupInventoryError(
                        "backup inventory trailer does not bind entries"
                    )
                break
            try:
                entry = json.loads(line)
            except ValueError as error:
                raise WorkspaceV2BackupInventoryError(
                    "backup inventory entry is malformed"
                ) from error
            if isinstance(entry, dict) and entry.get("schema_version") == (
                "workspace-v2-backup-inventory-trailer-v1"
            ):
                raise WorkspaceV2BackupInventoryError(
                    "backup inventory trailer does not bind entries"
                )
            if (
                not isinstance(entry, dict)
                or set(entry) != {"path", "sha256", "size_bytes"}
                or not isinstance(entry["path"], str)
                or not isinstance(entry["sha256"], str)
                or isinstance(entry["size_bytes"], bool)
                or not isinstance(entry["size_bytes"], int)
                or entry["size_bytes"] < 0
            ):
                raise WorkspaceV2BackupInventoryError("backup inventory entry is malformed")
            digest.update(line.encode("utf-8"))
            count += 1
            total_bytes += entry["size_bytes"]
            yield entry
    if (
        count != trailer.file_count
        or trailer.total_bytes != total_bytes
        or trailer.digest_sha256 != digest.hexdigest()
    ):
        raise WorkspaceV2BackupInventoryError("backup inventory trailer does not bind entries")


def load_inventory(
    path: Path,
) -> tuple[tuple[dict[str, object], ...], WorkspaceV2BackupInventorySummary]:
    """Compatibility reader for legacy callers; new packed flows use ``iter_inventory``."""
    entries = tuple(iter_inventory(path))
    trailer = _sealed_trailer(path)
    return (
        entries,
        WorkspaceV2BackupInventorySummary(
            file_count=trailer.file_count,
            total_bytes=trailer.total_bytes,
            digest_sha256=trailer.digest_sha256,
        ),
    )


def verify_inventory(path: Path) -> WorkspaceV2BackupInventorySummary:
    """Consume the complete inventory once without retaining entries."""
    for _ in iter_inventory(path):
        pass
    trailer = _sealed_trailer(path)
    return WorkspaceV2BackupInventorySummary(
        file_count=trailer.file_count,
        total_bytes=trailer.total_bytes,
        digest_sha256=trailer.digest_sha256,
    )


def inventory_summary(path: Path) -> WorkspaceV2BackupInventorySummary:
    """Read the sealed inventory trailer without rescanning its entries."""
    trailer = _sealed_trailer(path)
    return WorkspaceV2BackupInventorySummary(
        file_count=trailer.file_count,
        total_bytes=trailer.total_bytes,
        digest_sha256=trailer.digest_sha256,
    )


__all__ = [
    "WorkspaceV2BackupInventoryError",
    "WorkspaceV2BackupInventorySummary",
    "WorkspaceV2BackupInventoryTrailer",
    "inventory_summary",
    "iter_inventory",
    "load_inventory",
    "verify_inventory",
    "write_inventory",
]
