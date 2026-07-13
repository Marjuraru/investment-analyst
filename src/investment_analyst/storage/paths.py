"""Filesystem locations used by local storage."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StoragePaths:
    """Immutable collection of paths rooted at an explicitly supplied directory."""

    root: Path
    raw_dir: Path
    processed_dir: Path
    exports_dir: Path
    database_path: Path

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
            database_path=processed_dir / "investment_analyst.duckdb",
        )

    def create_directories(self) -> None:
        """Create directories required by local storage."""
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
