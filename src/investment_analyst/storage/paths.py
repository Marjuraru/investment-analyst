"""Filesystem locations used by local storage."""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True, slots=True)
class StoragePaths:
    """Immutable collection of paths rooted at an explicitly supplied directory."""

    root: Path
    raw_dir: Path
    processed_dir: Path
    exports_dir: Path
    documents_dir: Path
    database_path: Path
    format_version: int = 1
    workspace_id: UUID | None = None

    @classmethod
    def from_root(cls, root: Path) -> "StoragePaths":
        """Build all storage paths without depending on the process working directory."""
        normalized_root = Path(root).expanduser().resolve()
        processed_dir = normalized_root / "data" / "processed"
        return cls(
            root=normalized_root,
            raw_dir=normalized_root / "data" / "raw",
            processed_dir=processed_dir,
            exports_dir=normalized_root / "data" / "exports",
            documents_dir=normalized_root / "data" / "documents",
            database_path=processed_dir / "investment_analyst.duckdb",
        )

    @classmethod
    def from_workspace_root(
        cls,
        root: Path,
        *,
        format_version: int,
        workspace_id: UUID,
    ) -> "StoragePaths":
        """Resolve storage paths for an already identified workspace format."""
        normalized_root = Path(root).expanduser().resolve()
        if format_version == 1:
            paths = cls.from_root(normalized_root / "storage")
            return cls(
                **{
                    field: getattr(paths, field)
                    for field in (
                        "root",
                        "raw_dir",
                        "processed_dir",
                        "exports_dir",
                        "documents_dir",
                        "database_path",
                    )
                },
                workspace_id=workspace_id,
            )
        if format_version != 2:
            raise ValueError("workspace storage format is unsupported")
        versioned_root = normalized_root / "storage" / "v2"
        return cls(
            root=versioned_root,
            raw_dir=versioned_root / "raw",
            processed_dir=versioned_root,
            exports_dir=normalized_root / "exports",
            documents_dir=normalized_root / "data" / "documents",
            database_path=versioned_root / "index.duckdb",
            format_version=2,
            workspace_id=workspace_id,
        )

    def create_directories(self) -> None:
        """Create directories required by local storage."""
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        if self.format_version == 1:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
