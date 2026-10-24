"""Selective, resumable recovery of historical evidence into a v2 workspace.

The module implements two isolated contracts:

* ``historical-recovery-selection-v1`` — a deterministic, paginated selection of
  consumer-referenced historical entities plus the evidence required by their
  lineage. It is sealed before the destination is mutated.
* ``historical-recovery-progress-v1`` — a durable checkpoint bound to one exact
  selection, source and destination.

Selection is read-only. Import writes only the selected missing entities under a
single writer and verifies every page against the sealed source digest.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import DiagnosticMode
from investment_analyst.storage.compact_analytical_v2 import (
    _COMPONENT_TABLE,
    _DIAGNOSTIC_TABLE,
    _EVIDENCE_TABLE,
    _METRIC_TABLE,
    _SEGMENT_MEMBER_TABLE,
    _SEQUENCE_SEGMENT_TABLE,
    CompactAnalyticalStore,
    HistoricalAnalyticalSeal,
)
from investment_analyst.storage.local import LocalStorage
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex
from investment_analyst.workspace.historical_recovery_budget import (
    HistoricalRecoveryBudget,
    measure_historical_recovery_budget,
    require_historical_recovery_admission,
)
from investment_analyst.workspace.historical_selection import workspace_fingerprint
from investment_analyst.workspace.models import WorkspaceAccessMode, WorkspaceInspection
from investment_analyst.workspace.service import WorkspaceService

HISTORICAL_RECOVERY_SELECTION_SCHEMA = "historical-recovery-selection-v1"
HISTORICAL_RECOVERY_PROGRESS_SCHEMA = "historical-recovery-progress-v1"
HISTORICAL_RECOVERY_IMPORT_SCHEMA = "historical-recovery-import-v1"
HISTORICAL_RECOVERY_REPAIR_RECEIPT_SCHEMA = "historical-recovery-repair-receipt-v1"
HISTORICAL_RECOVERY_REPAIR_MANIFEST_SCHEMA = "data-chassis-48-historical-repair-manifest-v1"
HISTORICAL_RECOVERY_ADMISSION_SCHEMA = "historical-recovery-admission-v1"
HISTORICAL_RECOVERY_POLICY_VERSION = "historical-recovery-v2-pit"
HISTORICAL_RECOVERY_SELECTION_NAMESPACE = "investment-analyst:historical-recovery-selection-v1:"
HISTORICAL_RECOVERY_OWNED_OBSERVATION_FREQUENCIES = (
    "day_1",
    "annual",
    "quarterly",
    "event",
)
HISTORICAL_RECOVERY_PAGE_ITEMS = 4096
_PAGE_SIZE = 256
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MANAGER_OBSERVATION_SOURCE = "sec-edgar:institutional-holdings-observations"
_POSITION_SCHEMA = "sec-institutional-holding-position-v1"
_13F_SOURCE = "sec-edgar:institutional-holdings-13f"
_DOCUMENT_SOURCES = (
    "sec-edgar:primary-documents",
    "sec-edgar:filer-documents",
    "sec-edgar:beneficial-ownership-13d-13g",
    "sec-edgar:section16-ownership",
    "sec-edgar:institutional-holdings-semantics",
    "sec-edgar:institutional-row-correspondence",
    "sec-edgar:form-13f-data-set:manager-universe",
    "smv-open-data:registered-companies",
    "smv-open-data:registered-securities",
)
_KIND_ORDER = (
    "document-content",
    "raw-record",
    "observation",
    "metric-result",
    "diagnostic-result",
)


class HistoricalRecoveryError(RuntimeError):
    """Raised when a recovery selection, import or checkpoint cannot be proven safe."""


class HistoricalRecoveryPause(RuntimeError):
    """Raised after a bounded page budget; the durable checkpoint remains resumable."""


class HistoricalRecoveryEntityKind(StrEnum):
    """Closed entity kinds represented by one selection item."""

    DOCUMENT_CONTENT = "document-content"
    RAW_RECORD = "raw-record"
    OBSERVATION = "observation"
    METRIC_RESULT = "metric-result"
    DIAGNOSTIC_RESULT = "diagnostic-result"


class HistoricalRecoveryFamily(StrEnum):
    """Analytical or evidence family used for matrix and exclusion reporting."""

    MARKET = "market"
    FUNDAMENTALS = "fundamentals"
    DOCUMENTS = "documents"
    INSTITUTIONAL = "institutional"
    ACTIVITY = "activity"
    ANALYTICAL = "analytical"


class HistoricalRecoveryClassification(StrEnum):
    """Closed disposition of one source entity against the destination."""

    YA_PRESENTE = "YA_PRESENTE"
    IMPORTAR_FALTANTE = "IMPORTAR_FALTANTE"
    RECONSTRUIR_DERIVADO = "RECONSTRUIR_DERIVADO"
    ARCHIVE_ONLY = "ARCHIVE_ONLY"
    CONFLICTO = "CONFLICTO"
    AUSENTE_ORIGEN = "AUSENTE_ORIGEN"


class HistoricalRecoveryPresentBasis(StrEnum):
    """How an already-present destination entity was accepted."""

    IDENTICAL_CONTENT = "identical-content"
    SOURCE_IDENTITY_V2_NORMALIZATION = "source-identity-v2-normalization"


class HistoricalRecoveryItem(ContractModel):
    """One exact source entity selected, with its lineage and disposition."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    entity_kind: HistoricalRecoveryEntityKind
    entity_id: UUID
    family: HistoricalRecoveryFamily
    asset_id: NonEmptyStr | None = None
    source_id: NonEmptyStr
    frequency: NonEmptyStr | None = None
    range_start: UTCDateTime | None = None
    range_end: UTCDateTime | None = None
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    byte_length: int = Field(ge=0)
    consumer: NonEmptyStr
    classification: HistoricalRecoveryClassification
    dependencies: tuple[UUID, ...] = ()
    document_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    present_basis: HistoricalRecoveryPresentBasis | None = None
    admission_reason: (
        Literal["PIT_AVAILABLE_AT", "PIT_ASSET", "PIT_MISSING", "PIT_DEPENDENCY"] | None
    ) = None
    admission_detail: str | None = None

    @model_validator(mode="after")
    def _validate_item(self) -> HistoricalRecoveryItem:
        if self.dependencies != tuple(sorted(set(self.dependencies), key=str)):
            raise ValueError("historical recovery dependencies must be sorted and unique")
        if self.entity_kind is HistoricalRecoveryEntityKind.DOCUMENT_CONTENT:
            if self.document_sha256 != self.content_sha256:
                raise ValueError("document item digest must equal its content digest")
        elif self.document_sha256 is not None:
            raise ValueError("only document items carry a document digest")
        if self.classification is HistoricalRecoveryClassification.CONFLICTO:
            if self.present_basis is not None:
                raise ValueError("conflicting recovery items cannot declare a present basis")
        elif self.classification is HistoricalRecoveryClassification.YA_PRESENTE:
            if self.present_basis is None:
                raise ValueError("present recovery items require a present basis")
        elif self.present_basis is not None:
            raise ValueError("non-present recovery items cannot declare a present basis")
        if self.classification is HistoricalRecoveryClassification.ARCHIVE_ONLY:
            if self.admission_reason is None:
                raise ValueError("archive-only recovery items require a PIT reason")
        elif self.admission_reason is not None or self.admission_detail is not None:
            raise ValueError("non-archive recovery items cannot declare a PIT reason")
        return self


class HistoricalRecoveryPage(ContractModel):
    """One sealed JSONL page of selection items."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    relative_path: str = Field(min_length=1)
    entity_kind: HistoricalRecoveryEntityKind
    rows: int = Field(gt=0)
    byte_length: int = Field(gt=0)
    sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_page(self) -> HistoricalRecoveryPage:
        path = Path(self.relative_path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in self.relative_path
            or not path.parts
            or path.parts[0] != "pages"
        ):
            raise ValueError("historical recovery page path must be relative and safe")
        return self


class HistoricalRecoveryExclusion(ContractModel):
    """One asset/family absence or explicitly non-selected source family."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    asset_id: NonEmptyStr
    family: HistoricalRecoveryFamily
    reason: Literal["AUSENTE_ORIGEN", "N/A_CONTRATO", "ARCHIVE_ONLY"]
    detail: NonEmptyStr


class HistoricalRecoveryTotals(ContractModel):
    """Deterministic totals over every selection page."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    items: int = Field(ge=0)
    raw_records: int = Field(ge=0)
    observations: int = Field(ge=0)
    metric_results: int = Field(ge=0)
    diagnostic_results: int = Field(ge=0)
    document_contents: int = Field(ge=0)
    byte_length: int = Field(ge=0)
    import_required: int = Field(ge=0)
    already_present: int = Field(ge=0)
    conflicts: int = Field(ge=0)
    archive_only: int = Field(ge=0)
    rebuilt: int = Field(ge=0)


class HistoricalRecoveryFamilyTotal(ContractModel):
    """Per family/kind totals used for compact reporting."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    family: HistoricalRecoveryFamily
    entity_kind: HistoricalRecoveryEntityKind
    items: int = Field(ge=0)
    byte_length: int = Field(ge=0)
    import_required: int = Field(ge=0)
    already_present: int = Field(ge=0)


class HistoricalRecoveryManifest(ContractModel):
    """Closed selection manifest written before the destination is mutated."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["historical-recovery-selection-v1"] = (
        HISTORICAL_RECOVERY_SELECTION_SCHEMA
    )
    selection_id: UUID
    source_workspace_id: UUID
    source_format_version: Literal[1] = 1
    source_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    destination_workspace_id: UUID
    destination_format_version: Literal[2] = 2
    catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    known_at: UTCDateTime
    policy_version: NonEmptyStr
    content_sha256_basis: Literal["persisted-v1-document"] = "persisted-v1-document"
    pages: tuple[HistoricalRecoveryPage, ...]
    totals: HistoricalRecoveryTotals
    family_totals: tuple[HistoricalRecoveryFamilyTotal, ...]
    exclusions: tuple[HistoricalRecoveryExclusion, ...] = ()
    digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_manifest(self) -> HistoricalRecoveryManifest:
        paths = tuple(page.relative_path for page in self.pages)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("historical recovery pages must be sorted and unique")
        if len(self.family_totals) != len(
            {(item.family, item.entity_kind) for item in self.family_totals}
        ):
            raise ValueError("historical recovery family totals must be unique")
        if self.exclusions != tuple(
            sorted(
                set(self.exclusions),
                key=lambda item: (item.asset_id, item.family.value, item.reason, item.detail),
            )
        ):
            raise ValueError("historical recovery exclusions must be sorted and unique")
        if self.totals.items != sum(page.rows for page in self.pages):
            raise ValueError("historical recovery totals do not match page rows")
        if self.totals.items != sum(item.items for item in self.family_totals):
            raise ValueError("historical recovery totals do not match family totals")
        if self.totals.byte_length != sum(item.byte_length for item in self.family_totals):
            raise ValueError("historical recovery byte totals do not match")
        if self.totals.import_required != sum(item.import_required for item in self.family_totals):
            raise ValueError("historical recovery import totals do not match")
        if self.totals.already_present != sum(item.already_present for item in self.family_totals):
            raise ValueError("historical recovery present totals do not match")
        if self.totals.archive_only != (
            self.totals.items - self.totals.import_required - self.totals.already_present
        ):
            raise ValueError("historical recovery archive totals do not match")
        if self.totals.items != (
            self.totals.raw_records
            + self.totals.observations
            + self.totals.metric_results
            + self.totals.diagnostic_results
            + self.totals.document_contents
        ):
            raise ValueError("historical recovery entity totals do not match")
        if self.digest != _manifest_digest(self):
            raise ValueError("historical recovery digest does not match its content")
        if self.selection_id != _selection_id(self.digest):
            raise ValueError("historical recovery id does not match its digest")
        return self


class HistoricalRecoveryCounts(ContractModel):
    """Created/reused counters for one entity kind."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    created: int = Field(default=0, ge=0)
    reused: int = Field(default=0, ge=0)


class HistoricalRecoveryProgress(ContractModel):
    """Durable progress bound to one exact selection and destination."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["historical-recovery-progress-v1"] = HISTORICAL_RECOVERY_PROGRESS_SCHEMA
    selection_id: UUID
    selection_digest: str = Field(pattern=_SHA256_PATTERN)
    source_workspace_id: UUID
    destination_workspace_id: UUID
    phase: Literal["PREPARED", "IMPORTING", "VERIFYING", "COMPLETE"]
    current_kind: HistoricalRecoveryEntityKind | None = None
    page_index: int = Field(default=0, ge=0)
    document_contents: HistoricalRecoveryCounts = HistoricalRecoveryCounts()
    raw_records: HistoricalRecoveryCounts = HistoricalRecoveryCounts()
    observations: HistoricalRecoveryCounts = HistoricalRecoveryCounts()
    metric_results: HistoricalRecoveryCounts = HistoricalRecoveryCounts()
    diagnostic_results: HistoricalRecoveryCounts = HistoricalRecoveryCounts()
    completed_kinds: tuple[HistoricalRecoveryEntityKind, ...] = ()
    updated_at: UTCDateTime

    @model_validator(mode="after")
    def _validate_progress(self) -> HistoricalRecoveryProgress:
        order = {kind: index for index, kind in enumerate(_KIND_ORDER)}
        if self.completed_kinds != tuple(
            sorted(set(self.completed_kinds), key=lambda kind: order[kind.value])
        ):
            raise ValueError("historical recovery completed kinds must be ordered and unique")
        if self.current_kind is not None and self.current_kind in self.completed_kinds:
            raise ValueError("current historical recovery kind is already complete")
        if self.phase == "COMPLETE" and (
            self.current_kind is not None or len(self.completed_kinds) != len(_KIND_ORDER)
        ):
            raise ValueError("complete historical recovery progress is inconsistent")
        return self


class HistoricalRecoveryImportSummary(ContractModel):
    """Compact receipt for one complete or resumed selective import."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["historical-recovery-import-v1"] = HISTORICAL_RECOVERY_IMPORT_SCHEMA
    selection_id: UUID
    selection_digest: str = Field(pattern=_SHA256_PATTERN)
    destination_workspace_id: UUID
    source_workspace_id: UUID
    raw_items: int = Field(ge=0)
    observation_items: int = Field(ge=0)
    metric_items: int = Field(ge=0)
    diagnostic_items: int = Field(ge=0)
    document_items: int = Field(ge=0)
    created_raw: int = Field(ge=0)
    reused_raw: int = Field(ge=0)
    created_observations: int = Field(ge=0)
    reused_observations: int = Field(ge=0)
    created_metrics: int = Field(ge=0)
    reused_metrics: int = Field(ge=0)
    created_diagnostics: int = Field(ge=0)
    reused_diagnostics: int = Field(ge=0)
    created_documents: int = Field(ge=0)
    reused_documents: int = Field(ge=0)
    metric_digest: str = Field(pattern=_SHA256_PATTERN)
    diagnostic_digest: str = Field(pattern=_SHA256_PATTERN)
    replay: bool
    source_unchanged: bool
    completed_at: UTCDateTime


def _manifest_digest(manifest: HistoricalRecoveryManifest) -> str:
    payload = manifest.model_dump(mode="json", exclude={"digest", "selection_id"})
    encoded = json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _selection_id(digest: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"{HISTORICAL_RECOVERY_SELECTION_NAMESPACE}{digest}")


def _document_id(checksum: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"historical-recovery-document-v1:{checksum}")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _write_json(path: Path, document: object) -> None:
    encoded = json.dumps(document, allow_nan=False, separators=(",", ":"), sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded.encode("utf-8"))
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise HistoricalRecoveryError("historical recovery document is unreadable") from error


def _instant(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise HistoricalRecoveryError("historical recovery instant lacks timezone")
    return parsed.astimezone(UTC)


def _stable_record_key(value: str | None) -> str:
    """Ignore the volatile submissions snapshot identity inside company facts."""
    if value is None:
        return ""
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    if isinstance(parsed, dict):
        parsed.pop("submissions_record_id", None)
        return json.dumps(parsed, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return value


def _decimal_equal(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return left == right
    try:
        return Decimal(left) == Decimal(right)
    except (InvalidOperation, ValueError):
        return left == right


def _source_family(source_id: str) -> HistoricalRecoveryFamily:
    if source_id.startswith(
        ("sec-edgar:institutional", "sec-edgar:form-13f", "sec-edgar:manager")
    ) or source_id in {
        "sec-edgar:beneficial-ownership-13d-13g",
        "sec-edgar:section16-ownership",
    }:
        return HistoricalRecoveryFamily.INSTITUTIONAL
    if source_id in {"sec-edgar:primary-documents", "sec-edgar:filer-documents"}:
        return HistoricalRecoveryFamily.DOCUMENTS
    if source_id.startswith("smv-open-data"):
        return HistoricalRecoveryFamily.ACTIVITY
    if source_id.endswith(":submissions") or source_id.endswith(":companyfacts"):
        return HistoricalRecoveryFamily.FUNDAMENTALS
    return HistoricalRecoveryFamily.MARKET


def _metric_family(metric_key: str) -> HistoricalRecoveryFamily:
    if metric_key.startswith("cazatiburones"):
        return HistoricalRecoveryFamily.INSTITUTIONAL
    if metric_key.startswith(("fundamental.", "valuation.")):
        return HistoricalRecoveryFamily.FUNDAMENTALS
    return HistoricalRecoveryFamily.MARKET


def _diagnostic_family(mode: str) -> HistoricalRecoveryFamily:
    return (
        HistoricalRecoveryFamily.FUNDAMENTALS
        if mode == DiagnosticMode.FUNDAMENTAL.value
        else HistoricalRecoveryFamily.MARKET
    )


def _consumer(family: HistoricalRecoveryFamily) -> str:
    return {
        HistoricalRecoveryFamily.MARKET: "market-chart-comparison-technical",
        HistoricalRecoveryFamily.FUNDAMENTALS: "fundamental-research-valuation",
        HistoricalRecoveryFamily.DOCUMENTS: "sec-document-timeline",
        HistoricalRecoveryFamily.INSTITUTIONAL: "cazatiburones-institutional",
        HistoricalRecoveryFamily.ACTIVITY: "cazatiburones-declared-activity",
        HistoricalRecoveryFamily.ANALYTICAL: "listed-company-diagnostics",
    }[family]


def _catalog_sha256() -> str:
    import investment_analyst

    path = (
        Path(investment_analyst.__file__).resolve().parent / "catalog" / ("default_assets.v1.json")
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _catalog_assets() -> dict[str, str]:
    import investment_analyst

    path = (
        Path(investment_analyst.__file__).resolve().parent / "catalog" / ("default_assets.v1.json")
    )
    payload = json.loads(path.read_bytes())
    return {str(item["asset_id"]): str(item.get("asset_class", "")) for item in payload["assets"]}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_disjoint(source_root: Path, destination_root: Path) -> None:
    source = source_root.resolve(strict=False)
    destination = destination_root.resolve(strict=False)
    if source == destination or source in destination.parents or destination in source.parents:
        raise HistoricalRecoveryError("source and destination recovery roots must be disjoint")


def _iter_chunks(values: Sequence[object], size: int = _PAGE_SIZE) -> Iterator[Sequence[object]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def _create_temp_table(connection, name: str, sql: str, parameters: Sequence[object] = ()) -> None:
    connection.execute(f"CREATE TEMP TABLE {name} AS {sql}", list(parameters))


def _insert_ids(connection, table: str, column: str, identifiers: Iterable[UUID]) -> None:
    rows = [(str(identifier),) for identifier in identifiers]
    if rows:
        connection.executemany(f"INSERT OR IGNORE INTO {table} ({column}) VALUES (?)", rows)


def _manager_ciks(storage: LocalStorage) -> tuple[str, ...]:
    if storage.paths.format_version == 1:
        rows = storage.store.connection.execute(
            "SELECT DISTINCT json_extract_string("
            "json_extract_string(document_json, '$.source.record_key'), '$.manager_cik') "
            "FROM normalized_observations WHERE "
            "json_extract_string(document_json, '$.source.source_id') = ? ORDER BY 1",
            [_MANAGER_OBSERVATION_SOURCE],
        ).fetchall()
    else:
        rows = storage.store.connection.execute(
            "SELECT DISTINCT json_extract_string(source_record_key, '$.manager_cik') "
            "FROM normalized_observations_v2 WHERE source_id = ? ORDER BY 1",
            [_MANAGER_OBSERVATION_SOURCE],
        ).fetchall()
    return tuple(str(row[0]) for row in rows if row[0])


def _document_source_files(root: Path) -> Iterator[tuple[str, Path, int]]:
    base = root / "sha256"
    if not base.is_dir():
        return
    for first in sorted(base.iterdir()):
        if not first.is_dir():
            continue
        for second in sorted(first.iterdir()):
            if not second.is_dir():
                continue
            for path in sorted(second.iterdir()):
                if path.is_symlink() or not path.is_file():
                    continue
                checksum = path.name
                if (
                    len(checksum) != 64
                    or checksum[:2] != first.name
                    or checksum[2:4] != second.name
                    or any(char not in "0123456789abcdef" for char in checksum)
                ):
                    raise HistoricalRecoveryError("document content path is not canonical")
                yield checksum, path, path.stat().st_size


class _PageWriter:
    """Write deterministic JSONL pages without ever overwriting a page."""

    def __init__(self, root: Path, kind: HistoricalRecoveryEntityKind) -> None:
        self._root = root / "pages" / kind.value
        self._kind = kind
        self._root.mkdir(parents=True, exist_ok=False)
        self._items: list[HistoricalRecoveryItem] = []
        self._ordinal = 0
        self.pages: list[HistoricalRecoveryPage] = []

    def add(self, item: HistoricalRecoveryItem) -> None:
        self._items.append(item)
        if len(self._items) >= HISTORICAL_RECOVERY_PAGE_ITEMS:
            self.flush()

    def flush(self) -> None:
        if not self._items:
            return
        relative = f"pages/{self._kind.value}/{self._ordinal:06d}.jsonl"
        path = self._root / f"{self._ordinal:06d}.jsonl"
        encoded = b"".join(
            _canonical_bytes(item.model_dump(mode="json")) + b"\n" for item in self._items
        )
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        self.pages.append(
            HistoricalRecoveryPage(
                relative_path=relative,
                entity_kind=self._kind,
                rows=len(self._items),
                byte_length=len(encoded),
                sha256=hashlib.sha256(encoded).hexdigest(),
            )
        )
        self._items.clear()
        self._ordinal += 1

    def close(self) -> tuple[HistoricalRecoveryPage, ...]:
        self.flush()
        return tuple(self.pages)


class HistoricalRecoverySelectionBuilder:
    """Build one closed, consumer-referenced selection before any destination write."""

    def __init__(
        self,
        *,
        service: WorkspaceService,
        source_paths,
        destination_paths,
        source_inspection: WorkspaceInspection,
        destination_inspection: WorkspaceInspection,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        if source_inspection.format_version != 1:
            raise HistoricalRecoveryError("historical recovery source must be format 1")
        if destination_inspection.format_version != 2:
            raise HistoricalRecoveryError("historical recovery destination must be format 2")
        if source_inspection.workspace_id == destination_inspection.workspace_id:
            raise HistoricalRecoveryError("historical recovery requires distinct workspaces")
        self._service = service
        self._source_paths = source_paths
        self._destination_paths = destination_paths
        self._source_inspection = source_inspection
        self._destination_inspection = destination_inspection
        self._clock = clock

    def build(
        self,
        *,
        known_at: datetime,
        output_root: Path,
        backup_reused_bytes: int = 0,
        reserve_bytes: int = 5_000_000_000,
        host_free_bytes=None,
        wsl_free_bytes=None,
    ) -> tuple[HistoricalRecoveryManifest, dict[str, object], HistoricalRecoveryBudget]:
        if known_at.tzinfo is None or known_at.utcoffset() is None:
            raise HistoricalRecoveryError("historical recovery known_at must be timezone-aware")
        output = output_root.expanduser().resolve(strict=False)
        if output.exists() or output.is_symlink():
            raise HistoricalRecoveryError("historical recovery output root must be new")
        _ensure_disjoint(self._source_paths.root, output)
        _ensure_disjoint(self._destination_paths.root, output)
        output.mkdir(parents=True)
        source = self._service.open_storage(self._source_paths, WorkspaceAccessMode.READ_ONLY)
        destination = self._service.open_storage(
            self._destination_paths, WorkspaceAccessMode.READ_ONLY
        )
        self._source = source
        self._destination = destination
        try:
            source_manifest_fingerprint = workspace_fingerprint(
                workspace_id=self._source_inspection.workspace_id,
                format_version=self._source_inspection.format_version,
                manifest_sha256=hashlib.sha256(
                    self._source_paths.manifest_path.read_bytes()
                ).hexdigest(),
                raw_count=self._source_inspection.raw_record_count,
                observation_count=self._source_inspection.observation_count,
                metric_count=self._source_inspection.metric_result_count,
                diagnostic_count=self._source_inspection.diagnostic_result_count,
            )
            self._prepare_tables(source, destination)
            self._prepare_pit_maps(source, destination)
            writers = {
                kind: _PageWriter(output, kind)
                for kind in (
                    HistoricalRecoveryEntityKind.DOCUMENT_CONTENT,
                    HistoricalRecoveryEntityKind.RAW_RECORD,
                    HistoricalRecoveryEntityKind.OBSERVATION,
                    HistoricalRecoveryEntityKind.METRIC_RESULT,
                    HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT,
                )
            }
            totals = {
                "items": 0,
                "raw_records": 0,
                "observations": 0,
                "metric_results": 0,
                "diagnostic_results": 0,
                "document_contents": 0,
                "byte_length": 0,
                "import_required": 0,
                "already_present": 0,
                "conflicts": 0,
                "archive_only": 0,
                "rebuilt": 0,
            }
            family_totals: dict[tuple[str, str], dict[str, int]] = defaultdict(
                lambda: {
                    "items": 0,
                    "byte_length": 0,
                    "import_required": 0,
                    "already_present": 0,
                }
            )
            state = {"present_byte_length": 0, "archive_byte_length": 0}
            matrix_assets: dict[str, dict[str, dict[str, int]]] = defaultdict(
                lambda: defaultdict(
                    lambda: {
                        "selected": 0,
                        "import_required": 0,
                        "already_present": 0,
                        "archive_only": 0,
                        "bytes": 0,
                    }
                )
            )
            matrix_sources: dict[tuple[str, str, str, str], dict[str, object]] = defaultdict(
                lambda: {
                    "selected": 0,
                    "import_required": 0,
                    "already_present": 0,
                    "archive_only": 0,
                    "bytes": 0,
                    "range_start": None,
                    "range_end": None,
                }
            )
            for item in self._document_items(destination):
                self._accept(
                    item, writers, totals, family_totals, matrix_assets, matrix_sources, state
                )
            for item in self._raw_items(source, destination):
                self._accept(
                    item, writers, totals, family_totals, matrix_assets, matrix_sources, state
                )
            for item in self._observation_items(source, destination):
                self._accept(
                    item, writers, totals, family_totals, matrix_assets, matrix_sources, state
                )
            for item in self._metric_items(source, destination):
                self._accept(
                    item, writers, totals, family_totals, matrix_assets, matrix_sources, state
                )
            for item in self._diagnostic_items(source, destination):
                self._accept(
                    item, writers, totals, family_totals, matrix_assets, matrix_sources, state
                )
            pages: list[HistoricalRecoveryPage] = []
            for kind in _KIND_ORDER:
                pages.extend(writers[HistoricalRecoveryEntityKind(kind)].close())
            if totals["conflicts"]:
                raise HistoricalRecoveryError(
                    "historical recovery selection contains unresolved conflicts"
                )
            exclusions = self._exclusions(matrix_assets)
            family_totals_models = tuple(
                sorted(
                    (
                        HistoricalRecoveryFamilyTotal(
                            family=HistoricalRecoveryFamily(family),
                            entity_kind=HistoricalRecoveryEntityKind(kind),
                            items=values["items"],
                            byte_length=values["byte_length"],
                            import_required=values["import_required"],
                            already_present=values["already_present"],
                        )
                        for (kind, family), values in family_totals.items()
                    ),
                    key=lambda item: (item.family.value, item.entity_kind.value),
                )
            )
            totals_model = HistoricalRecoveryTotals(**totals)
            provisional = HistoricalRecoveryManifest.model_construct(
                schema_version=HISTORICAL_RECOVERY_SELECTION_SCHEMA,
                selection_id=UUID(int=0),
                source_workspace_id=self._source_inspection.workspace_id,
                source_format_version=1,
                source_fingerprint=source_manifest_fingerprint,
                destination_workspace_id=self._destination_inspection.workspace_id,
                destination_format_version=2,
                catalog_sha256=_catalog_sha256(),
                known_at=known_at,
                policy_version=HISTORICAL_RECOVERY_POLICY_VERSION,
                content_sha256_basis="persisted-v1-document",
                pages=tuple(sorted(pages, key=lambda page: page.relative_path)),
                totals=totals_model,
                family_totals=family_totals_models,
                exclusions=tuple(exclusions),
                digest="0" * 64,
            )
            digest = _manifest_digest(provisional)
            manifest = HistoricalRecoveryManifest(
                **provisional.model_dump(exclude={"digest", "selection_id"}, mode="python"),
                digest=digest,
                selection_id=_selection_id(digest),
            )
            matrix = {
                "schema_version": "historical-recovery-matrix-v1",
                "as_of": self._clock().isoformat(),
                "selection_id": str(manifest.selection_id),
                "selection_digest": manifest.digest,
                "source_workspace_id": str(manifest.source_workspace_id),
                "destination_workspace_id": str(manifest.destination_workspace_id),
                "catalog_sha256": manifest.catalog_sha256,
                "assets": {
                    asset_id: {
                        "asset_class": _catalog_assets().get(asset_id, ""),
                        "families": {
                            family: dict(values) for family, values in sorted(families.items())
                        },
                        "sources": [
                            {
                                "family": family,
                                "source_id": source_id,
                                "frequency": frequency,
                                **dict(values),
                            }
                            for (asset, family, source_id, frequency), values in sorted(
                                matrix_sources.items()
                            )
                            if asset == asset_id
                        ],
                    }
                    for asset_id, families in sorted(matrix_assets.items())
                },
                "source_inventory": self._source_inventory(source, destination),
                "exclusions": [item.model_dump(mode="json") for item in manifest.exclusions],
            }
            _write_json(output / "matrix.json", matrix)
            importable_bytes = totals["byte_length"] - state["archive_byte_length"]
            budget = measure_historical_recovery_budget(
                selected_payload_bytes=importable_bytes,
                present_payload_bytes=state["present_byte_length"],
                import_peak_bytes=importable_bytes - state["present_byte_length"],
                backup_new_bytes=importable_bytes - state["present_byte_length"],
                backup_reused_bytes=backup_reused_bytes,
                restore_peak_bytes=importable_bytes - state["present_byte_length"],
                reserve_bytes=reserve_bytes,
                host_free_bytes=host_free_bytes,
                wsl_free_bytes=wsl_free_bytes,
                clock=self._clock,
            )
            _write_json(output / "budget.json", budget.model_dump(mode="json"))
            _write_json(output / "selection.json", manifest.model_dump(mode="json"))
            return manifest, matrix, budget
        finally:
            destination.close()
            source.close()

    def _prepare_tables(self, source: LocalStorage, destination: LocalStorage) -> None:
        connection = source.store.connection
        connection.execute("SET preserve_insertion_order=false")
        connection.execute("SET threads=1")
        connection.execute("DROP TABLE IF EXISTS tmp_recovery_obs")
        connection.execute("DROP TABLE IF EXISTS tmp_recovery_metric")
        connection.execute("DROP TABLE IF EXISTS tmp_recovery_raw")
        connection.execute("DROP TABLE IF EXISTS tmp_recovery_manager")
        connection.execute("DROP TABLE IF EXISTS tmp_recovery_frontier")
        connection.execute(
            "CREATE TEMP TABLE tmp_recovery_obs (observation_id VARCHAR PRIMARY KEY)"
        )
        connection.execute("CREATE TEMP TABLE tmp_recovery_metric (result_id VARCHAR PRIMARY KEY)")
        connection.execute("DROP TABLE IF EXISTS tmp_recovery_raw_rows")
        connection.execute("DROP TABLE IF EXISTS tmp_recovery_obs_rows")
        connection.execute("DROP TABLE IF EXISTS tmp_recovery_metric_rows")
        connection.execute("CREATE TEMP TABLE tmp_recovery_raw (record_id VARCHAR PRIMARY KEY)")
        connection.execute(
            "CREATE TEMP TABLE tmp_recovery_manager (manager_cik VARCHAR PRIMARY KEY)"
        )
        connection.execute(
            "CREATE TEMP TABLE tmp_recovery_frontier (result_id VARCHAR PRIMARY KEY)"
        )
        managers = _manager_ciks(source)
        if managers:
            connection.executemany(
                "INSERT INTO tmp_recovery_manager (manager_cik) VALUES (?)",
                [(manager,) for manager in managers],
            )
        owned_frequency_placeholders = _placeholders(
            len(HISTORICAL_RECOVERY_OWNED_OBSERVATION_FREQUENCIES)
        )
        connection.execute(
            "INSERT INTO tmp_recovery_obs (observation_id) "
            "SELECT observation_id FROM normalized_observations "
            f"WHERE frequency IN ({owned_frequency_placeholders})",
            list(HISTORICAL_RECOVERY_OWNED_OBSERVATION_FREQUENCIES),
        )
        diagnostic_metric_ids: set[UUID] = set()
        for (document,) in connection.execute(
            "SELECT document_json FROM diagnostic_results"
        ).fetchall():
            payload = json.loads(document)
            for component in payload.get("components", []):
                diagnostic_metric_ids.update(
                    UUID(str(value)) for value in component.get("metric_result_ids", [])
                )
            for evidence in payload.get("evidence", []):
                if evidence.get("metric_result_id"):
                    diagnostic_metric_id = UUID(str(evidence["metric_result_id"]))
                    diagnostic_metric_ids.add(diagnostic_metric_id)
        _insert_ids(connection, "tmp_recovery_metric", "result_id", diagnostic_metric_ids)
        connection.execute(
            "INSERT OR IGNORE INTO tmp_recovery_metric (result_id) "
            "SELECT result_id FROM metric_results WHERE metric_key LIKE 'cazatiburones.%'"
        )
        for _ in range(8):
            connection.execute(
                "CREATE OR REPLACE TEMP TABLE tmp_recovery_frontier AS "
                "SELECT DISTINCT CAST(unnest(CAST(json_extract(document_json, "
                "'$.input_metric_result_ids') AS UUID[])) AS VARCHAR) AS result_id "
                "FROM metric_results WHERE result_id IN "
                "(SELECT result_id FROM tmp_recovery_metric) "
                "EXCEPT SELECT result_id FROM tmp_recovery_metric"
            )
            if connection.execute("SELECT count(*) FROM tmp_recovery_frontier").fetchone()[0] == 0:
                break
            connection.execute(
                "INSERT OR IGNORE INTO tmp_recovery_metric (result_id) "
                "SELECT result_id FROM tmp_recovery_frontier"
            )
        else:
            raise HistoricalRecoveryError("historical recovery metric closure did not converge")
        missing_metrics = connection.execute(
            "SELECT count(*) FROM tmp_recovery_frontier WHERE result_id NOT IN "
            "(SELECT result_id FROM metric_results)"
        ).fetchone()[0]
        if missing_metrics:
            raise HistoricalRecoveryError("historical recovery references a missing metric")
        connection.execute(
            "INSERT OR IGNORE INTO tmp_recovery_obs (observation_id) "
            "SELECT DISTINCT CAST(unnest(CAST(json_extract(document_json, "
            "'$.input_observation_ids') AS UUID[])) AS VARCHAR) AS observation_id "
            "FROM metric_results WHERE result_id IN "
            "(SELECT result_id FROM tmp_recovery_metric)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO tmp_recovery_raw (record_id) "
            "SELECT raw_record_id FROM normalized_observations "
            "WHERE observation_id IN (SELECT observation_id FROM tmp_recovery_obs)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO tmp_recovery_raw (record_id) "
            "SELECT record_id FROM raw_record_index WHERE source_id IN "
            f"({_placeholders(len(_DOCUMENT_SOURCES))}) "
            "OR source_id LIKE 'sec-edgar:%:submissions' "
            "OR source_id LIKE 'sec-edgar:manager:%:submissions' "
            "OR (source_id = ? AND schema_version <> ?) "
            "OR (schema_version = ? AND json_extract_string(document_json, "
            "'$.payload.position.information_table_revision.filer_cik') IN "
            "(SELECT manager_cik FROM tmp_recovery_manager))",
            [
                *_DOCUMENT_SOURCES,
                _13F_SOURCE,
                _POSITION_SCHEMA,
                _POSITION_SCHEMA,
            ],
        )
        connection.execute(
            "CREATE TEMP TABLE tmp_recovery_raw_rows ("
            "record_id VARCHAR PRIMARY KEY, asset_id VARCHAR, source_id VARCHAR, "
            "schema_version VARCHAR, content_sha256 VARCHAR, byte_length BIGINT, "
            "available_at VARCHAR, event_time VARCHAR, source_checksum VARCHAR, "
            "source_record_key VARCHAR)"
        )
        connection.execute(
            "INSERT INTO tmp_recovery_raw_rows "
            "SELECT r.record_id, r.asset_id, r.source_id, r.schema_version, "
            "sha256(r.document_json) AS content_sha256, "
            "octet_length(encode(r.document_json)) AS byte_length, "
            "CAST(r.available_at AS VARCHAR) AS available_at, "
            "CAST(r.event_time AS VARCHAR) AS event_time, "
            "json_extract_string(r.document_json, '$.source.checksum_sha256') "
            "AS source_checksum, "
            "json_extract_string(r.document_json, '$.source.record_key') AS source_record_key "
            "FROM raw_record_index r WHERE r.record_id IN "
            "(SELECT record_id FROM tmp_recovery_raw)"
        )
        connection.execute(
            "CREATE TEMP TABLE tmp_recovery_obs_rows ("
            "observation_id VARCHAR PRIMARY KEY, raw_record_id VARCHAR, asset_id VARCHAR, "
            "field_name VARCHAR, frequency VARCHAR, observed_at VARCHAR, period_end VARCHAR, "
            "available_at VARCHAR, content_sha256 VARCHAR, byte_length BIGINT, "
            "source_id VARCHAR, source_record_key VARCHAR, value VARCHAR, unit VARCHAR)"
        )
        connection.execute(
            "INSERT INTO tmp_recovery_obs_rows "
            "SELECT o.observation_id, o.raw_record_id, o.asset_id, o.field_name, o.frequency, "
            "CAST(o.observed_at AS VARCHAR) AS observed_at, "
            "CAST(o.period_end AS VARCHAR) AS period_end, "
            "CAST(o.available_at AS VARCHAR) AS available_at, "
            "sha256(o.document_json) AS content_sha256, "
            "octet_length(encode(o.document_json)) AS byte_length, "
            "json_extract_string(o.document_json, '$.source.source_id') AS source_id, "
            "json_extract_string(o.document_json, '$.source.record_key') AS source_record_key, "
            "json_extract_string(o.document_json, '$.value') AS value, "
            "json_extract_string(o.document_json, '$.unit') AS unit "
            "FROM normalized_observations o WHERE o.observation_id IN "
            "(SELECT observation_id FROM tmp_recovery_obs)"
        )
        connection.execute(
            "CREATE TEMP TABLE tmp_recovery_metric_rows ("
            "result_id VARCHAR PRIMARY KEY, asset_id VARCHAR, metric_key VARCHAR, as_of VARCHAR, "
            "available_at VARCHAR, content_sha256 VARCHAR, byte_length BIGINT, source_id VARCHAR, "
            "input_observations VARCHAR, input_metrics VARCHAR)"
        )
        connection.execute(
            "INSERT INTO tmp_recovery_metric_rows "
            "SELECT m.result_id, m.asset_id, m.metric_key, CAST(m.as_of AS VARCHAR) AS as_of, "
            "CAST(m.available_at AS VARCHAR) AS available_at, "
            "sha256(m.document_json) AS content_sha256, "
            "octet_length(encode(m.document_json)) AS byte_length, "
            "json_extract_string(m.document_json, '$.parameters.source_id') AS source_id, "
            "json_extract_string(m.document_json, '$.input_observation_ids') "
            "AS input_observations, "
            "json_extract_string(m.document_json, '$.input_metric_result_ids') AS input_metrics "
            "FROM metric_results m WHERE m.result_id IN "
            "(SELECT result_id FROM tmp_recovery_metric)"
        )

    def _prepare_pit_maps(self, source: LocalStorage, destination: LocalStorage) -> None:
        source_connection = source.store.connection
        destination_connection = destination.store.connection
        selected_observations = {
            UUID(str(row[0]))
            for row in source_connection.execute(
                "SELECT observation_id FROM tmp_recovery_obs"
            ).fetchall()
        }
        selected_metrics = {
            UUID(str(row[0]))
            for row in source_connection.execute(
                "SELECT result_id FROM tmp_recovery_metric"
            ).fetchall()
        }
        self._pit_source_observations = {
            UUID(str(row[0])): (str(row[1]), _instant(str(row[2])))
            for row in source_connection.execute(
                "SELECT observation_id, asset_id, CAST(available_at AS VARCHAR) "
                "FROM normalized_observations WHERE observation_id IN "
                "(SELECT observation_id FROM tmp_recovery_obs)"
            ).fetchall()
        }
        self._pit_destination_observations = {
            UUID(str(row[0])): (str(row[1]), _instant(str(row[2])))
            for row in destination_connection.execute(
                "SELECT observation_id, asset_id, CAST(available_at AS VARCHAR) "
                "FROM normalized_observations_v2"
            ).fetchall()
            if UUID(str(row[0])) in selected_observations
        }
        self._pit_source_metrics = {
            UUID(str(row[0])): (str(row[1]), _instant(str(row[2])))
            for row in source_connection.execute(
                "SELECT result_id, asset_id, CAST(available_at AS VARCHAR) "
                "FROM metric_results WHERE result_id IN "
                "(SELECT result_id FROM tmp_recovery_metric)"
            ).fetchall()
        }
        self._pit_destination_metrics = {
            UUID(str(row[0])): (str(row[1]), _instant(str(row[2])))
            for row in destination_connection.execute(
                "SELECT result_id, asset_id, CAST(available_at AS VARCHAR) "
                "FROM workspace_metric_results_v2"
            ).fetchall()
            if UUID(str(row[0])) in selected_metrics
        }
        self._metric_admissions: dict[
            UUID, tuple[HistoricalRecoveryClassification, str | None, str | None, str, object]
        ] = {}

    def _metric_pit_admission(
        self,
        asset_id: str | None,
        available_at: datetime | None,
        observation_ids: Sequence[UUID],
        metric_ids: Sequence[UUID],
    ) -> tuple[str | None, str | None]:
        if asset_id is None or available_at is None:
            return "PIT_MISSING", "historical metric has no asset or availability"
        for identifier in observation_ids:
            source = self._pit_source_observations.get(identifier)
            if source is None:
                return "PIT_MISSING", f"source observation {identifier} is absent"
            if source[0] != asset_id:
                return "PIT_ASSET", f"source observation {identifier} belongs to {source[0]}"
            if source[1] is None or source[1] > available_at:
                return (
                    "PIT_AVAILABLE_AT",
                    f"source observation {identifier} is available after the metric",
                )
            destination = self._pit_destination_observations.get(identifier)
            if destination is not None:
                if destination[0] != asset_id:
                    return (
                        "PIT_ASSET",
                        f"destination observation {identifier} belongs to {destination[0]}",
                    )
                if destination[1] is None or destination[1] > available_at:
                    return (
                        "PIT_AVAILABLE_AT",
                        f"destination observation {identifier} is available after the metric",
                    )
        for identifier in metric_ids:
            source = self._pit_source_metrics.get(identifier)
            if source is None:
                return "PIT_MISSING", f"source metric {identifier} is absent"
            if source[0] != asset_id:
                return "PIT_ASSET", f"source metric {identifier} belongs to {source[0]}"
            if source[1] is None or source[1] > available_at:
                return (
                    "PIT_AVAILABLE_AT",
                    f"source metric {identifier} is available after the parent",
                )
            destination = self._pit_destination_metrics.get(identifier)
            if destination is not None:
                if destination[0] != asset_id:
                    return (
                        "PIT_ASSET",
                        f"destination metric {identifier} belongs to {destination[0]}",
                    )
                if destination[1] is None or destination[1] > available_at:
                    return (
                        "PIT_AVAILABLE_AT",
                        f"destination metric {identifier} is available after the parent",
                    )
        return None, None

    def _diagnostic_pit_admission(
        self,
        asset_id: str | None,
        available_at: datetime | None,
        metric_ids: Sequence[UUID],
    ) -> tuple[str | None, str | None]:
        if asset_id is None or available_at is None:
            return "PIT_MISSING", "historical diagnostic has no asset or availability"
        for identifier in metric_ids:
            admission = self._metric_admissions.get(identifier)
            if admission is None:
                return "PIT_DEPENDENCY", f"metric {identifier} is absent from the selection"
            if admission[0] is HistoricalRecoveryClassification.ARCHIVE_ONLY:
                return (
                    "PIT_DEPENDENCY",
                    f"metric {identifier} is PIT-archive-only: {admission[1]}",
                )
            if admission[3] != asset_id:
                return "PIT_ASSET", f"metric {identifier} belongs to {admission[3]}"
            if admission[4] is None or admission[4] > available_at:
                return "PIT_AVAILABLE_AT", f"metric {identifier} is available after the diagnostic"
        return None, None

    def _accept(
        self,
        item: HistoricalRecoveryItem,
        writers: dict[HistoricalRecoveryEntityKind, _PageWriter],
        totals: dict[str, int],
        family_totals: dict[tuple[str, str], dict[str, int]],
        matrix_assets: dict[str, dict[str, dict[str, int]]],
        matrix_sources: dict[tuple[str, str, str, str], dict[str, object]],
        state: dict[str, int],
    ) -> None:
        if item.classification is HistoricalRecoveryClassification.CONFLICTO:
            totals["conflicts"] += 1
            raise HistoricalRecoveryError(
                f"historical recovery conflict for {item.entity_kind.value} {item.entity_id}"
            )
        writers[item.entity_kind].add(item)
        totals["items"] += 1
        totals["byte_length"] += item.byte_length
        key = (item.entity_kind.value, item.family.value)
        family_totals[key]["items"] += 1
        family_totals[key]["byte_length"] += item.byte_length
        if item.classification is HistoricalRecoveryClassification.IMPORTAR_FALTANTE:
            totals["import_required"] += 1
            family_totals[key]["import_required"] += 1
        elif item.classification is HistoricalRecoveryClassification.YA_PRESENTE:
            totals["already_present"] += 1
            family_totals[key]["already_present"] += 1
            state["present_byte_length"] += item.byte_length
        elif item.classification is HistoricalRecoveryClassification.ARCHIVE_ONLY:
            totals["archive_only"] += 1
            state["archive_byte_length"] += item.byte_length
        if item.entity_kind is HistoricalRecoveryEntityKind.RAW_RECORD:
            totals["raw_records"] += 1
        elif item.entity_kind is HistoricalRecoveryEntityKind.OBSERVATION:
            totals["observations"] += 1
        elif item.entity_kind is HistoricalRecoveryEntityKind.METRIC_RESULT:
            totals["metric_results"] += 1
        elif item.entity_kind is HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT:
            totals["diagnostic_results"] += 1
        elif item.entity_kind is HistoricalRecoveryEntityKind.DOCUMENT_CONTENT:
            totals["document_contents"] += 1
        if item.asset_id:
            values = matrix_assets[item.asset_id][item.family.value]
            values["selected"] += 1
            values["bytes"] += item.byte_length
            if item.classification is HistoricalRecoveryClassification.IMPORTAR_FALTANTE:
                values["import_required"] += 1
            elif item.classification is HistoricalRecoveryClassification.YA_PRESENTE:
                values["already_present"] += 1
            elif item.classification is HistoricalRecoveryClassification.ARCHIVE_ONLY:
                values["archive_only"] += 1
            source_values = matrix_sources[
                (
                    item.asset_id,
                    item.family.value,
                    item.source_id,
                    item.frequency or item.entity_kind.value,
                )
            ]
            source_values["selected"] += 1
            source_values["bytes"] += item.byte_length
            if item.classification is HistoricalRecoveryClassification.IMPORTAR_FALTANTE:
                source_values["import_required"] += 1
            elif item.classification is HistoricalRecoveryClassification.YA_PRESENTE:
                source_values["already_present"] += 1
            elif item.classification is HistoricalRecoveryClassification.ARCHIVE_ONLY:
                source_values["archive_only"] += 1
            if item.range_start is not None:
                current = source_values["range_start"]
                if current is None or item.range_start.isoformat() < current:
                    source_values["range_start"] = item.range_start.isoformat()
            if item.range_end is not None:
                current = source_values["range_end"]
                if current is None or item.range_end.isoformat() > current:
                    source_values["range_end"] = item.range_end.isoformat()

    def _source_inventory(
        self, source: LocalStorage, destination: LocalStorage
    ) -> dict[str, object]:
        source_connection = source.store.connection
        destination_connection = destination.store.connection
        return {
            "v1": {
                "observations": source_connection.execute(
                    "SELECT asset_id, json_extract_string(document_json, '$.source.source_id'), "
                    "frequency, count(*), min(observed_at)::varchar, max(observed_at)::varchar, "
                    "sum(octet_length(encode(document_json))) "
                    "FROM normalized_observations GROUP BY 1,2,3 ORDER BY 1,2,3"
                ).fetchall(),
                "raw_records": source_connection.execute(
                    "SELECT asset_id, source_id, schema_version, count(*), "
                    "sum(octet_length(encode(document_json))) FROM raw_record_index "
                    "GROUP BY 1,2,3 ORDER BY 1,2,3"
                ).fetchall(),
                "metric_results": source_connection.execute(
                    "SELECT asset_id, metric_key, count(*) FROM metric_results "
                    "GROUP BY 1,2 ORDER BY 1,2"
                ).fetchall(),
                "diagnostic_results": source_connection.execute(
                    "SELECT asset_id, mode, count(*) FROM diagnostic_results "
                    "GROUP BY 1,2 ORDER BY 1,2"
                ).fetchall(),
            },
            "v2": {
                "observations": destination_connection.execute(
                    "SELECT asset_id, source_id, frequency, count(*), "
                    "min(observed_at), max(observed_at) FROM normalized_observations_v2 "
                    "GROUP BY 1,2,3 ORDER BY 1,2,3"
                ).fetchall(),
                "raw_records": destination_connection.execute(
                    "SELECT asset_id, source_id, schema_version, count(*) FROM raw_v2_index "
                    "GROUP BY 1,2,3 ORDER BY 1,2,3"
                ).fetchall(),
                "metric_results": destination_connection.execute(
                    "SELECT asset_id, metric_key, count(*) FROM workspace_metric_results_v2 "
                    "GROUP BY 1,2 ORDER BY 1,2"
                ).fetchall(),
                "diagnostic_results": destination_connection.execute(
                    "SELECT asset_id, count(*) FROM workspace_diagnostic_results_v2 "
                    "GROUP BY 1 ORDER BY 1"
                ).fetchall(),
            },
        }

    def _document_items(self, destination: LocalStorage) -> Iterator[HistoricalRecoveryItem]:
        for checksum, path, size in _document_source_files(self._source.paths.documents_dir):
            if _sha256_file(path) != checksum:
                raise HistoricalRecoveryError("source document content checksum mismatch")
            target = (
                destination.paths.documents_dir / "sha256" / checksum[:2] / checksum[2:4] / checksum
            )
            if target.is_symlink():
                raise HistoricalRecoveryError("destination document content path is unsafe")
            if target.is_file():
                try:
                    destination.documents.verify(checksum, size_bytes=size)
                except Exception as error:
                    raise HistoricalRecoveryError(
                        "destination document content conflicts with the source"
                    ) from error
                classification = HistoricalRecoveryClassification.YA_PRESENTE
                basis = HistoricalRecoveryPresentBasis.IDENTICAL_CONTENT
            else:
                classification = HistoricalRecoveryClassification.IMPORTAR_FALTANTE
                basis = None
            yield HistoricalRecoveryItem(
                entity_kind=HistoricalRecoveryEntityKind.DOCUMENT_CONTENT,
                entity_id=_document_id(checksum),
                family=HistoricalRecoveryFamily.DOCUMENTS,
                asset_id=None,
                source_id="document-cas",
                content_sha256=checksum,
                byte_length=size,
                consumer=_consumer(HistoricalRecoveryFamily.DOCUMENTS),
                classification=classification,
                document_sha256=checksum,
                present_basis=basis,
            )

    def _raw_items(
        self, source: LocalStorage, destination: LocalStorage
    ) -> Iterator[HistoricalRecoveryItem]:
        connection = source.store.connection
        rows_all = connection.execute(
            "SELECT record_id, asset_id, source_id, schema_version, content_sha256, "
            "byte_length, available_at, event_time, source_checksum, source_record_key "
            "FROM tmp_recovery_raw_rows ORDER BY record_id"
        ).fetchall()
        connection.execute("DROP TABLE IF EXISTS tmp_recovery_raw_rows")
        for page_rows in _iter_chunks(rows_all, _PAGE_SIZE):
            identifiers = [UUID(str(row[0])) for row in page_rows]
            destination_rows = self._destination_raw_rows(destination, identifiers)
            for row in page_rows:
                record_id = UUID(str(row[0]))
                present = destination_rows.get(record_id)
                item = self._raw_item(row, present)
                yield item

    def _destination_raw_rows(
        self, destination: LocalStorage, identifiers: Sequence[UUID]
    ) -> dict[UUID, tuple[object, ...]]:
        if not identifiers:
            return {}
        rows = destination.store.connection.execute(
            "SELECT record_id, relative_path, schema_version, asset_id, source_id "
            f"FROM raw_v2_index WHERE record_id IN ({_placeholders(len(identifiers))})",
            [str(identifier) for identifier in identifiers],
        ).fetchall()
        output: dict[UUID, tuple[object, ...]] = {}
        for row in rows:
            identifier = UUID(str(row[0]))
            relative = str(row[1])
            path = destination.paths.raw_dir / relative
            if path.is_symlink() or not path.is_file():
                raise HistoricalRecoveryError("historical recovery destination raw blob is unsafe")
            try:
                payload = json.loads(path.read_bytes())
            except (OSError, ValueError) as error:
                raise HistoricalRecoveryError(
                    "historical recovery destination raw blob is unreadable"
                ) from error
            source = payload.get("source", {})
            output[identifier] = (
                str(payload.get("schema_version") or row[2]),
                payload.get("asset_id"),
                str(source.get("source_id") or row[4]),
                source.get("checksum_sha256"),
                source.get("record_key"),
            )
        return output

    def _raw_item(self, row, present) -> HistoricalRecoveryItem:
        record_id = UUID(str(row[0]))
        asset_id = str(row[1]) if row[1] is not None else None
        source_id = str(row[2])
        schema_version = str(row[3])
        content_sha256 = str(row[4])
        byte_length = int(row[5])
        family = _source_family(source_id)
        classification = HistoricalRecoveryClassification.IMPORTAR_FALTANTE
        basis = None
        if present is not None:
            same_source = (
                present[0] == schema_version
                and (present[1] or None) == asset_id
                and present[2] == source_id
                and present[3] == row[8]
                and present[4] == row[9]
            )
            if not same_source:
                classification = HistoricalRecoveryClassification.CONFLICTO
            else:
                classification = HistoricalRecoveryClassification.YA_PRESENTE
                basis = HistoricalRecoveryPresentBasis.SOURCE_IDENTITY_V2_NORMALIZATION
        return HistoricalRecoveryItem(
            entity_kind=HistoricalRecoveryEntityKind.RAW_RECORD,
            entity_id=record_id,
            family=family,
            asset_id=asset_id,
            source_id=source_id,
            range_start=_instant(row[7]),
            range_end=_instant(row[6]),
            content_sha256=content_sha256,
            byte_length=byte_length,
            consumer=_consumer(family),
            classification=classification,
            present_basis=basis,
        )

    def _observation_items(
        self, source: LocalStorage, destination: LocalStorage
    ) -> Iterator[HistoricalRecoveryItem]:
        connection = source.store.connection
        rows_all = connection.execute(
            "SELECT observation_id, raw_record_id, asset_id, field_name, frequency, "
            "observed_at, period_end, available_at, content_sha256, byte_length, source_id, "
            "source_record_key, value, unit FROM tmp_recovery_obs_rows ORDER BY observation_id"
        ).fetchall()
        connection.execute("DROP TABLE IF EXISTS tmp_recovery_obs_rows")
        for page_rows in _iter_chunks(rows_all, _PAGE_SIZE):
            identifiers = [UUID(str(row[0])) for row in page_rows]
            destination_rows = self._destination_observation_rows(destination, identifiers)
            for row in page_rows:
                yield self._observation_item(row, destination_rows.get(UUID(str(row[0]))))

    def _destination_observation_rows(
        self, destination: LocalStorage, identifiers: Sequence[UUID]
    ) -> dict[UUID, tuple]:
        rows = destination.store.connection.execute(
            "SELECT observation_id, raw_record_id, asset_id, field_name, frequency, "
            "observed_at, period_start, period_end, available_at, value_text, unit, "
            "source_id, source_record_key FROM normalized_observations_v2 "
            f"WHERE observation_id IN ({_placeholders(len(identifiers))})",
            [str(identifier) for identifier in identifiers],
        ).fetchall()
        return {UUID(str(row[0])): row for row in rows}

    def _observation_item(self, row, present) -> HistoricalRecoveryItem:
        observation_id = UUID(str(row[0]))
        source_id = str(row[10] or "")
        family = _source_family(source_id)
        classification = HistoricalRecoveryClassification.IMPORTAR_FALTANTE
        basis = None
        if present is not None:
            same_source = (
                str(present[1]) == str(row[1])
                and (str(present[2]) if present[2] is not None else None)
                == (str(row[2]) if row[2] is not None else None)
                and str(present[3]) == str(row[3])
                and str(present[4]) == str(row[4])
                and _instant(present[5]) == _instant(row[5])
                and _instant(present[7]) == _instant(row[6])
                and _decimal_equal(str(present[9]), str(row[12]))
                and str(present[10]) == str(row[13])
                and str(present[11]) == source_id
                and _stable_record_key(str(present[12])) == _stable_record_key(str(row[11]))
            )
            if not same_source:
                classification = HistoricalRecoveryClassification.CONFLICTO
            else:
                classification = HistoricalRecoveryClassification.YA_PRESENTE
                basis = HistoricalRecoveryPresentBasis.SOURCE_IDENTITY_V2_NORMALIZATION
        return HistoricalRecoveryItem(
            entity_kind=HistoricalRecoveryEntityKind.OBSERVATION,
            entity_id=observation_id,
            family=family,
            asset_id=str(row[2]) if row[2] is not None else None,
            source_id=source_id or "unknown",
            frequency=str(row[4]),
            range_start=_instant(row[5]) or _instant(row[6]),
            range_end=_instant(row[6]) or _instant(row[5]),
            content_sha256=str(row[8]),
            byte_length=int(row[9]),
            consumer=_consumer(family),
            classification=classification,
            dependencies=(UUID(str(row[1])),),
            present_basis=basis,
        )

    def _metric_items(
        self, source: LocalStorage, destination: LocalStorage
    ) -> Iterator[HistoricalRecoveryItem]:
        connection = source.store.connection
        rows_all = connection.execute(
            "SELECT result_id, asset_id, metric_key, as_of, available_at, content_sha256, "
            "byte_length, source_id, input_observations, input_metrics "
            "FROM tmp_recovery_metric_rows ORDER BY result_id"
        ).fetchall()
        connection.execute("DROP TABLE IF EXISTS tmp_recovery_metric_rows")
        for page_rows in _iter_chunks(rows_all, _PAGE_SIZE):
            identifiers = [UUID(str(row[0])) for row in page_rows]
            present = self._destination_analytical(destination, identifiers, metrics=True)
            for row in page_rows:
                yield self._metric_item(row, present.get(UUID(str(row[0]))))

    def _diagnostic_items(
        self, source: LocalStorage, destination: LocalStorage
    ) -> Iterator[HistoricalRecoveryItem]:
        connection = source.store.connection
        cursor: str | None = None
        while True:
            where = "" if cursor is None else "WHERE d.diagnostic_id > ?"
            parameters: list[object] = [] if cursor is None else [cursor]
            rows = connection.execute(
                "SELECT d.diagnostic_id, d.asset_id, d.mode, CAST(d.as_of AS VARCHAR), "
                "CAST(d.available_at AS VARCHAR), sha256(d.document_json), "
                "octet_length(encode(d.document_json)), d.document_json "
                f"FROM diagnostic_results d {where} "
                f"ORDER BY d.diagnostic_id LIMIT {_PAGE_SIZE}",
                parameters,
            ).fetchall()
            if not rows:
                return
            cursor = str(rows[-1][0])
            identifiers = [UUID(str(row[0])) for row in rows]
            present = self._destination_analytical(destination, identifiers, metrics=False)
            for row in rows:
                yield self._diagnostic_item(row, present.get(UUID(str(row[0]))))

    def _destination_analytical(
        self, destination: LocalStorage, identifiers: Sequence[UUID], *, metrics: bool
    ) -> dict[UUID, object]:
        table = "workspace_metric_results_v2" if metrics else "workspace_diagnostic_results_v2"
        key = "result_id" if metrics else "diagnostic_id"
        rows = destination.store.connection.execute(
            f"SELECT {key} FROM {table} WHERE {key} IN ({_placeholders(len(identifiers))})",
            [str(identifier) for identifier in identifiers],
        ).fetchall()
        present = {UUID(str(row[0])) for row in rows}
        if not present:
            return {}
        return (
            destination.metric_results.get_many(sorted(present, key=str))
            if metrics
            else destination.diagnostics.get_many(sorted(present, key=str))
        )

    def _metric_item(self, row, present) -> HistoricalRecoveryItem:
        metric_key = str(row[2])
        family = _metric_family(metric_key)
        classification = HistoricalRecoveryClassification.IMPORTAR_FALTANTE
        basis = None
        if present is not None:
            source = self._source.metric_results.get(UUID(str(row[0])))
            if source.model_dump(exclude={"computed_at"}) != present.model_dump(
                exclude={"computed_at"}
            ):
                classification = HistoricalRecoveryClassification.CONFLICTO
            else:
                classification = HistoricalRecoveryClassification.YA_PRESENTE
                basis = HistoricalRecoveryPresentBasis.IDENTICAL_CONTENT
        observation_ids = tuple(UUID(str(value)) for value in json.loads(row[8] or "[]"))
        metric_input_ids = tuple(UUID(str(value)) for value in json.loads(row[9] or "[]"))
        dependencies = tuple(sorted({*observation_ids, *metric_input_ids}, key=str))
        admission_reason, admission_detail = self._metric_pit_admission(
            str(row[1]) if row[1] is not None else None,
            _instant(row[4]),
            observation_ids,
            metric_input_ids,
        )
        if admission_reason is not None and classification is not (
            HistoricalRecoveryClassification.CONFLICTO
        ):
            classification = HistoricalRecoveryClassification.ARCHIVE_ONLY
            basis = None
        entity_id = UUID(str(row[0]))
        self._metric_admissions[entity_id] = (
            classification,
            admission_reason,
            admission_detail,
            str(row[1]) if row[1] is not None else "unscoped",
            _instant(row[4]),
        )
        source_id = str(row[7] or "analytical-v1")
        return HistoricalRecoveryItem(
            entity_kind=HistoricalRecoveryEntityKind.METRIC_RESULT,
            entity_id=entity_id,
            family=family,
            asset_id=str(row[1]) if row[1] is not None else None,
            source_id=source_id,
            frequency=None,
            range_start=_instant(row[3]),
            range_end=_instant(row[3]),
            content_sha256=str(row[5]),
            byte_length=int(row[6]),
            consumer=_consumer(family),
            classification=classification,
            dependencies=dependencies,
            present_basis=basis,
            admission_reason=admission_reason,
            admission_detail=admission_detail,
        )

    def _diagnostic_item(self, row, present) -> HistoricalRecoveryItem:
        payload = json.loads(str(row[7]))
        family = _diagnostic_family(str(row[2]))
        classification = HistoricalRecoveryClassification.IMPORTAR_FALTANTE
        basis = None
        if present is not None:
            source = self._source.diagnostics.get(UUID(str(row[0])))
            if source.model_dump(exclude={"computed_at"}) != present.model_dump(
                exclude={"computed_at"}
            ):
                classification = HistoricalRecoveryClassification.CONFLICTO
            else:
                classification = HistoricalRecoveryClassification.YA_PRESENTE
                basis = HistoricalRecoveryPresentBasis.IDENTICAL_CONTENT
        dependencies: set[UUID] = set()
        for component in payload.get("components", []):
            dependencies.update(
                UUID(str(value)) for value in component.get("metric_result_ids", [])
            )
        for evidence in payload.get("evidence", []):
            if evidence.get("metric_result_id"):
                dependencies.add(UUID(str(evidence["metric_result_id"])))
        ordered_dependencies = tuple(sorted(dependencies, key=str))
        admission_reason, admission_detail = self._diagnostic_pit_admission(
            str(row[1]) if row[1] is not None else None,
            _instant(row[4]),
            ordered_dependencies,
        )
        if admission_reason is not None and classification is not (
            HistoricalRecoveryClassification.CONFLICTO
        ):
            classification = HistoricalRecoveryClassification.ARCHIVE_ONLY
            basis = None
        return HistoricalRecoveryItem(
            entity_kind=HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT,
            entity_id=UUID(str(row[0])),
            family=family,
            asset_id=str(row[1]) if row[1] is not None else None,
            source_id="diagnostic-v1",
            frequency=None,
            range_start=_instant(row[3]),
            range_end=_instant(row[3]),
            content_sha256=str(row[5]),
            byte_length=int(row[6]),
            consumer=_consumer(family),
            classification=classification,
            dependencies=ordered_dependencies,
            present_basis=basis,
            admission_reason=admission_reason,
            admission_detail=admission_detail,
        )

    def _exclusions(
        self, matrix_assets: dict[str, dict[str, dict[str, int]]]
    ) -> tuple[HistoricalRecoveryExclusion, ...]:
        catalog = _catalog_assets()
        exclusions: list[HistoricalRecoveryExclusion] = []
        for asset_id, asset_class in catalog.items():
            families = matrix_assets.get(asset_id, {})
            is_bvl = ":bvl:" in asset_id
            for family in (HistoricalRecoveryFamily.MARKET, HistoricalRecoveryFamily.FUNDAMENTALS):
                if families.get(family.value, {}).get("selected", 0):
                    continue
                if is_bvl:
                    reason = "N/A_CONTRATO"
                    detail = "catalog asset has no supported market/fundamental provider binding"
                elif family is HistoricalRecoveryFamily.FUNDAMENTALS and asset_class in {
                    "crypto",
                    "etf",
                }:
                    reason = "N/A_CONTRATO"
                    detail = "catalog contract does not support issuer fundamentals for this class"
                else:
                    reason = "AUSENTE_ORIGEN"
                    detail = "no source evidence exists for this asset and family"
                exclusions.append(
                    HistoricalRecoveryExclusion(
                        asset_id=asset_id,
                        family=family,
                        reason=reason,
                        detail=detail,
                    )
                )
        exclusions.extend(
            (
                HistoricalRecoveryExclusion(
                    asset_id="*",
                    family=HistoricalRecoveryFamily.MARKET,
                    reason="ARCHIVE_ONLY",
                    detail="minute_1 and hour_1 source families are not current consumers",
                ),
                HistoricalRecoveryExclusion(
                    asset_id="*",
                    family=HistoricalRecoveryFamily.ANALYTICAL,
                    reason="ARCHIVE_ONLY",
                    detail=(
                        "non-referenced reproducible metrics and diagnostics stay archive-only;"
                        " referenced diagnostics and cazatiburones artifacts are preserved"
                    ),
                ),
                HistoricalRecoveryExclusion(
                    asset_id="*",
                    family=HistoricalRecoveryFamily.MARKET,
                    reason="ARCHIVE_ONLY",
                    detail="FRED macro series are not bound to a current catalog consumer",
                ),
            )
        )
        return tuple(
            sorted(
                set(exclusions),
                key=lambda item: (item.asset_id, item.family.value, item.reason, item.detail),
            )
        )


class HistoricalRecoveryImporter:
    """Import or resume one sealed selection with page-level verification."""

    def __init__(
        self,
        *,
        source: LocalStorage,
        destination: LocalStorage,
        manifest: HistoricalRecoveryManifest,
        manifest_root: Path,
        checkpoint_path: Path,
        source_fingerprint: str,
        admission: HistoricalRecoveryAdmission | None = None,
        page_limit: int = _PAGE_SIZE,
        failure_injector=None,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        if source.read_only is not True:
            raise HistoricalRecoveryError("historical recovery source must be read-only")
        if destination.read_only:
            raise HistoricalRecoveryError("historical recovery destination must be writable")
        if not 1 <= page_limit <= _PAGE_SIZE:
            raise HistoricalRecoveryError("historical recovery page limit is out of range")
        if source.paths.workspace_id != manifest.source_workspace_id:
            raise HistoricalRecoveryError("source workspace does not match the selection")
        if destination.paths.workspace_id != manifest.destination_workspace_id:
            raise HistoricalRecoveryError("destination workspace does not match the selection")
        if destination.paths.format_version != 2:
            raise HistoricalRecoveryError("historical recovery destination must be v2")
        if source_fingerprint != manifest.source_fingerprint:
            raise HistoricalRecoveryError("source fingerprint does not match the selection")
        if admission is not None:
            validate_historical_recovery_admission(admission, manifest, manifest_root)
        self._source = source
        self._destination = destination
        self._manifest = manifest
        self._root = manifest_root.resolve()
        self._checkpoint_path = checkpoint_path
        self._admission = admission
        self._page_limit = page_limit
        self._failure_injector = failure_injector
        self._clock = clock
        self._compact = CompactAnalyticalStore(destination.store.connection)

    def _page_path(self, page: HistoricalRecoveryPage) -> Path:
        path = self._root / page.relative_path
        try:
            path.relative_to(self._root)
        except ValueError as error:
            raise HistoricalRecoveryError("historical recovery page escapes its root") from error
        if path.is_symlink() or not path.is_file():
            raise HistoricalRecoveryError("historical recovery page is missing or unsafe")
        return path

    def _page_items(self, page: HistoricalRecoveryPage) -> tuple[HistoricalRecoveryItem, ...]:
        path = self._page_path(page)
        data = path.read_bytes()
        if len(data) != page.byte_length or hashlib.sha256(data).hexdigest() != page.sha256:
            raise HistoricalRecoveryError("historical recovery page digest differs")
        items: list[HistoricalRecoveryItem] = []
        for line in data.splitlines():
            if not line:
                raise HistoricalRecoveryError("historical recovery page contains an empty row")
            items.append(HistoricalRecoveryItem.model_validate_json(line))
        if len(items) != page.rows:
            raise HistoricalRecoveryError("historical recovery page row count differs")
        if any(item.entity_kind is not page.entity_kind for item in items):
            raise HistoricalRecoveryError("historical recovery page entity kind differs")
        return tuple(items)

    def _load_progress(self) -> HistoricalRecoveryProgress | None:
        if not self._checkpoint_path.exists():
            return None
        try:
            progress = HistoricalRecoveryProgress.model_validate_json(
                self._checkpoint_path.read_bytes()
            )
        except (OSError, ValueError) as error:
            raise HistoricalRecoveryError("historical recovery checkpoint is invalid") from error
        if (
            progress.selection_id != self._manifest.selection_id
            or progress.selection_digest != self._manifest.digest
            or progress.source_workspace_id != self._manifest.source_workspace_id
            or progress.destination_workspace_id != self._manifest.destination_workspace_id
        ):
            raise HistoricalRecoveryError("checkpoint is bound to another selection")
        if progress.current_kind is not None:
            page_count = sum(
                1 for page in self._manifest.pages if page.entity_kind is progress.current_kind
            )
            if progress.page_index > page_count:
                raise HistoricalRecoveryError("historical recovery cursor is out of range")
        return progress

    def _write_progress(self, progress: HistoricalRecoveryProgress) -> None:
        encoded = _canonical_bytes(progress.model_dump(mode="json"))
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._checkpoint_path.with_name(
            f".{self._checkpoint_path.name}.{os.getpid()}.tmp"
        )
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._checkpoint_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _progress(self, **changes: object) -> HistoricalRecoveryProgress:
        existing = self._load_progress()
        base = (
            existing.model_dump()
            if existing is not None
            else {
                "schema_version": HISTORICAL_RECOVERY_PROGRESS_SCHEMA,
                "selection_id": self._manifest.selection_id,
                "selection_digest": self._manifest.digest,
                "source_workspace_id": self._manifest.source_workspace_id,
                "destination_workspace_id": self._manifest.destination_workspace_id,
                "phase": "PREPARED",
                "current_kind": None,
                "page_index": 0,
                "completed_kinds": (),
                "updated_at": self._clock(),
            }
        )
        base.update(changes)
        base["updated_at"] = self._clock()
        progress = HistoricalRecoveryProgress.model_validate(base)
        self._write_progress(progress)
        return progress

    def run(self, *, max_pages: int | None = None) -> HistoricalRecoveryImportSummary:
        if max_pages is not None and (
            isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1
        ):
            raise HistoricalRecoveryError("historical recovery page budget must be positive")
        pages_processed = 0
        progress = self._load_progress()
        importable = {
            kind: sum(
                family.import_required + family.already_present
                for family in self._manifest.family_totals
                if family.entity_kind is kind
            )
            for kind in HistoricalRecoveryEntityKind
        }
        replay = progress is not None and progress.phase == "COMPLETE"
        if progress is not None and progress.phase == "COMPLETE":
            complete = (
                progress.raw_records.created + progress.raw_records.reused
                == importable[HistoricalRecoveryEntityKind.RAW_RECORD]
                and progress.observations.created + progress.observations.reused
                == importable[HistoricalRecoveryEntityKind.OBSERVATION]
                and progress.metric_results.created + progress.metric_results.reused
                == importable[HistoricalRecoveryEntityKind.METRIC_RESULT]
                and progress.diagnostic_results.created + progress.diagnostic_results.reused
                == importable[HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT]
                and progress.document_contents.created + progress.document_contents.reused
                == importable[HistoricalRecoveryEntityKind.DOCUMENT_CONTENT]
            )
            if not complete:
                raise HistoricalRecoveryError("complete checkpoint does not match this selection")
        if not replay:
            progress = self._progress(phase="IMPORTING")
        for kind_value in _KIND_ORDER:
            kind = HistoricalRecoveryEntityKind(kind_value)
            pages = tuple(page for page in self._manifest.pages if page.entity_kind is kind)
            if progress is not None and kind in progress.completed_kinds:
                for page in pages:
                    self._page_items(page)
                continue
            start = (
                progress.page_index if progress is not None and progress.current_kind is kind else 0
            )
            if start > len(pages):
                raise HistoricalRecoveryError("historical recovery cursor is out of range")
            for index in range(start, len(pages)):
                if progress is None:
                    raise AssertionError("progress must be initialized")
                self._import_page(kind, pages[index])
                if self._failure_injector is not None:
                    self._failure_injector("after_page_import")
                progress = self._progress(
                    phase="IMPORTING",
                    current_kind=kind,
                    page_index=index + 1,
                )
                pages_processed += 1
                if max_pages is not None and pages_processed >= max_pages:
                    raise HistoricalRecoveryPause(
                        f"historical recovery paused after {pages_processed} page"
                    )
            progress = self._progress(
                phase="IMPORTING",
                current_kind=None,
                page_index=0,
                completed_kinds=(*progress.completed_kinds, kind),
            )
        assert progress is not None
        selection_counts = self._selection_counts()
        expected_metric_historical = selection_counts[HistoricalRecoveryEntityKind.METRIC_RESULT][
            0
        ] + self._present_historical_count(HistoricalRecoveryEntityKind.METRIC_RESULT)
        expected_diagnostic_historical = selection_counts[
            HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT
        ][0] + self._present_historical_count(HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT)
        if not replay:
            metric_count, metric_digest = self._historical_digests(metrics=True)
            diagnostic_count, diagnostic_digest = self._historical_digests(metrics=False)
            if metric_count != expected_metric_historical:
                raise HistoricalRecoveryError(
                    "historical metric count differs from the selected historical closure"
                )
            if diagnostic_count != expected_diagnostic_historical:
                raise HistoricalRecoveryError(
                    "historical diagnostic count differs from the selected historical closure"
                )
            self._compact.verify_historical_relationships()
            self._compact.seal_historical_inventory(
                source_fingerprint=self._manifest.source_fingerprint,
                metric_count=metric_count,
                metric_digest=metric_digest,
                diagnostic_count=diagnostic_count,
                diagnostic_digest=diagnostic_digest,
                sealed_at=self._clock(),
            )
            if self._failure_injector is not None:
                self._failure_injector("after_historical_seal")
        metric_count, metric_digest = self._historical_digests(metrics=True)
        diagnostic_count, diagnostic_digest = self._historical_digests(metrics=False)
        if replay:
            self._verify_all_pages()
        if replay:
            progress = self._progress(phase="COMPLETE")
        else:
            progress = self._progress(
                phase="COMPLETE",
                document_contents=HistoricalRecoveryCounts(
                    created=selection_counts[HistoricalRecoveryEntityKind.DOCUMENT_CONTENT][0],
                    reused=selection_counts[HistoricalRecoveryEntityKind.DOCUMENT_CONTENT][1],
                ),
                raw_records=HistoricalRecoveryCounts(
                    created=selection_counts[HistoricalRecoveryEntityKind.RAW_RECORD][0],
                    reused=selection_counts[HistoricalRecoveryEntityKind.RAW_RECORD][1],
                ),
                observations=HistoricalRecoveryCounts(
                    created=selection_counts[HistoricalRecoveryEntityKind.OBSERVATION][0],
                    reused=selection_counts[HistoricalRecoveryEntityKind.OBSERVATION][1],
                ),
                metric_results=HistoricalRecoveryCounts(
                    created=selection_counts[HistoricalRecoveryEntityKind.METRIC_RESULT][0],
                    reused=selection_counts[HistoricalRecoveryEntityKind.METRIC_RESULT][1],
                ),
                diagnostic_results=HistoricalRecoveryCounts(
                    created=selection_counts[HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT][0],
                    reused=selection_counts[HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT][1],
                ),
            )
        created = {
            "raw": 0 if replay else selection_counts[HistoricalRecoveryEntityKind.RAW_RECORD][0],
            "observations": 0
            if replay
            else selection_counts[HistoricalRecoveryEntityKind.OBSERVATION][0],
            "metrics": 0
            if replay
            else selection_counts[HistoricalRecoveryEntityKind.METRIC_RESULT][0],
            "diagnostics": 0
            if replay
            else selection_counts[HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT][0],
            "documents": 0
            if replay
            else selection_counts[HistoricalRecoveryEntityKind.DOCUMENT_CONTENT][0],
        }
        reused = {
            "raw": (
                self._manifest.totals.raw_records
                if replay
                else selection_counts[HistoricalRecoveryEntityKind.RAW_RECORD][1]
            ),
            "observations": (
                self._manifest.totals.observations
                if replay
                else selection_counts[HistoricalRecoveryEntityKind.OBSERVATION][1]
            ),
            "metrics": (
                self._manifest.totals.metric_results
                if replay
                else selection_counts[HistoricalRecoveryEntityKind.METRIC_RESULT][1]
            ),
            "diagnostics": (
                self._manifest.totals.diagnostic_results
                if replay
                else selection_counts[HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT][1]
            ),
            "documents": (
                self._manifest.totals.document_contents
                if replay
                else selection_counts[HistoricalRecoveryEntityKind.DOCUMENT_CONTENT][1]
            ),
        }
        return HistoricalRecoveryImportSummary(
            selection_id=self._manifest.selection_id,
            selection_digest=self._manifest.digest,
            destination_workspace_id=self._manifest.destination_workspace_id,
            source_workspace_id=self._manifest.source_workspace_id,
            raw_items=importable[HistoricalRecoveryEntityKind.RAW_RECORD],
            observation_items=importable[HistoricalRecoveryEntityKind.OBSERVATION],
            metric_items=importable[HistoricalRecoveryEntityKind.METRIC_RESULT],
            diagnostic_items=importable[HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT],
            document_items=importable[HistoricalRecoveryEntityKind.DOCUMENT_CONTENT],
            created_raw=created["raw"],
            reused_raw=reused["raw"],
            created_observations=created["observations"],
            reused_observations=reused["observations"],
            created_metrics=created["metrics"],
            reused_metrics=reused["metrics"],
            created_diagnostics=created["diagnostics"],
            reused_diagnostics=reused["diagnostics"],
            created_documents=created["documents"],
            reused_documents=reused["documents"],
            metric_digest=metric_digest,
            diagnostic_digest=diagnostic_digest,
            replay=replay,
            source_unchanged=True,
            completed_at=self._clock(),
        )

    def _historical_digests(self, *, metrics: bool) -> tuple[int, str]:
        count, metric_digest, diagnostic_count, diagnostic_digest = (
            self._compact.historical_inventory_digests()
        )
        return (count, metric_digest) if metrics else (diagnostic_count, diagnostic_digest)

    def _verify_all_pages(self) -> None:
        for page in self._manifest.pages:
            items = self._page_items(page)
            self._verify_items(items)

    def _import_page(
        self, kind: HistoricalRecoveryEntityKind, page: HistoricalRecoveryPage
    ) -> None:
        items = self._page_items(page)
        for chunk in _iter_chunks(items, self._page_limit):
            self._import_chunk(kind, chunk)

    def _import_chunk(
        self, kind: HistoricalRecoveryEntityKind, items: Sequence[HistoricalRecoveryItem]
    ) -> None:
        missing = [
            item
            for item in items
            if item.classification is HistoricalRecoveryClassification.IMPORTAR_FALTANTE
        ]
        present = [
            item
            for item in items
            if item.classification is HistoricalRecoveryClassification.YA_PRESENTE
        ]
        if present:
            self._verify_items(present)
            if kind in (
                HistoricalRecoveryEntityKind.METRIC_RESULT,
                HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT,
            ):
                self._admit_pit(kind, present)
        if not missing:
            return
        self._verify_source_items(kind, missing)
        if kind is HistoricalRecoveryEntityKind.DOCUMENT_CONTENT:
            for item in missing:
                checksum = item.document_sha256
                assert checksum is not None
                source_path = (
                    self._source.paths.documents_dir
                    / "sha256"
                    / checksum[:2]
                    / checksum[2:4]
                    / checksum
                )
                self._destination.documents.copy_verified_file(source_path, checksum)
                self._destination.documents.verify(checksum, size_bytes=item.byte_length)
            return
        identifiers = tuple(item.entity_id for item in missing)
        if kind is HistoricalRecoveryEntityKind.RAW_RECORD:
            models = self._source.raw_records.get_many(identifiers)
            self._admit_raw_delta(models)
            self._destination.raw_records.save_many(models.values())
            self._verify_destination_raw(missing)
        elif kind is HistoricalRecoveryEntityKind.OBSERVATION:
            models = self._source.observations.get_many(identifiers)
            self._destination.observations.save_many(models.values())
            self._verify_destination_observations(missing)
        elif kind is HistoricalRecoveryEntityKind.METRIC_RESULT:
            self._admit_pit(kind, missing)
            models = self._source.metric_results.get_many(identifiers)
            self._compact.save_metrics(models.values(), origin="HISTORICAL")
            self._verify_destination_metrics(missing)
        elif kind is HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT:
            self._admit_pit(kind, missing)
            models = self._source.diagnostics.get_many(identifiers)
            self._compact.save_diagnostics(models.values(), origin="HISTORICAL")
            self._verify_destination_diagnostics(missing)
        else:
            raise AssertionError("unreachable entity kind")

    def _admit_raw_delta(self, models) -> None:
        if self._admission is None:
            return
        managers = set(self._admission.manager_ciks)
        for identifier, model in models.items():
            if model.source.source_id != _13F_SOURCE:
                raise HistoricalRecoveryError(
                    f"admission delta raw {identifier} is outside the 13F source"
                )
            position = model.payload.get("position", {}) if model.payload else {}
            revision = position.get("information_table_revision", {})
            manager = str(revision.get("filer_cik", ""))
            if manager not in managers:
                raise HistoricalRecoveryError(
                    f"admission delta raw {identifier} manager {manager} is outside the frozen set"
                )

    def _admit_pit(
        self, kind: HistoricalRecoveryEntityKind, items: Sequence[HistoricalRecoveryItem]
    ) -> None:
        if kind is HistoricalRecoveryEntityKind.METRIC_RESULT:
            models = self._source.metric_results.get_many(tuple(item.entity_id for item in items))
            observation_ids = {
                identifier
                for model in models.values()
                for identifier in model.input_observation_ids
            }
            observations = self._destination.observations.get_many(observation_ids)
            for model in models.values():
                for identifier in model.input_observation_ids:
                    observation = observations.get(identifier)
                    if (
                        observation is None
                        or observation.asset_id != model.asset_id
                        or observation.available_at > model.available_at
                    ):
                        raise HistoricalRecoveryError(
                            "historical recovery PIT admission rejected a metric input observation"
                        )
                input_metric_ids = tuple(model.input_metric_result_ids)
                existing = self._destination.metric_results.get_existing(input_metric_ids)
                for identifier in input_metric_ids:
                    source = existing.get(identifier)
                    if source is None:
                        try:
                            source = self._source.metric_results.get(identifier)
                        except Exception as error:
                            raise HistoricalRecoveryError(
                                "historical recovery PIT admission found no metric input"
                            ) from error
                    if (
                        source.asset_id != model.asset_id
                        or source.available_at > model.available_at
                    ):
                        raise HistoricalRecoveryError(
                            "historical recovery PIT admission rejected a metric input"
                        )
        elif kind is HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT:
            models = self._source.diagnostics.get_many(tuple(item.entity_id for item in items))
            for model in models.values():
                metric_ids = {
                    identifier
                    for component in model.components
                    for identifier in component.metric_result_ids
                } | {evidence.metric_result_id for evidence in model.evidence}
                existing = self._destination.metric_results.get_existing(tuple(metric_ids))
                if set(existing) != metric_ids:
                    raise HistoricalRecoveryError(
                        "historical recovery PIT admission found a missing diagnostic metric"
                    )
                for identifier in metric_ids:
                    metric = existing[identifier]
                    if (
                        metric.asset_id != model.asset_id
                        or metric.available_at > model.available_at
                    ):
                        raise HistoricalRecoveryError(
                            "historical recovery PIT admission rejected a diagnostic metric"
                        )

    def _selection_counts(
        self,
    ) -> dict[HistoricalRecoveryEntityKind, tuple[int, int]]:
        counts = {kind: [0, 0] for kind in HistoricalRecoveryEntityKind}
        for page in self._manifest.pages:
            for item in self._page_items(page):
                if item.classification is HistoricalRecoveryClassification.IMPORTAR_FALTANTE:
                    counts[item.entity_kind][0] += 1
                elif item.classification is HistoricalRecoveryClassification.YA_PRESENTE:
                    counts[item.entity_kind][1] += 1
        return {kind: (values[0], values[1]) for kind, values in counts.items()}

    def _present_historical_count(self, kind: HistoricalRecoveryEntityKind) -> int:
        if kind not in (
            HistoricalRecoveryEntityKind.METRIC_RESULT,
            HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT,
        ):
            return 0
        identifiers = [
            item.entity_id
            for page in self._manifest.pages
            if page.entity_kind is kind
            for item in self._page_items(page)
            if item.classification is HistoricalRecoveryClassification.YA_PRESENTE
        ]
        table, key = (
            (_METRIC_TABLE, "result_id")
            if kind is HistoricalRecoveryEntityKind.METRIC_RESULT
            else (_DIAGNOSTIC_TABLE, "diagnostic_id")
        )
        count = 0
        for chunk in _iter_chunks(identifiers, _PAGE_SIZE):
            count += int(
                self._compact._connection.execute(
                    f"SELECT count(*) FROM {table} WHERE {key} IN "
                    f"({_placeholders(len(chunk))}) AND origin='HISTORICAL'",
                    [str(identifier) for identifier in chunk],
                ).fetchone()[0]
            )
        return count

    def _verify_source_items(
        self, kind: HistoricalRecoveryEntityKind, items: Sequence[HistoricalRecoveryItem]
    ) -> None:
        if kind is HistoricalRecoveryEntityKind.DOCUMENT_CONTENT:
            return
        table, key = {
            HistoricalRecoveryEntityKind.RAW_RECORD: ("raw_record_index", "record_id"),
            HistoricalRecoveryEntityKind.OBSERVATION: (
                "normalized_observations",
                "observation_id",
            ),
            HistoricalRecoveryEntityKind.METRIC_RESULT: ("metric_results", "result_id"),
            HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT: (
                "diagnostic_results",
                "diagnostic_id",
            ),
        }[kind]
        rows = self._source.store.connection.execute(
            f"SELECT {key}, sha256(document_json), octet_length(encode(document_json)) "
            f"FROM {table} WHERE {key} IN ({_placeholders(len(items))})",
            [str(item.entity_id) for item in items],
        ).fetchall()
        found = {str(row[0]): (str(row[1]), int(row[2])) for row in rows}
        if len(found) != len(items):
            raise HistoricalRecoveryError("historical recovery source page is incomplete")
        for item in items:
            digest, length = found[str(item.entity_id)]
            if digest != item.content_sha256 or length != item.byte_length:
                raise HistoricalRecoveryError("historical recovery source content changed")

    def _verify_items(self, items: Sequence[HistoricalRecoveryItem]) -> None:
        by_kind: dict[HistoricalRecoveryEntityKind, list[HistoricalRecoveryItem]] = defaultdict(
            list
        )
        for item in items:
            if item.classification is HistoricalRecoveryClassification.ARCHIVE_ONLY:
                continue
            by_kind[item.entity_kind].append(item)
        if not by_kind:
            return
        if by_kind.get(HistoricalRecoveryEntityKind.DOCUMENT_CONTENT):
            for item in by_kind[HistoricalRecoveryEntityKind.DOCUMENT_CONTENT]:
                checksum = item.document_sha256
                assert checksum is not None
                self._destination.documents.verify(checksum, size_bytes=item.byte_length)
        if by_kind.get(HistoricalRecoveryEntityKind.RAW_RECORD):
            self._verify_destination_raw(by_kind[HistoricalRecoveryEntityKind.RAW_RECORD])
        if by_kind.get(HistoricalRecoveryEntityKind.OBSERVATION):
            self._verify_destination_observations(by_kind[HistoricalRecoveryEntityKind.OBSERVATION])
        if by_kind.get(HistoricalRecoveryEntityKind.METRIC_RESULT):
            self._verify_destination_metrics(by_kind[HistoricalRecoveryEntityKind.METRIC_RESULT])
        if by_kind.get(HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT):
            self._verify_destination_diagnostics(
                by_kind[HistoricalRecoveryEntityKind.DIAGNOSTIC_RESULT]
            )

    def _verify_destination_raw(self, items: Sequence[HistoricalRecoveryItem]) -> None:
        identifiers = tuple(item.entity_id for item in items)
        models = self._destination.raw_records.get_many(identifiers)
        sources = self._source.raw_records.get_many(identifiers)
        if len(models) != len(items) or len(sources) != len(items):
            raise HistoricalRecoveryError("historical recovery destination raw page is incomplete")
        for item in items:
            model = models[item.entity_id]
            source = sources[item.entity_id]
            if item.classification is HistoricalRecoveryClassification.YA_PRESENTE:
                if (
                    model.source.checksum_sha256 != source.source.checksum_sha256
                    or model.source.record_key != source.source.record_key
                    or model.schema_version != source.schema_version
                    or model.source.source_id != source.source.source_id
                    or (model.asset_id or None) != (source.asset_id or None)
                ):
                    raise HistoricalRecoveryError(
                        "historical recovery destination raw source identity differs"
                    )
            elif sha256_hex(canonical_json_bytes(model)) != sha256_hex(
                canonical_json_bytes(source)
            ):
                raise HistoricalRecoveryError("historical recovery destination raw content differs")

    def _verify_destination_observations(self, items: Sequence[HistoricalRecoveryItem]) -> None:
        identifiers = tuple(item.entity_id for item in items)
        models = self._destination.observations.get_many(identifiers)
        sources = self._source.observations.get_many(identifiers)
        if len(models) != len(items) or len(sources) != len(items):
            raise HistoricalRecoveryError(
                "historical recovery destination observation page is incomplete"
            )
        for item in items:
            model = models[item.entity_id]
            source = sources[item.entity_id]
            if item.classification is HistoricalRecoveryClassification.YA_PRESENTE:
                if (
                    model.raw_record_id != source.raw_record_id
                    or model.asset_id != source.asset_id
                    or model.field_name != source.field_name
                    or model.frequency != source.frequency
                    or model.observed_at != source.observed_at
                    or model.period_end != source.period_end
                    or model.value != source.value
                    or model.unit != source.unit
                    or (
                        model.source.record_key != source.source.record_key
                        and _stable_record_key(model.source.record_key)
                        != _stable_record_key(source.source.record_key)
                    )
                    or model.source.source_id != source.source.source_id
                ):
                    raise HistoricalRecoveryError(
                        "historical recovery destination observation identity differs"
                    )
            elif sha256_hex(canonical_json_bytes(model)) != sha256_hex(
                canonical_json_bytes(source)
            ):
                raise HistoricalRecoveryError(
                    "historical recovery destination observation content differs"
                )

    def _verify_destination_metrics(self, items: Sequence[HistoricalRecoveryItem]) -> None:
        identifiers = tuple(item.entity_id for item in items)
        models = self._compact.get_metrics(identifiers)
        sources = self._source.metric_results.get_many(identifiers)
        if len(models) != len(items) or len(sources) != len(items):
            raise HistoricalRecoveryError(
                "historical recovery destination metric page is incomplete"
            )
        for item in items:
            model = models[item.entity_id]
            source = sources[item.entity_id]
            if model.model_dump(exclude={"computed_at"}) != source.model_dump(
                exclude={"computed_at"}
            ):
                raise HistoricalRecoveryError(
                    "historical recovery destination metric content differs"
                )

    def _verify_destination_diagnostics(self, items: Sequence[HistoricalRecoveryItem]) -> None:
        identifiers = tuple(item.entity_id for item in items)
        models = self._compact.get_diagnostics(identifiers)
        sources = self._source.diagnostics.get_many(identifiers)
        if len(models) != len(items) or len(sources) != len(items):
            raise HistoricalRecoveryError(
                "historical recovery destination diagnostic page is incomplete"
            )
        for item in items:
            model = models[item.entity_id]
            source = sources[item.entity_id]
            if model.model_dump(exclude={"computed_at"}) != source.model_dump(
                exclude={"computed_at"}
            ):
                raise HistoricalRecoveryError(
                    "historical recovery destination diagnostic content differs"
                )


class HistoricalRecoveryAdmission(ContractModel):
    """Frozen admission control for the authorized 13F raw delta."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["historical-recovery-admission-v1"] = (
        HISTORICAL_RECOVERY_ADMISSION_SCHEMA
    )
    admission_id: UUID
    selection_id: UUID
    selection_digest: str = Field(pattern=_SHA256_PATTERN)
    source_workspace_id: UUID
    destination_workspace_id: UUID
    policy_version: NonEmptyStr
    manager_ciks: tuple[str, ...]
    manager_set_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorized_delta_count: int = Field(gt=0)
    authorized_delta_digest: str = Field(pattern=_SHA256_PATTERN)
    created_at: UTCDateTime

    @model_validator(mode="after")
    def _validate_admission(self) -> HistoricalRecoveryAdmission:
        if self.manager_ciks != tuple(sorted(set(self.manager_ciks))):
            raise ValueError("admission manager CIKs must be sorted and unique")
        if any(len(cik) != 10 or not cik.isdigit() for cik in self.manager_ciks):
            raise ValueError("admission manager CIKs must be ten-digit strings")
        if _manager_set_digest(self.manager_ciks) != self.manager_set_sha256:
            raise ValueError("admission manager-set digest differs")
        return self


def _manager_set_digest(manager_ciks: Sequence[str]) -> str:
    return hashlib.sha256(
        json.dumps(list(manager_ciks), ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _raw_delta_digest(manifest: HistoricalRecoveryManifest, root: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for page in manifest.pages:
        if page.entity_kind is not HistoricalRecoveryEntityKind.RAW_RECORD:
            continue
        path = root.resolve() / page.relative_path
        try:
            path.relative_to(root.resolve())
        except ValueError as error:
            raise HistoricalRecoveryError("admission page escapes its root") from error
        if path.is_symlink() or not path.is_file():
            raise HistoricalRecoveryError("admission page is missing or unsafe")
        data = path.read_bytes()
        if len(data) != page.byte_length or hashlib.sha256(data).hexdigest() != page.sha256:
            raise HistoricalRecoveryError("admission page digest differs")
        for line in data.splitlines():
            if not line:
                continue
            item = HistoricalRecoveryItem.model_validate_json(line)
            if item.classification is not HistoricalRecoveryClassification.IMPORTAR_FALTANTE:
                continue
            digest.update(f"{item.entity_id}:{item.content_sha256}\n".encode("ascii"))
            count += 1
    return count, digest.hexdigest()


def create_historical_recovery_admission(
    *,
    manifest: HistoricalRecoveryManifest,
    manifest_root: Path,
    source: LocalStorage,
    clock=lambda: datetime.now(UTC),
) -> HistoricalRecoveryAdmission:
    """Freeze the authorized raw delta and source manager set without rewriting pages."""
    if source.paths.format_version != 1:
        raise HistoricalRecoveryError("admission source must be format 1")
    if source.paths.workspace_id != manifest.source_workspace_id:
        raise HistoricalRecoveryError("admission source does not match the selection")
    verify_historical_recovery_selection(manifest, manifest_root)
    manager_ciks = tuple(sorted(set(_manager_ciks(source))))
    manager_set_sha256 = _manager_set_digest(manager_ciks)
    delta_count, delta_digest = _raw_delta_digest(manifest, manifest_root)
    if delta_count != manifest.totals.import_required:
        raise HistoricalRecoveryError("admission delta is not the full raw remainder")
    admission_id = uuid5(
        NAMESPACE_URL,
        f"historical-recovery-admission-v1:{manifest.digest}:{manager_set_sha256}:{delta_digest}",
    )
    return HistoricalRecoveryAdmission(
        admission_id=admission_id,
        selection_id=manifest.selection_id,
        selection_digest=manifest.digest,
        source_workspace_id=manifest.source_workspace_id,
        destination_workspace_id=manifest.destination_workspace_id,
        policy_version=manifest.policy_version,
        manager_ciks=manager_ciks,
        manager_set_sha256=manager_set_sha256,
        authorized_delta_count=delta_count,
        authorized_delta_digest=delta_digest,
        created_at=clock(),
    )


def load_historical_recovery_admission(path: Path) -> HistoricalRecoveryAdmission:
    try:
        return HistoricalRecoveryAdmission.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise HistoricalRecoveryError("historical recovery admission is unreadable") from error


def validate_historical_recovery_admission(
    admission: HistoricalRecoveryAdmission,
    manifest: HistoricalRecoveryManifest,
    manifest_root: Path,
) -> None:
    if (
        admission.selection_id != manifest.selection_id
        or admission.selection_digest != manifest.digest
        or admission.source_workspace_id != manifest.source_workspace_id
        or admission.destination_workspace_id != manifest.destination_workspace_id
        or admission.policy_version != manifest.policy_version
    ):
        raise HistoricalRecoveryError("historical recovery admission binding differs")
    delta_count, delta_digest = _raw_delta_digest(manifest, manifest_root)
    if (
        admission.authorized_delta_count != delta_count
        or admission.authorized_delta_digest != delta_digest
    ):
        raise HistoricalRecoveryError("historical recovery admission delta differs")


def write_historical_recovery_admission(path: Path, admission: HistoricalRecoveryAdmission) -> None:
    if path.exists() or path.is_symlink():
        raise HistoricalRecoveryError("historical recovery admission output must be new")
    _write_json(path, admission.model_dump(mode="json"))


class HistoricalRecoveryRepairManifest(ContractModel):
    """External human authorization for one exact PIT repair of block-created rows."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["data-chassis-48-historical-repair-manifest-v1"] = (
        HISTORICAL_RECOVERY_REPAIR_MANIFEST_SCHEMA
    )
    selection_id: UUID
    selection_digest: str = Field(pattern=_SHA256_PATTERN)
    invalid_seal: HistoricalAnalyticalSeal
    invalid_metric_count: int = Field(ge=0)
    invalid_metric_ids: tuple[UUID, ...]
    invalid_diagnostic_count: int = Field(ge=0)
    invalid_diagnostic_ids: tuple[UUID, ...]
    valid_projected_metric_count: int = Field(ge=0)
    valid_projected_metric_digest: str = Field(pattern=_SHA256_PATTERN)
    valid_projected_diagnostic_count: int = Field(ge=0)
    valid_projected_diagnostic_digest: str = Field(pattern=_SHA256_PATTERN)
    cause: NonEmptyStr
    required_authority: NonEmptyStr
    valid_scope: NonEmptyStr

    @model_validator(mode="after")
    def _validate_repair_manifest(self) -> HistoricalRecoveryRepairManifest:
        if self.invalid_metric_count != len(self.invalid_metric_ids) or len(
            self.invalid_metric_ids
        ) != len(set(self.invalid_metric_ids)):
            raise ValueError("repair metric count or uniqueness differs")
        if self.invalid_diagnostic_count != len(self.invalid_diagnostic_ids) or len(
            self.invalid_diagnostic_ids
        ) != len(set(self.invalid_diagnostic_ids)):
            raise ValueError("repair diagnostic count or uniqueness differs")
        if set(self.invalid_metric_ids) & set(self.invalid_diagnostic_ids):
            raise ValueError("repair metric and diagnostic IDs overlap")
        return self


class HistoricalRecoveryRepairReceipt(ContractModel):
    """Durable idempotent receipt for one exact PIT repair."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["historical-recovery-repair-receipt-v1"] = (
        HISTORICAL_RECOVERY_REPAIR_RECEIPT_SCHEMA
    )
    repair_id: UUID
    destination_workspace_id: UUID
    selection_id: UUID
    selection_digest: str = Field(pattern=_SHA256_PATTERN)
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    before_inventory_digest: str = Field(pattern=_SHA256_PATTERN)
    after_inventory_digest: str = Field(pattern=_SHA256_PATTERN)
    removed_metric_ids: tuple[UUID, ...]
    removed_diagnostic_ids: tuple[UUID, ...]
    before_seal: HistoricalAnalyticalSeal
    after_seal: HistoricalAnalyticalSeal
    status: Literal["COMPLETE"] = "COMPLETE"
    started_at: UTCDateTime
    completed_at: UTCDateTime

    @model_validator(mode="after")
    def _validate_repair_receipt(self) -> HistoricalRecoveryRepairReceipt:
        if self.removed_metric_ids != tuple(sorted(set(self.removed_metric_ids), key=str)):
            raise ValueError("repair metric IDs must be sorted and unique")
        if self.removed_diagnostic_ids != tuple(sorted(set(self.removed_diagnostic_ids), key=str)):
            raise ValueError("repair diagnostic IDs must be sorted and unique")
        return self


def _inventory_digest(seal: HistoricalAnalyticalSeal) -> str:
    payload = {
        "source_fingerprint": seal.source_fingerprint,
        "metric_count": seal.metric_count,
        "metric_digest": seal.metric_digest,
        "diagnostic_count": seal.diagnostic_count,
        "diagnostic_digest": seal.diagnostic_digest,
    }
    return hashlib.sha256(
        json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _repair_id(manifest_sha256: str, selection_digest: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"historical-recovery-repair-v1:{manifest_sha256}:{selection_digest}",
    )


def _ids_digest(identifiers: Sequence[UUID]) -> str:
    return hashlib.sha256(
        "\n".join(str(identifier) for identifier in sorted(identifiers, key=str)).encode("ascii")
    ).hexdigest()


class HistoricalRecoveryRepairService:
    """Validate and execute one exact, human-authorized PIT repair."""

    def __init__(
        self,
        *,
        source: LocalStorage,
        destination: LocalStorage,
        selection: HistoricalRecoveryManifest,
        selection_root: Path,
        repair_manifest_path: Path,
        expected_manifest_sha256: str,
        intent_path: Path,
        receipt_path: Path,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        if source.read_only is not True:
            raise HistoricalRecoveryError("historical repair source must be read-only")
        if destination.read_only:
            raise HistoricalRecoveryError("historical repair destination must be writable")
        if source.paths.workspace_id != selection.source_workspace_id:
            raise HistoricalRecoveryError("repair source does not match the selection")
        if destination.paths.workspace_id != selection.destination_workspace_id:
            raise HistoricalRecoveryError("repair destination does not match the selection")
        if destination.paths.format_version != 2:
            raise HistoricalRecoveryError("historical repair destination must be v2")
        self._source = source
        self._destination = destination
        self._selection = selection
        self._selection_root = selection_root.resolve()
        self._repair_manifest_path = repair_manifest_path
        self._expected_manifest_sha256 = expected_manifest_sha256
        self._intent_path = intent_path
        self._receipt_path = receipt_path
        self._clock = clock
        self._compact = CompactAnalyticalStore(destination.store.connection)

    def _load_repair_manifest(self) -> tuple[bytes, HistoricalRecoveryRepairManifest]:
        try:
            raw = self._repair_manifest_path.read_bytes()
        except OSError as error:
            raise HistoricalRecoveryError("historical repair manifest is unreadable") from error
        digest = hashlib.sha256(raw).hexdigest()
        if digest != self._expected_manifest_sha256:
            raise HistoricalRecoveryError("historical repair manifest SHA-256 differs")
        try:
            manifest = HistoricalRecoveryRepairManifest.model_validate_json(raw)
        except ValueError as error:
            raise HistoricalRecoveryError("historical repair manifest is malformed") from error
        if (
            manifest.selection_id != self._selection.selection_id
            or manifest.selection_digest != self._selection.digest
        ):
            raise HistoricalRecoveryError("historical repair selection binding differs")
        return raw, manifest

    def _selection_items(
        self, metric_ids: set[UUID], diagnostic_ids: set[UUID]
    ) -> dict[UUID, HistoricalRecoveryItem]:
        wanted = metric_ids | diagnostic_ids
        found: dict[UUID, HistoricalRecoveryItem] = {}
        for page in self._selection.pages:
            path = self._selection_root / page.relative_path
            try:
                path.relative_to(self._selection_root)
            except ValueError as error:
                raise HistoricalRecoveryError("repair selection page escapes its root") from error
            if path.is_symlink() or not path.is_file():
                raise HistoricalRecoveryError("repair selection page is missing or unsafe")
            data = path.read_bytes()
            if len(data) != page.byte_length or hashlib.sha256(data).hexdigest() != page.sha256:
                raise HistoricalRecoveryError("repair selection page digest differs")
            for line in data.splitlines():
                if not line:
                    continue
                item = HistoricalRecoveryItem.model_validate_json(line)
                if item.entity_id in wanted:
                    if item.entity_id in found:
                        raise HistoricalRecoveryError("repair selection item is duplicated")
                    found[item.entity_id] = item
        if set(found) != wanted:
            raise HistoricalRecoveryError("repair selection does not contain every candidate")
        for item in found.values():
            if item.classification is not HistoricalRecoveryClassification.IMPORTAR_FALTANTE:
                raise HistoricalRecoveryError("repair candidate was not an imported historical row")
        return found

    def _repository_models(self, repository, identifiers: Sequence[UUID]) -> dict[UUID, object]:
        output: dict[UUID, object] = {}
        for chunk in _iter_chunks(identifiers, _PAGE_SIZE):
            output.update(repository.get_many(chunk))
        return output

    def _compact_models(self, identifiers: Sequence[UUID]) -> dict[UUID, object]:
        output: dict[UUID, object] = {}
        for chunk in _iter_chunks(identifiers, _PAGE_SIZE):
            output.update(self._compact.get_metrics(chunk))
        return output

    def _compact_diagnostics(self, identifiers: Sequence[UUID]) -> dict[UUID, object]:
        output: dict[UUID, object] = {}
        for chunk in _iter_chunks(identifiers, _PAGE_SIZE):
            output.update(self._compact.get_diagnostics(chunk))
        return output

    def _source_document_digests(
        self, table: str, key: str, identifiers: Sequence[UUID]
    ) -> dict[UUID, str]:
        output: dict[UUID, str] = {}
        connection = self._source.store.connection
        for chunk in _iter_chunks(identifiers, _PAGE_SIZE):
            rows = connection.execute(
                f"SELECT {key}, sha256(document_json) FROM {table} "
                f"WHERE {key} IN ({_placeholders(len(chunk))})",
                [str(identifier) for identifier in chunk],
            ).fetchall()
            if len(rows) != len(chunk):
                raise HistoricalRecoveryError("historical repair source row is missing")
            output.update({UUID(str(row[0])): str(row[1]) for row in rows})
        return output

    def _destination_origins(
        self, table: str, key: str, identifiers: Sequence[UUID]
    ) -> dict[UUID, str]:
        output: dict[UUID, str] = {}
        for chunk in _iter_chunks(identifiers, _PAGE_SIZE):
            rows = self._compact._connection.execute(
                f"SELECT {key}, origin FROM {table} WHERE {key} IN ({_placeholders(len(chunk))})",
                [str(identifier) for identifier in chunk],
            ).fetchall()
            output.update({UUID(str(row[0])): str(row[1]) for row in rows})
        return output

    def _recompute_invalid_sets(self) -> tuple[set[UUID], set[UUID]]:
        compact = self._compact
        compact._build_relationship_metadata(_METRIC_TABLE)
        invalid = compact._invalid_reference_metadata("p", "metric-input-observation-v1")
        metric_rows = compact._relationship_sql(
            f"SELECT p.result_id FROM {_METRIC_TABLE} p "
            f"LEFT JOIN _workspace_v2_reference_sequences q "
            f"ON q.sequence_id=p.observation_sequence_id "
            f"WHERE p.origin='HISTORICAL' AND {invalid}"
        ).fetchall()
        metrics = {UUID(str(row[0])) for row in metric_rows}
        if not metrics:
            raise HistoricalRecoveryError("historical repair has no PIT-invalid metrics")
        metric_placeholders = _placeholders(len(metrics))
        metric_text = [str(identifier) for identifier in metrics]
        components = compact._relationship_sql(
            f"SELECT DISTINCT c.diagnostic_id FROM {_COMPONENT_TABLE} c "
            f"JOIN {_SEQUENCE_SEGMENT_TABLE} l ON l.sequence_id=c.metric_sequence_id "
            f"JOIN {_SEGMENT_MEMBER_TABLE} m ON m.segment_id=l.segment_id "
            f"WHERE m.member_id IN ({metric_placeholders})",
            metric_text,
        ).fetchall()
        evidence = compact._relationship_sql(
            f"SELECT DISTINCT diagnostic_id FROM {_EVIDENCE_TABLE} "
            f"WHERE metric_result_id IN ({metric_placeholders})",
            metric_text,
        ).fetchall()
        diagnostics = {UUID(str(row[0])) for row in (*components, *evidence)}
        compact._relationship_sql("DROP TABLE IF EXISTS _workspace_v2_reference_sequences")
        compact._relationship_sql("DROP TABLE IF EXISTS _workspace_v2_reference_segments")
        return metrics, diagnostics

    def _auxiliary_references(
        self, metric_ids: Sequence[UUID], diagnostic_ids: Sequence[UUID]
    ) -> tuple[str | None, int]:
        skip = {
            _METRIC_TABLE,
            _DIAGNOSTIC_TABLE,
            _COMPONENT_TABLE,
            _EVIDENCE_TABLE,
            "workspace_v2_historical_seal",
            "workspace_v2_historical_seal_history",
        }
        columns = self._destination.store.connection.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema='main' AND column_name IN "
            "('result_id','metric_result_id','diagnostic_id') ORDER BY 1,2"
        ).fetchall()
        all_ids = [str(identifier) for identifier in (*metric_ids, *diagnostic_ids)]
        if not all_ids:
            return None, 0
        placeholders = _placeholders(len(all_ids))
        for table, column in columns:
            table_name = str(table)
            if table_name in skip:
                continue
            rows = self._destination.store.connection.execute(
                f'SELECT count(*) FROM "{table_name}" '
                f'WHERE CAST("{str(column)}" AS VARCHAR) IN ({placeholders})',
                all_ids,
            ).fetchone()
            count = int(rows[0]) if rows is not None else 0
            if count:
                return f"{table_name}.{column}", count
        return None, 0

    def _historical_inventory(self, excluded_metrics: set[UUID]) -> tuple[int, str]:
        digest = hashlib.sha256(b"").hexdigest()
        count = 0
        cursor_at = None
        cursor_id = None
        while True:
            identifiers = self._compact.list_metric_ids_page(
                limit=_PAGE_SIZE,
                after_available_at=cursor_at,
                after_result_id=cursor_id,
                origin="HISTORICAL",
            )
            if not identifiers:
                return count, digest
            models = self._compact.get_metrics(identifiers)
            for identifier in identifiers:
                if identifier in excluded_metrics:
                    continue
                checksum = sha256_hex(canonical_json_bytes(models[identifier]))
                digest = hashlib.sha256(f"{digest}:{checksum}".encode("ascii")).hexdigest()
                count += 1
            last = models[identifiers[-1]]
            cursor_at = last.available_at
            cursor_id = last.result_id

    def _historical_diagnostics(self, excluded: set[UUID]) -> tuple[int, str]:
        digest = hashlib.sha256(b"").hexdigest()
        count = 0
        cursor_at = None
        cursor_id = None
        while True:
            identifiers = self._compact.list_diagnostic_ids_page(
                limit=_PAGE_SIZE,
                after_available_at=cursor_at,
                after_diagnostic_id=cursor_id,
                origin="HISTORICAL",
            )
            if not identifiers:
                return count, digest
            models = self._compact.get_diagnostics(identifiers)
            for identifier in identifiers:
                if identifier in excluded:
                    continue
                checksum = sha256_hex(canonical_json_bytes(models[identifier]))
                digest = hashlib.sha256(f"{digest}:{checksum}".encode("ascii")).hexdigest()
                count += 1
            last = models[identifiers[-1]]
            cursor_at = last.available_at
            cursor_id = last.diagnostic_id

    def _write_new_or_equal(self, path: Path, document: object) -> None:
        encoded = _canonical_bytes(document) + b"\n"
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file() or path.read_bytes() != encoded:
                raise HistoricalRecoveryError("historical repair control already differs")
            return
        _write_json(path, document)

    def _complete_poststate(
        self,
        repair: HistoricalRecoveryRepairManifest,
        manifest_sha: str,
        metric_ids: set[UUID],
        diagnostic_ids: set[UUID],
        started_at: datetime,
    ) -> HistoricalRecoveryRepairReceipt | None:
        current_seal = self._compact.historical_seal()
        if self._receipt_path.exists() or self._receipt_path.is_symlink():
            if self._receipt_path.is_symlink() or not self._receipt_path.is_file():
                raise HistoricalRecoveryError("historical repair receipt is unsafe")
            try:
                receipt = HistoricalRecoveryRepairReceipt.model_validate_json(
                    self._receipt_path.read_bytes()
                )
            except (OSError, ValueError) as error:
                raise HistoricalRecoveryError("historical repair receipt is malformed") from error
            if (
                receipt.manifest_sha256 != manifest_sha
                or receipt.selection_id != self._selection.selection_id
                or receipt.selection_digest != self._selection.digest
                or receipt.destination_workspace_id != self._selection.destination_workspace_id
                or receipt.before_seal != repair.invalid_seal
                or current_seal != receipt.after_seal
            ):
                raise HistoricalRecoveryError("historical repair receipt binding differs")
            after_metrics = self._historical_inventory(metric_ids)
            after_diagnostics = self._historical_diagnostics(diagnostic_ids)
            if after_metrics != (
                current_seal.metric_count,
                current_seal.metric_digest,
            ) or after_diagnostics != (
                current_seal.diagnostic_count,
                current_seal.diagnostic_digest,
            ):
                raise HistoricalRecoveryError("historical repair receipt inventory differs")
            if (
                self._compact._connection.execute(
                    f"SELECT count(*) FROM {_METRIC_TABLE} "
                    f"WHERE result_id IN ({_placeholders(len(metric_ids))})",
                    [str(identifier) for identifier in metric_ids],
                ).fetchone()[0]
                or self._compact._connection.execute(
                    f"SELECT count(*) FROM {_DIAGNOSTIC_TABLE} "
                    f"WHERE diagnostic_id IN ({_placeholders(len(diagnostic_ids))})",
                    [str(identifier) for identifier in diagnostic_ids],
                ).fetchone()[0]
            ):
                raise HistoricalRecoveryError("historical repair poststate is mixed")
            return receipt
        if (
            current_seal is None
            or current_seal.metric_count != repair.valid_projected_metric_count
            or current_seal.metric_digest != repair.valid_projected_metric_digest
            or current_seal.diagnostic_count != repair.valid_projected_diagnostic_count
            or current_seal.diagnostic_digest != repair.valid_projected_diagnostic_digest
            or current_seal.source_fingerprint != repair.invalid_seal.source_fingerprint
        ):
            return None
        after_metrics, after_metric_digest = self._historical_inventory(metric_ids)
        after_diagnostics, after_diagnostic_digest = self._historical_diagnostics(diagnostic_ids)
        if (
            after_metrics != current_seal.metric_count
            or after_metric_digest != current_seal.metric_digest
            or after_diagnostics != current_seal.diagnostic_count
            or after_diagnostic_digest != current_seal.diagnostic_digest
        ):
            raise HistoricalRecoveryError("historical repair poststate inventory differs")
        if (
            self._compact._connection.execute(
                f"SELECT count(*) FROM {_METRIC_TABLE} "
                f"WHERE result_id IN ({_placeholders(len(metric_ids))})",
                [str(identifier) for identifier in metric_ids],
            ).fetchone()[0]
            or self._compact._connection.execute(
                f"SELECT count(*) FROM {_DIAGNOSTIC_TABLE} "
                f"WHERE diagnostic_id IN ({_placeholders(len(diagnostic_ids))})",
                [str(identifier) for identifier in diagnostic_ids],
            ).fetchone()[0]
        ):
            raise HistoricalRecoveryError("historical repair poststate is mixed")
        receipt = HistoricalRecoveryRepairReceipt(
            repair_id=_repair_id(manifest_sha, self._selection.digest),
            destination_workspace_id=self._selection.destination_workspace_id,
            selection_id=self._selection.selection_id,
            selection_digest=self._selection.digest,
            manifest_sha256=manifest_sha,
            before_inventory_digest=_inventory_digest(repair.invalid_seal),
            after_inventory_digest=_inventory_digest(current_seal),
            removed_metric_ids=tuple(sorted(metric_ids, key=str)),
            removed_diagnostic_ids=tuple(sorted(diagnostic_ids, key=str)),
            before_seal=repair.invalid_seal,
            after_seal=current_seal,
            started_at=started_at,
            completed_at=self._clock(),
        )
        self._write_new_or_equal(self._receipt_path, receipt.model_dump(mode="json"))
        return receipt

    def run(self) -> HistoricalRecoveryRepairReceipt:
        started_at = self._clock()
        raw, repair = self._load_repair_manifest()
        manifest_sha = hashlib.sha256(raw).hexdigest()
        metric_ids = set(repair.invalid_metric_ids)
        diagnostic_ids = set(repair.invalid_diagnostic_ids)
        if len(metric_ids) != repair.invalid_metric_count:
            raise HistoricalRecoveryError("historical repair metric set differs")
        if len(diagnostic_ids) != repair.invalid_diagnostic_count:
            raise HistoricalRecoveryError("historical repair diagnostic set differs")
        completed = self._complete_poststate(
            repair, manifest_sha, metric_ids, diagnostic_ids, started_at
        )
        if completed is not None:
            return completed
        items = self._selection_items(metric_ids, diagnostic_ids)
        source_metric_digests = self._source_document_digests(
            "metric_results", "result_id", tuple(metric_ids)
        )
        source_diagnostic_digests = self._source_document_digests(
            "diagnostic_results", "diagnostic_id", tuple(diagnostic_ids)
        )
        for identifier in metric_ids:
            if source_metric_digests[identifier] != items[identifier].content_sha256:
                raise HistoricalRecoveryError("repair metric source content differs")
        for identifier in diagnostic_ids:
            if source_diagnostic_digests[identifier] != items[identifier].content_sha256:
                raise HistoricalRecoveryError("repair diagnostic source content differs")
        metric_origins = self._destination_origins(_METRIC_TABLE, "result_id", tuple(metric_ids))
        diagnostic_origins = self._destination_origins(
            _DIAGNOSTIC_TABLE, "diagnostic_id", tuple(diagnostic_ids)
        )
        if set(metric_origins) != metric_ids or any(
            origin != "HISTORICAL" for origin in metric_origins.values()
        ):
            raise HistoricalRecoveryError("repair metric origin is not HISTORICAL")
        if set(diagnostic_origins) != diagnostic_ids or any(
            origin != "HISTORICAL" for origin in diagnostic_origins.values()
        ):
            raise HistoricalRecoveryError("repair diagnostic origin is not HISTORICAL")
        source_metrics = self._repository_models(self._source.metric_results, tuple(metric_ids))
        destination_metrics = self._compact_models(tuple(metric_ids))
        for identifier in metric_ids:
            if source_metrics[identifier].model_dump(
                exclude={"computed_at"}
            ) != destination_metrics[identifier].model_dump(exclude={"computed_at"}):
                raise HistoricalRecoveryError("repair metric content differs from source")
        source_diagnostics = self._repository_models(
            self._source.diagnostics, tuple(diagnostic_ids)
        )
        destination_diagnostics = self._compact_diagnostics(tuple(diagnostic_ids))
        for identifier in diagnostic_ids:
            if source_diagnostics[identifier].model_dump(
                exclude={"computed_at"}
            ) != destination_diagnostics[identifier].model_dump(exclude={"computed_at"}):
                raise HistoricalRecoveryError("repair diagnostic content differs from source")
        pre_seal = self._compact.historical_seal()
        if pre_seal != repair.invalid_seal:
            raise HistoricalRecoveryError("repair pre-seal differs from the authorization")
        pre_counts = self._compact.historical_inventory_digests()
        if pre_counts != (
            pre_seal.metric_count,
            pre_seal.metric_digest,
            pre_seal.diagnostic_count,
            pre_seal.diagnostic_digest,
        ):
            raise HistoricalRecoveryError("repair pre-inventory differs from the seal")
        invalid_metrics, invalid_diagnostics = self._recompute_invalid_sets()
        if invalid_metrics != metric_ids or invalid_diagnostics != diagnostic_ids:
            raise HistoricalRecoveryError("repair candidate set is not the exact PIT-invalid set")
        after_metric_count, after_metric_digest = self._historical_inventory(metric_ids)
        after_diagnostic_count, after_diagnostic_digest = self._historical_diagnostics(
            diagnostic_ids
        )
        if (
            after_metric_count != repair.valid_projected_metric_count
            or after_metric_digest != repair.valid_projected_metric_digest
            or after_diagnostic_count != repair.valid_projected_diagnostic_count
            or after_diagnostic_digest != repair.valid_projected_diagnostic_digest
        ):
            raise HistoricalRecoveryError("real post-inventory differs from the projection")
        after_preview = HistoricalAnalyticalSeal(
            source_fingerprint=pre_seal.source_fingerprint,
            metric_count=after_metric_count,
            metric_digest=after_metric_digest,
            diagnostic_count=after_diagnostic_count,
            diagnostic_digest=after_diagnostic_digest,
            sealed_at=started_at,
        )
        before_inventory_digest = _inventory_digest(pre_seal)
        after_inventory_digest = _inventory_digest(after_preview)
        reference, reference_count = self._auxiliary_references(
            tuple(metric_ids), tuple(diagnostic_ids)
        )
        if reference is not None:
            raise HistoricalRecoveryError(
                f"historical repair auxiliary reference remains: {reference} ({reference_count})"
            )
        repair_id = _repair_id(manifest_sha, self._selection.digest)
        intent_document = {
            "schema_version": "historical-recovery-repair-intent-v1",
            "repair_id": str(repair_id),
            "destination_workspace_id": str(self._selection.destination_workspace_id),
            "selection_id": str(self._selection.selection_id),
            "selection_digest": self._selection.digest,
            "manifest_sha256": manifest_sha,
            "before_inventory_digest": before_inventory_digest,
            "after_inventory_digest": after_inventory_digest,
            "before_seal": pre_seal.model_dump(mode="json"),
            "after_seal_preview": after_preview.model_dump(mode="json"),
            "removed_metric_count": len(metric_ids),
            "removed_metric_ids_digest": _ids_digest(tuple(metric_ids)),
            "removed_diagnostic_count": len(diagnostic_ids),
            "removed_diagnostic_ids_digest": _ids_digest(tuple(diagnostic_ids)),
        }
        self._write_new_or_equal(self._intent_path, intent_document)
        live_metrics_before = int(
            self._compact._connection.execute(
                f"SELECT count(*) FROM {_METRIC_TABLE} WHERE origin='LIVE'"
            ).fetchone()[0]
        )
        live_diagnostics_before = int(
            self._compact._connection.execute(
                f"SELECT count(*) FROM {_DIAGNOSTIC_TABLE} WHERE origin='LIVE'"
            ).fetchone()[0]
        )
        raw_before = self._destination.raw_records.count()
        observations_before = self._destination.observations.count()
        current_seal = self._compact.historical_seal()
        if current_seal != repair.invalid_seal:
            raise HistoricalRecoveryError("historical repair was not in a recognized prestate")
        new_seal = self._compact.repair_historical_inventory(
            metric_ids=metric_ids,
            diagnostic_ids=diagnostic_ids,
            before_seal=pre_seal,
            after_metric_count=after_metric_count,
            after_metric_digest=after_metric_digest,
            after_diagnostic_count=after_diagnostic_count,
            after_diagnostic_digest=after_diagnostic_digest,
            sealed_at=self._clock(),
        )
        if (
            new_seal.metric_count != after_metric_count
            or new_seal.metric_digest != after_metric_digest
            or new_seal.diagnostic_count != after_diagnostic_count
            or new_seal.diagnostic_digest != after_diagnostic_digest
            or new_seal.source_fingerprint != pre_seal.source_fingerprint
        ):
            raise HistoricalRecoveryError("historical repair published an unexpected seal")
        if (
            self._compact._connection.execute(
                f"SELECT count(*) FROM {_METRIC_TABLE} "
                f"WHERE result_id IN ({_placeholders(len(metric_ids))})",
                [str(identifier) for identifier in metric_ids],
            ).fetchone()[0]
            or self._compact._connection.execute(
                f"SELECT count(*) FROM {_DIAGNOSTIC_TABLE} "
                f"WHERE diagnostic_id IN ({_placeholders(len(diagnostic_ids))})",
                [str(identifier) for identifier in diagnostic_ids],
            ).fetchone()[0]
        ):
            raise HistoricalRecoveryError("historical repair candidates remain")
        if (
            self._compact._connection.execute(
                f"SELECT count(*) FROM {_METRIC_TABLE} WHERE origin='LIVE'"
            ).fetchone()[0]
            != live_metrics_before
            or self._compact._connection.execute(
                f"SELECT count(*) FROM {_DIAGNOSTIC_TABLE} WHERE origin='LIVE'"
            ).fetchone()[0]
            != live_diagnostics_before
            or self._destination.raw_records.count() != raw_before
            or self._destination.observations.count() != observations_before
        ):
            raise HistoricalRecoveryError("historical repair touched a foreign inventory")
        self._compact.verify_historical_relationships()
        receipt = HistoricalRecoveryRepairReceipt(
            repair_id=repair_id,
            destination_workspace_id=self._selection.destination_workspace_id,
            selection_id=self._selection.selection_id,
            selection_digest=self._selection.digest,
            manifest_sha256=manifest_sha,
            before_inventory_digest=before_inventory_digest,
            after_inventory_digest=after_inventory_digest,
            removed_metric_ids=tuple(sorted(metric_ids, key=str)),
            removed_diagnostic_ids=tuple(sorted(diagnostic_ids, key=str)),
            before_seal=pre_seal,
            after_seal=new_seal,
            started_at=started_at,
            completed_at=self._clock(),
        )
        self._write_new_or_equal(self._receipt_path, receipt.model_dump(mode="json"))
        return receipt


def load_historical_recovery_manifest(path: Path) -> HistoricalRecoveryManifest:
    """Load and validate one sealed recovery selection."""
    try:
        return HistoricalRecoveryManifest.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise HistoricalRecoveryError("historical recovery selection is unreadable") from error


def verify_historical_recovery_selection(manifest: HistoricalRecoveryManifest, root: Path) -> None:
    """Re-verify every sealed page and its aggregate totals without writing."""
    base = root.resolve()
    components: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {
            "items": 0,
            "byte_length": 0,
            "import_required": 0,
            "already_present": 0,
            "archive_only": 0,
        }
    )
    counts = {
        "raw_records": 0,
        "observations": 0,
        "metric_results": 0,
        "diagnostic_results": 0,
        "document_contents": 0,
        "import_required": 0,
        "already_present": 0,
        "archive_only": 0,
        "conflicts": 0,
        "byte_length": 0,
    }
    for page in manifest.pages:
        path = base / page.relative_path
        try:
            path.relative_to(base)
        except ValueError as error:
            raise HistoricalRecoveryError("historical recovery page escapes its root") from error
        if path.is_symlink() or not path.is_file():
            raise HistoricalRecoveryError("historical recovery page is missing or unsafe")
        data = path.read_bytes()
        if len(data) != page.byte_length or hashlib.sha256(data).hexdigest() != page.sha256:
            raise HistoricalRecoveryError("historical recovery page digest differs")
        items = tuple(
            HistoricalRecoveryItem.model_validate_json(line) for line in data.splitlines() if line
        )
        if len(items) != page.rows or any(
            item.entity_kind is not page.entity_kind for item in items
        ):
            raise HistoricalRecoveryError("historical recovery page content differs")
        for item in items:
            counts["byte_length"] += item.byte_length
            counts[item.entity_kind.value.replace("-", "_") + "s"] += 1
            if item.classification is HistoricalRecoveryClassification.IMPORTAR_FALTANTE:
                counts["import_required"] += 1
            elif item.classification is HistoricalRecoveryClassification.YA_PRESENTE:
                counts["already_present"] += 1
            elif item.classification is HistoricalRecoveryClassification.ARCHIVE_ONLY:
                counts["archive_only"] += 1
            elif item.classification is HistoricalRecoveryClassification.CONFLICTO:
                counts["conflicts"] += 1
            key = (item.entity_kind.value, item.family.value)
            components[key]["items"] += 1
            components[key]["byte_length"] += item.byte_length
            if item.classification is HistoricalRecoveryClassification.IMPORTAR_FALTANTE:
                components[key]["import_required"] += 1
            elif item.classification is HistoricalRecoveryClassification.YA_PRESENTE:
                components[key]["already_present"] += 1
            elif item.classification is HistoricalRecoveryClassification.ARCHIVE_ONLY:
                components[key]["archive_only"] += 1
    if counts["conflicts"]:
        raise HistoricalRecoveryError("historical recovery selection contains conflicts")
    if counts["byte_length"] != manifest.totals.byte_length:
        raise HistoricalRecoveryError("historical recovery selection byte total differs")
    for name, value in (
        ("raw_records", manifest.totals.raw_records),
        ("observations", manifest.totals.observations),
        ("metric_results", manifest.totals.metric_results),
        ("diagnostic_results", manifest.totals.diagnostic_results),
        ("document_contents", manifest.totals.document_contents),
        ("import_required", manifest.totals.import_required),
        ("already_present", manifest.totals.already_present),
        ("archive_only", manifest.totals.archive_only),
    ):
        if counts.get(name) != value:
            raise HistoricalRecoveryError("historical recovery selection total differs")


def require_historical_recovery_budget(budget: HistoricalRecoveryBudget) -> None:
    """Validate the measured budget before opening a destination writer."""
    require_historical_recovery_admission(budget)


__all__ = [
    "HISTORICAL_RECOVERY_ADMISSION_SCHEMA",
    "HISTORICAL_RECOVERY_IMPORT_SCHEMA",
    "HISTORICAL_RECOVERY_POLICY_VERSION",
    "HISTORICAL_RECOVERY_PROGRESS_SCHEMA",
    "HISTORICAL_RECOVERY_REPAIR_MANIFEST_SCHEMA",
    "HISTORICAL_RECOVERY_REPAIR_RECEIPT_SCHEMA",
    "HISTORICAL_RECOVERY_SELECTION_SCHEMA",
    "HistoricalRecoveryAdmission",
    "HistoricalRecoveryClassification",
    "HistoricalRecoveryCounts",
    "HistoricalRecoveryEntityKind",
    "HistoricalRecoveryError",
    "HistoricalRecoveryExclusion",
    "HistoricalRecoveryFamily",
    "HistoricalRecoveryFamilyTotal",
    "HistoricalRecoveryImportSummary",
    "HistoricalRecoveryImporter",
    "HistoricalRecoveryItem",
    "HistoricalRecoveryManifest",
    "HistoricalRecoveryPage",
    "HistoricalRecoveryPause",
    "HistoricalRecoveryPresentBasis",
    "HistoricalRecoveryProgress",
    "HistoricalRecoveryRepairManifest",
    "HistoricalRecoveryRepairReceipt",
    "HistoricalRecoveryRepairService",
    "HistoricalRecoverySelectionBuilder",
    "HistoricalRecoveryTotals",
    "create_historical_recovery_admission",
    "load_historical_recovery_admission",
    "load_historical_recovery_manifest",
    "require_historical_recovery_budget",
    "validate_historical_recovery_admission",
    "verify_historical_recovery_selection",
    "write_historical_recovery_admission",
]
