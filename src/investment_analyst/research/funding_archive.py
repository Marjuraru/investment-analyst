"""Bounded Deribit funding acquisition and canonical sealing of one archive."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic as default_monotonic
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, ValidationError

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.catalog.provider_configuration import resolve_deribit_configuration
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.asset_config import DeribitAssetConfiguration
from investment_analyst.providers.crypto.deribit import (
    DeribitClient,
    DeribitError,
    DeribitFundingPoint,
)
from investment_analyst.research.funding_models import (
    ARCHIVE_PAGES_FILE_NAME,
    FUNDING_FEATURES_FILE_NAME,
    FUNDING_LIMITATIONS,
    MANIFEST_FILE_NAME,
    MAX_LOGICAL_PAGES,
    MAX_PAGE_INTERVAL,
    MAX_SEALED_ARCHIVE_BYTES,
    FundingArchive,
    FundingArchiveCounts,
    FundingArchiveManifest,
    FundingArchivePage,
    FundingArchiveRequest,
    FundingAssetIdentity,
    FundingPoint,
    assumed_funding_available_instant,
    funding_archive_id,
    funding_archive_semantic_hash,
    funding_point_id,
)
from investment_analyst.research.historical_models import AvailabilityBasis


class FundingArchiveError(RuntimeError):
    """Base error for funding archive acquisition and sealing."""


class FundingArchiveRequestError(FundingArchiveError, ValueError):
    """The requested range, cut or destination is not admissible."""


class FundingArchiveScopeError(FundingArchiveError, ValueError):
    """The requested asset is outside the supported crypto funding scope."""


class FundingArchiveIntegrityError(FundingArchiveError):
    """Sealed content contradicts its manifest or deterministic identity."""


class FundingArchiveConflictError(FundingArchiveError):
    """An existing destination differs from the requested archive."""


class ResolvedFundingScope(ContractModel):
    """Catalog-backed Deribit funding scope for one crypto asset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset: FundingAssetIdentity
    configuration: DeribitAssetConfiguration


class FundingArchiveReceipt(ContractModel):
    """Compact archive seal receipt without point values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    archive_id: UUID
    semantic_sha256: str
    pages_sha256: str
    pages_bytes: int = Field(ge=0)
    decision_count: int = Field(ge=1)
    expected_points: int = Field(ge=1)
    observed_points: int = Field(ge=0)
    missing_points: int = Field(ge=0)
    page_count: int = Field(ge=1)
    retrieved_at: UTCDateTime
    output_dir: NonEmptyStr
    archive_dir: NonEmptyStr
    status: Literal["sealed", "reused"]
    availability_policy: NonEmptyStr
    strict_pit_verified: Literal[False] = False
    provider_documentation_url: NonEmptyStr
    provider_terms_url: NonEmptyStr

    def to_json_dict(self) -> dict[str, object]:
        """Return the deterministic JSON projection used by the CLI."""
        return self.model_dump(mode="json")


def resolve_funding_scope(
    asset_id: str,
    *,
    catalog: AssetCatalogService | None = None,
) -> ResolvedFundingScope:
    """Resolve one catalog-backed crypto Deribit funding scope."""
    service = catalog if catalog is not None else AssetCatalogService.load_default()
    asset = service.get(asset_id)
    if not asset.is_active:
        raise FundingArchiveScopeError(f"asset {asset_id} is not active in the catalog")
    if asset.crypto_profile is None:
        raise FundingArchiveScopeError(f"asset {asset_id} lacks an explicit crypto profile")
    configuration = resolve_deribit_configuration(
        ProviderAssetContextResolver(service),
        asset_id=asset_id,
    )
    identity = FundingAssetIdentity(
        asset_id=asset.asset_id,
        funding_source_id=configuration.funding_source_id,
        currency=configuration.currency,
        instrument_name=configuration.instrument_name,
        asset_class=configuration.asset_class,
        quote_currency=asset.quote_currency,
        crypto_profile=asset.crypto_profile.value,
    )
    return ResolvedFundingScope(asset=identity, configuration=configuration)


def validate_funding_archive_request(
    scope: ResolvedFundingScope,
    request: FundingArchiveRequest,
) -> None:
    """Reject an archive request whose asset differs from the resolved scope."""
    if request.asset_id != scope.asset.asset_id:
        raise FundingArchiveRequestError("request asset does not match the resolved funding scope")


def _utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FundingArchiveRequestError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _raw_payload(point: DeribitFundingPoint) -> dict[str, str]:
    payload: dict[str, str] = {}
    for key in ("timestamp", "index_price", "prev_index_price", "interest_1h", "interest_8h"):
        value = point.raw_payload.get(key)
        if not isinstance(value, str):
            raise FundingArchiveIntegrityError("funding raw_payload values must be exact strings")
        payload[key] = value
    if set(point.raw_payload) != set(payload):
        raise FundingArchiveIntegrityError("funding raw_payload has an unexpected shape")
    return payload


def _point_from_provider(
    point: DeribitFundingPoint,
    *,
    page_index: int,
    funding_source_id: str,
) -> FundingPoint:
    raw = _raw_payload(point)
    point_id = funding_point_id(
        funding_source_id=funding_source_id,
        timestamp=point.timestamp,
        index_price=point.index_price,
        prev_index_price=point.prev_index_price,
        interest_1h=point.interest_1h,
        interest_8h=point.interest_8h,
        raw_payload=raw,
    )
    return FundingPoint(
        point_id=point_id,
        page_index=page_index,
        timestamp=point.timestamp,
        index_price=point.index_price,
        prev_index_price=point.prev_index_price,
        interest_1h=point.interest_1h,
        interest_8h=point.interest_8h,
        raw_payload=raw,
        assumed_available_at=assumed_funding_available_instant(point.timestamp),
    )


def _page_bytes(points: tuple[FundingPoint, ...]) -> bytes:
    ordered = tuple(sorted(points, key=lambda point: point.timestamp))
    return b"".join(
        canonical_analytical_json(
            {
                "page_index": point.page_index,
                "point_id": str(point.point_id),
                "timestamp": point.timestamp,
                "raw_payload": point.raw_payload,
            }
        )
        + b"\n"
        for point in ordered
    )


def _pages_bytes(
    pages: tuple[FundingArchivePage, ...],
    points: tuple[FundingPoint, ...],
) -> bytes:
    return b"".join(
        _page_bytes(tuple(point for point in points if point.page_index == page.page_index))
        for page in pages
    )


def _manifest_bytes(archive: FundingArchive) -> bytes:
    return canonical_analytical_json(archive.manifest.model_dump(mode="json"))


def build_funding_archive(
    *,
    asset: FundingAssetIdentity,
    request: FundingArchiveRequest,
    pages: tuple[FundingArchivePage, ...],
    points: tuple[FundingPoint, ...],
    retrieved_at: datetime,
) -> FundingArchive:
    """Assemble one sealed archive from its canonical pages and hourly points."""
    counts = FundingArchiveCounts(
        requested_points=request.expected_points,
        observed_points=len(points),
        missing_points=request.expected_points - len(points),
        page_count=len(pages),
    )
    ordered_points = tuple(sorted(points, key=lambda point: point.timestamp))
    pages_bytes = _pages_bytes(pages, ordered_points)
    archive_id = funding_archive_id(
        asset=asset,
        request=request,
        pages=pages,
        retrieved_at=retrieved_at,
        points=ordered_points,
    )
    semantic_sha256 = funding_archive_semantic_hash(
        archive_id=archive_id,
        points=ordered_points,
    )
    manifest = FundingArchiveManifest(
        archive_id=archive_id,
        asset=asset,
        request=request,
        pages=pages,
        counts=counts,
        retrieved_at=retrieved_at,
        availability_basis=AvailabilityBasis.HISTORICAL_RECONSTRUCTION_ASSUMED,
        semantic_sha256=semantic_sha256,
        pages_sha256=_sha256_bytes(pages_bytes),
        pages_bytes=len(pages_bytes),
        limitations=FUNDING_LIMITATIONS,
    )
    return FundingArchive(
        archive_id=archive_id,
        manifest=manifest,
        points=ordered_points,
    )


def _point_from_payload(
    payload: object,
    *,
    manifest: FundingArchiveManifest,
) -> FundingPoint:
    if not isinstance(payload, dict):
        raise FundingArchiveIntegrityError("pages.jsonl rows must be JSON objects")
    expected = {"page_index", "point_id", "timestamp", "raw_payload"}
    if set(payload) != expected:
        raise FundingArchiveIntegrityError("pages.jsonl row has an unexpected shape")
    page_index = payload["page_index"]
    if isinstance(page_index, bool) or not isinstance(page_index, int):
        raise FundingArchiveIntegrityError("pages.jsonl page_index must be an integer")
    timestamp_value = payload["timestamp"]
    if not isinstance(timestamp_value, str):
        raise FundingArchiveIntegrityError("pages.jsonl timestamp must be ISO 8601 text")
    try:
        timestamp = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
    except ValueError as error:
        raise FundingArchiveIntegrityError("pages.jsonl timestamp is malformed") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise FundingArchiveIntegrityError("pages.jsonl timestamp must be UTC")
    raw_payload = payload["raw_payload"]
    if not isinstance(raw_payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw_payload.items()
    ):
        raise FundingArchiveIntegrityError("pages.jsonl raw_payload must contain exact strings")
    try:
        point_id = UUID(str(payload["point_id"]))
    except ValueError as error:
        raise FundingArchiveIntegrityError("pages.jsonl point_id is malformed") from error
    try:
        point = FundingPoint(
            point_id=point_id,
            page_index=page_index,
            timestamp=timestamp,
            index_price=raw_payload["index_price"],
            prev_index_price=raw_payload["prev_index_price"],
            interest_1h=raw_payload["interest_1h"],
            interest_8h=raw_payload["interest_8h"],
            raw_payload=raw_payload,
            assumed_available_at=assumed_funding_available_instant(timestamp),
        )
    except (KeyError, ValueError, ValidationError) as error:
        raise FundingArchiveIntegrityError("pages.jsonl row is invalid") from error
    expected_id = funding_point_id(
        funding_source_id=manifest.asset.funding_source_id,
        timestamp=point.timestamp,
        index_price=point.index_price,
        prev_index_price=point.prev_index_price,
        interest_1h=point.interest_1h,
        interest_8h=point.interest_8h,
        raw_payload=point.raw_payload,
    )
    if point.point_id != expected_id:
        raise FundingArchiveIntegrityError("pages.jsonl point identity is not deterministic")
    return point


def read_funding_archive(archive_dir: Path) -> FundingArchive:
    """Read and fully verify one sealed funding archive directory."""
    if archive_dir.is_symlink() or not archive_dir.is_dir():
        raise FundingArchiveConflictError("archive directory is missing or is a symlink")
    manifest_path = archive_dir / MANIFEST_FILE_NAME
    pages_path = archive_dir / ARCHIVE_PAGES_FILE_NAME
    if pages_path.is_symlink() or not pages_path.is_file():
        raise FundingArchiveConflictError("pages.jsonl is missing or is a symlink")
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise FundingArchiveConflictError("manifest.json is missing or is a symlink")
    try:
        manifest_payload = manifest_path.read_bytes()
    except OSError as error:
        raise FundingArchiveConflictError("manifest.json is unreadable") from error
    try:
        manifest = FundingArchiveManifest.model_validate_json(manifest_payload)
    except ValueError as error:
        raise FundingArchiveConflictError("manifest.json is malformed") from error
    if manifest_payload != canonical_analytical_json(manifest.model_dump(mode="json")):
        raise FundingArchiveConflictError("manifest.json is not in canonical encoding")
    digest, size = _sha256_file(pages_path)
    if digest != manifest.pages_sha256 or size != manifest.pages_bytes:
        raise FundingArchiveConflictError("pages.jsonl differs from its manifest")
    if size > MAX_SEALED_ARCHIVE_BYTES or len(manifest_payload) > MAX_SEALED_ARCHIVE_BYTES:
        raise FundingArchiveConflictError("sealed funding archive exceeds the byte budget")
    try:
        lines = pages_path.read_bytes().splitlines()
    except OSError as error:
        raise FundingArchiveConflictError("pages.jsonl is unreadable") from error
    points: list[FundingPoint] = []
    for line in lines:
        if not line:
            raise FundingArchiveConflictError("pages.jsonl contains an empty line")
        try:
            decoded = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FundingArchiveConflictError("pages.jsonl contains invalid JSON") from error
        points.append(_point_from_payload(decoded, manifest=manifest))
    for page in manifest.pages:
        page_points = tuple(point for point in points if point.page_index == page.page_index)
        page_payload = _page_bytes(page_points)
        if len(page_points) != page.point_count:
            raise FundingArchiveConflictError("pages.jsonl page count differs from its manifest")
        if _sha256_bytes(page_payload) != page.points_sha256:
            raise FundingArchiveConflictError("pages.jsonl page hash differs from its manifest")
        if len(page_payload) != page.points_bytes:
            raise FundingArchiveConflictError("pages.jsonl page size differs from its manifest")
    try:
        return FundingArchive(
            archive_id=manifest.archive_id,
            manifest=manifest,
            points=tuple(points),
        )
    except ValidationError as error:
        raise FundingArchiveConflictError("archive content contradicts its manifest") from error


def _sealed_archive_bytes(archive: FundingArchive) -> tuple[bytes, bytes]:
    return (
        _pages_bytes(archive.manifest.pages, archive.points),
        _manifest_bytes(archive),
    )


def _validate_output_dir(output_dir: Path) -> Path:
    if not output_dir.is_absolute():
        raise FundingArchiveRequestError("output_dir must be an absolute path")
    declared = output_dir.expanduser()
    for candidate in (declared, *declared.parents):
        if candidate.is_symlink():
            raise FundingArchiveRequestError("output_dir must not traverse a symbolic link")
    resolved = declared.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise FundingArchiveRequestError("output_dir must not be the filesystem root")
    if resolved.exists():
        if resolved.is_symlink() or not resolved.is_dir():
            raise FundingArchiveConflictError("output_dir must be a plain directory")
        _validate_output_entries(resolved)
    return resolved


def _is_archive_claim(directory: Path) -> bool:
    return (directory / ARCHIVE_PAGES_FILE_NAME).exists()


def _is_bundle_claim(directory: Path) -> bool:
    return (directory / FUNDING_FEATURES_FILE_NAME).exists()


def _validate_output_entries(root: Path) -> None:
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child.name.startswith("."):
            continue
        if child.is_symlink() or not child.is_dir():
            raise FundingArchiveConflictError("output_dir contains an unexpected entry")
        if _is_archive_claim(child) or _is_bundle_claim(child):
            continue
        if (child / MANIFEST_FILE_NAME).exists():
            raise FundingArchiveConflictError("output_dir contains an unrecognized artifact")
        raise FundingArchiveConflictError("output_dir contains an unrelated entry")


def _find_reusable_archive(
    output_dir: Path,
    *,
    scope: FundingAssetIdentity,
    request: FundingArchiveRequest,
) -> FundingArchive | None:
    """Return the single sealed archive for this request without network access."""
    if not output_dir.exists():
        return None
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise FundingArchiveConflictError("output_dir is not a plain directory")
    _validate_output_entries(output_dir)
    matches: list[FundingArchive] = []
    for child in sorted(output_dir.iterdir(), key=lambda item: item.name):
        if child.name.startswith(".") or not _is_archive_claim(child):
            continue
        existing = read_funding_archive(child)
        if existing.manifest.asset != scope:
            raise FundingArchiveConflictError("output_dir contains another funding scope")
        if existing.manifest.request == request:
            matches.append(existing)
    if len(matches) > 1:
        raise FundingArchiveConflictError("output_dir holds multiple receptions for the request")
    return matches[0] if matches else None


def seal_funding_archive(archive: FundingArchive, *, output_dir: Path) -> FundingArchiveReceipt:
    """Atomically seal one archive under output_dir or reuse an identical artifact."""
    resolved_output = _validate_output_dir(output_dir)
    resolved_output.mkdir(parents=True, exist_ok=True)
    archive_dir = resolved_output / str(archive.archive_id)
    if archive_dir.exists():
        existing = read_funding_archive(archive_dir)
        if (
            existing.archive_id != archive.archive_id
            or existing.manifest.semantic_sha256 != archive.manifest.semantic_sha256
            or existing.manifest.request != archive.manifest.request
            or existing.manifest.asset != archive.manifest.asset
        ):
            raise FundingArchiveConflictError("destination contains a different funding archive")
        return _receipt(existing, resolved_output, status="reused")

    pages_bytes, manifest_bytes = _sealed_archive_bytes(archive)
    if (
        len(pages_bytes) > MAX_SEALED_ARCHIVE_BYTES
        or len(manifest_bytes) > MAX_SEALED_ARCHIVE_BYTES
    ):
        raise FundingArchiveRequestError("funding archive exceeds the sealed byte budget")
    temporary = resolved_output / f".{archive.archive_id}.tmp-{uuid4().hex}"
    temporary.mkdir()
    try:
        (temporary / ARCHIVE_PAGES_FILE_NAME).write_bytes(pages_bytes)
        (temporary / MANIFEST_FILE_NAME).write_bytes(manifest_bytes)
        sealed = read_funding_archive(temporary)
        try:
            temporary.rename(archive_dir)
        except OSError as error:
            if archive_dir.exists():
                competing = read_funding_archive(archive_dir)
                if competing.manifest.semantic_sha256 == archive.manifest.semantic_sha256:
                    _remove_temporary(temporary)
                    return _receipt(competing, resolved_output, status="reused")
            raise FundingArchiveConflictError(
                "funding archive destination appeared and is not the same artifact"
            ) from error
    except BaseException:
        if temporary.exists():
            _remove_temporary(temporary)
        raise
    return _receipt(sealed, resolved_output, status="sealed")


def _receipt(
    archive: FundingArchive,
    output_dir: Path,
    *,
    status: Literal["sealed", "reused"],
) -> FundingArchiveReceipt:
    manifest = archive.manifest
    return FundingArchiveReceipt(
        archive_id=archive.archive_id,
        semantic_sha256=manifest.semantic_sha256,
        pages_sha256=manifest.pages_sha256,
        pages_bytes=manifest.pages_bytes,
        decision_count=manifest.request.decision_count,
        expected_points=manifest.counts.requested_points,
        observed_points=manifest.counts.observed_points,
        missing_points=manifest.counts.missing_points,
        page_count=manifest.counts.page_count,
        retrieved_at=manifest.retrieved_at,
        output_dir=str(output_dir),
        archive_dir=str(output_dir / str(archive.archive_id)),
        status=status,
        availability_policy=manifest.availability_policy,
        provider_documentation_url=manifest.provider_documentation_url,
        provider_terms_url=manifest.provider_terms_url,
    )


def _remove_temporary(temporary: Path) -> None:
    for child in temporary.iterdir():
        if child.is_file() and not child.is_symlink():
            child.unlink()
    temporary.rmdir()


def acquire_funding_archive(
    client: DeribitClient,
    request: FundingArchiveRequest,
    *,
    output_dir: Path,
    catalog: AssetCatalogService | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = default_monotonic,
) -> FundingArchiveReceipt:
    """Fetch one bounded range page by page, then seal it or reuse it offline."""
    if not output_dir.is_absolute():
        raise FundingArchiveRequestError("output_dir must be an absolute path")
    resolved_output = _validate_output_dir(output_dir)
    scope = resolve_funding_scope(request.asset_id, catalog=catalog)
    validate_funding_archive_request(scope, request)
    reusable = _find_reusable_archive(
        resolved_output,
        scope=scope.asset,
        request=request,
    )
    if reusable is not None:
        return _receipt(reusable, resolved_output, status="reused")

    pages: list[FundingArchivePage] = []
    points: list[FundingPoint] = []
    cursor = request.requested_start
    page_index = 0
    while cursor < request.requested_end:
        if page_index >= MAX_LOGICAL_PAGES:
            raise FundingArchiveIntegrityError("funding acquisition exceeded the logical page cap")
        chunk_end = min(cursor + MAX_PAGE_INTERVAL, request.requested_end)
        started_at = _utc(clock(), "clock result")
        started_monotonic = monotonic()
        try:
            fetch = client.fetch_funding_history(
                scope.asset.instrument_name,
                cursor,
                chunk_end,
            )
        except DeribitError as error:
            raise FundingArchiveError(
                f"funding acquisition failed without a seal: {error}"
            ) from error
        finished_at = _utc(clock(), "clock result")
        elapsed_milliseconds = max(0, int((monotonic() - started_monotonic) * 1_000))
        if fetch.requested_start != cursor or fetch.requested_end != chunk_end:
            raise FundingArchiveIntegrityError("provider page interval differs from the request")
        if len(fetch.request_urls) != 1:
            raise FundingArchiveIntegrityError("a logical funding page must use one request")
        page_points = tuple(
            _point_from_provider(
                point,
                page_index=page_index,
                funding_source_id=scope.asset.funding_source_id,
            )
            for point in fetch.points
        )
        page_payload = _page_bytes(page_points)
        pages.append(
            FundingArchivePage(
                page_index=page_index,
                requested_start=cursor,
                requested_end=chunk_end,
                request_url=fetch.request_urls[0],
                started_at=started_at,
                finished_at=finished_at,
                elapsed_milliseconds=elapsed_milliseconds,
                retrieved_at=fetch.retrieved_at,
                point_count=len(page_points),
                points_sha256=_sha256_bytes(page_payload),
                points_bytes=len(page_payload),
            )
        )
        points.extend(page_points)
        cursor = chunk_end
        page_index += 1
    if not pages:
        raise FundingArchiveIntegrityError("funding acquisition produced no pages")
    retrieved_at = max(page.retrieved_at for page in pages)
    archive = build_funding_archive(
        asset=scope.asset,
        request=request,
        pages=tuple(pages),
        points=tuple(points),
        retrieved_at=retrieved_at,
    )
    return seal_funding_archive(archive, output_dir=resolved_output)


__all__ = [
    "FundingArchiveConflictError",
    "FundingArchiveError",
    "FundingArchiveIntegrityError",
    "FundingArchiveReceipt",
    "FundingArchiveRequestError",
    "FundingArchiveScopeError",
    "ResolvedFundingScope",
    "acquire_funding_archive",
    "build_funding_archive",
    "read_funding_archive",
    "resolve_funding_scope",
    "seal_funding_archive",
    "validate_funding_archive_request",
]
