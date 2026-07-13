"""Errors raised by the local storage layer."""


class StorageError(RuntimeError):
    """Base error for local storage failures."""


class RecordNotFoundError(StorageError):
    """Raised when a requested persistent record does not exist."""


class RecordConflictError(StorageError):
    """Raised when an immutable identifier is reused with different content."""


class StorageSchemaError(StorageError):
    """Raised when the DuckDB schema version is missing or incompatible."""
