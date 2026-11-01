"""Bounded Coinbase acquisition and canonical sealing of one historical archive."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, ValidationError

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.catalog.provider_configuration import resolve_coinbase_configuration
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.crypto.coinbase_exchange import (
    CoinbaseCandle,
    CoinbaseExchangeClient,
    CoinbaseExchangeError,
)
from investment_analyst.research.historical_models import (
    CANDLES_FILE_NAME,
    MANIFEST_FILE_NAME,
    MAX_ARCHIVE_REQUESTS,
    MAX_CANDLES_PER_REQUEST,
    ROWS_FILE_NAME,
    SUPPORTED_ASSET_CLASS,
    SUPPORTED_GRANULARITY_SECONDS,
    SUPPORTED_QUOTE_CURRENCY,
    AvailabilityBasis,
    HistoricalArchive,
    HistoricalArchiveCounts,
    HistoricalArchiveManifest,
    HistoricalArchiveRequest,
    HistoricalAssetIdentity,
    HistoricalCandle,
    assumed_available_instant,
    bar_start_instant,
    historical_archive_id,
    historical_archive_semantic_hash,
    historical_bar_content_id,
)

MAX_SEALED_ARCHIVE_BYTES = 4 * 1024 * 1024

_ARCHIVE_LIMITATIONS = (
    "Coinbase puede omitir intervalos sin ticks; una ausencia se conserva y nunca"
    " se rellena con el dia anterior, el siguiente ni un precio cero.",
    "El archivo es una representacion canonica de la respuesta parseada, no una copia"
    " exacta del HTTP ni un RawRecord del workspace.",
    "El receipt acredita que las peticiones finalizaron, no que todos los dias"
    " solicitados tengan vela.",
    "No se afirma checksum, licencia abierta ni derecho a redistribuir datos del"
    " proveedor; el uso previsto es personal y local.",
)


class HistoricalArchiveError(RuntimeError):
    """Base error for historical archive acquisition and sealing."""


class HistoricalArchiveRequestError(HistoricalArchiveError, ValueError):
    """The requested range, cut or destination is not admissible."""


class HistoricalArchiveScopeError(HistoricalArchiveError, ValueError):
    """The requested asset is outside the supported crypto spot USD daily scope."""


class HistoricalArchiveIntegrityError(HistoricalArchiveError):
    """Sealed content contradicts its manifest or deterministic identity."""


class HistoricalArchiveConflictError(HistoricalArchiveError):
    """An existing destination differs from the requested archive."""


class ResolvedHistoricalScope(ContractModel):
    """Catalog-backed scope plus the exact Coinbase daily configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset: HistoricalAssetIdentity
    product_id: NonEmptyStr
    history_start: str | None = None


@dataclass(frozen=True, slots=True)
class _ArchiveContent:
    archive_id: UUID
    candles: tuple[HistoricalCandle, ...]
    counts: HistoricalArchiveCounts
    semantic_sha256: str


class HistoricalArchiveReceipt(ContractModel):
    """Compact seal receipt without candle values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    archive_id: UUID
    semantic_sha256: str
    candles_sha256: str
    candles_bytes: int = Field(ge=0)
    requested_days: int = Field(ge=1)
    bar_count: int = Field(ge=0)
    absent_days: int = Field(ge=0)
    request_count: int = Field(ge=1)
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


def resolve_historical_scope(
    asset_id: str,
    *,
    catalog: AssetCatalogService | None = None,
) -> ResolvedHistoricalScope:
    """Resolve one catalog-backed crypto spot USD Coinbase daily scope."""
    service = catalog if catalog is not None else AssetCatalogService.load_default()
    asset = service.get(asset_id)
    if not asset.is_active:
        raise HistoricalArchiveScopeError(f"asset {asset_id} is not active in the catalog")
    if asset.asset_class is not SUPPORTED_ASSET_CLASS:
        raise HistoricalArchiveScopeError(f"asset {asset_id} is not a crypto asset")
    if asset.quote_currency != SUPPORTED_QUOTE_CURRENCY:
        raise HistoricalArchiveScopeError(
            f"asset {asset_id} does not quote in {SUPPORTED_QUOTE_CURRENCY}"
        )
    if asset.crypto_profile is None:
        raise HistoricalArchiveScopeError(f"asset {asset_id} lacks an explicit crypto profile")
    configuration = resolve_coinbase_configuration(
        ProviderAssetContextResolver(service),
        asset_id=asset_id,
    )
    if configuration.asset_class is not SUPPORTED_ASSET_CLASS:
        raise HistoricalArchiveScopeError("resolved Coinbase configuration is not crypto")
    if configuration.quote_unit != SUPPORTED_QUOTE_CURRENCY:
        raise HistoricalArchiveScopeError("resolved Coinbase configuration does not quote in USD")
    if configuration.granularity_seconds != SUPPORTED_GRANULARITY_SECONDS:
        raise HistoricalArchiveScopeError("resolved Coinbase configuration is not daily")
    identity = HistoricalAssetIdentity(
        asset_id=asset.asset_id,
        source_id=configuration.source_id,
        product_id=configuration.product_id,
        asset_class=configuration.asset_class,
        quote_currency=asset.quote_currency,
        crypto_profile=asset.crypto_profile.value,
        exchange=asset.exchange or "UNKNOWN",
        granularity_seconds=configuration.granularity_seconds,
    )
    return ResolvedHistoricalScope(
        asset=identity,
        product_id=configuration.product_id,
        history_start=(
            configuration.history_start.isoformat()
            if configuration.history_start is not None
            else None
        ),
    )


def validate_archive_range(
    scope: ResolvedHistoricalScope, request: HistoricalArchiveRequest
) -> None:
    """Reject assets and ranges that the resolved catalog scope cannot satisfy."""
    if request.asset_id != scope.asset.asset_id:
        raise HistoricalArchiveRequestError("request asset does not match the resolved scope")
    if scope.history_start is not None and request.start_date.isoformat() < scope.history_start:
        raise HistoricalArchiveRequestError(
            f"{scope.asset.asset_id} history starts at {scope.history_start}"
        )


def _candle_record(
    scope: HistoricalAssetIdentity,
    candle: CoinbaseCandle,
    *,
    retrieved_at: datetime,
) -> HistoricalCandle:
    bar_end = candle.start + timedelta(days=1)
    return HistoricalCandle(
        bar_content_id=historical_bar_content_id(
            asset_id=scope.asset_id,
            source_id=scope.source_id,
            bar_start=candle.start,
            open_value=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            raw_values=candle.raw_values,
        ),
        bar_start=candle.start,
        bar_end=bar_end,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
        raw_values=candle.raw_values,
        assumed_available_at=assumed_available_instant(bar_end),
        retrieved_at=retrieved_at,
    )


def _archive_content(
    scope: HistoricalAssetIdentity,
    request: HistoricalArchiveRequest,
    *,
    request_urls: tuple[str, ...],
    retrieved_at: datetime,
    candles: tuple[HistoricalCandle, ...],
) -> _ArchiveContent:
    requested_days = request.requested_days()
    if len(candles) > requested_days:
        raise HistoricalArchiveIntegrityError(
            "provider returned more bars than requested days; refusing ambiguous coverage"
        )
    counts = HistoricalArchiveCounts(
        requested_days=requested_days,
        bar_count=len(candles),
        absent_days=requested_days - len(candles),
    )
    archive_id = historical_archive_id(
        asset=scope,
        request=request,
        request_urls=request_urls,
        retrieved_at=retrieved_at,
        candles=candles,
    )
    return _ArchiveContent(
        archive_id=archive_id,
        candles=candles,
        counts=counts,
        semantic_sha256=historical_archive_semantic_hash(
            archive_id=archive_id,
            candles=candles,
        ),
    )


def build_historical_archive(
    scope: HistoricalAssetIdentity,
    request: HistoricalArchiveRequest,
    *,
    request_urls: tuple[str, ...],
    retrieved_at: datetime,
    candles: tuple[HistoricalCandle, ...],
) -> HistoricalArchive:
    """Assemble one sealed archive from its canonical candle content."""
    content = _archive_content(
        scope,
        request,
        request_urls=request_urls,
        retrieved_at=retrieved_at,
        candles=candles,
    )
    candles_bytes = _candle_bytes(candles)
    manifest = HistoricalArchiveManifest(
        archive_id=content.archive_id,
        asset=scope,
        request=request,
        requested_start=bar_start_instant(request.start_date),
        requested_end=bar_start_instant(request.end_date + timedelta(days=1)),
        request_urls=request_urls,
        retrieved_at=retrieved_at,
        availability_basis=AvailabilityBasis.HISTORICAL_RECONSTRUCTION_ASSUMED,
        counts=content.counts,
        semantic_sha256=content.semantic_sha256,
        candles_sha256=_sha256_bytes(candles_bytes),
        candles_bytes=len(candles_bytes),
        limitations=_ARCHIVE_LIMITATIONS,
    )
    return HistoricalArchive(
        archive_id=content.archive_id,
        manifest=manifest,
        candles=content.candles,
    )


def _candle_payload(candle: HistoricalCandle) -> dict[str, object]:
    return {
        "bar_content_id": str(candle.bar_content_id),
        "bar_start": candle.bar_start,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
        "raw_values": list(candle.raw_values),
    }


def _candle_bytes(candles: tuple[HistoricalCandle, ...]) -> bytes:
    return b"".join(
        canonical_analytical_json(_candle_payload(candle)) + b"\n" for candle in candles
    )


def _manifest_bytes(archive: HistoricalArchive) -> bytes:
    return canonical_analytical_json(archive.manifest.model_dump(mode="json"))


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


def _validate_output_dir(output_dir: Path) -> Path:
    if not output_dir.is_absolute():
        raise HistoricalArchiveRequestError("output_dir must be an absolute path")
    resolved = output_dir.expanduser().resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise HistoricalArchiveRequestError("output_dir must not be the filesystem root")
    return resolved


def _candle_from_payload(
    payload: object,
    *,
    scope: HistoricalAssetIdentity,
    retrieved_at: datetime,
) -> HistoricalCandle:
    if not isinstance(payload, dict):
        raise HistoricalArchiveIntegrityError("candles.jsonl rows must be JSON objects")
    expected = {
        "bar_content_id",
        "bar_start",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "raw_values",
    }
    if set(payload) != expected:
        raise HistoricalArchiveIntegrityError("candles.jsonl row has an unexpected shape")
    try:
        bar_start = datetime.fromisoformat(str(payload["bar_start"]))
    except ValueError as error:
        raise HistoricalArchiveIntegrityError(
            "candles.jsonl row has a malformed bar_start"
        ) from error
    if bar_start.tzinfo is None or bar_start.utcoffset() is None:
        raise HistoricalArchiveIntegrityError("candles.jsonl row must use UTC timestamps")
    raw_values = payload["raw_values"]
    if not isinstance(raw_values, list) or not all(isinstance(item, str) for item in raw_values):
        raise HistoricalArchiveIntegrityError("candles.jsonl raw_values must be provider strings")
    bar_end = bar_start.astimezone(UTC) + timedelta(days=1)
    try:
        candle = HistoricalCandle(
            bar_content_id=UUID(str(payload["bar_content_id"])),
            bar_start=bar_start,
            bar_end=bar_end,
            open=payload["open"],
            high=payload["high"],
            low=payload["low"],
            close=payload["close"],
            volume=payload["volume"],
            raw_values=tuple(raw_values),
            assumed_available_at=assumed_available_instant(bar_end),
            retrieved_at=retrieved_at,
        )
    except (ValueError, ValidationError) as error:
        raise HistoricalArchiveIntegrityError("candles.jsonl row is invalid") from error
    expected_id = historical_bar_content_id(
        asset_id=scope.asset_id,
        source_id=scope.source_id,
        bar_start=candle.bar_start,
        open_value=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
        raw_values=candle.raw_values,
    )
    if candle.bar_content_id != expected_id:
        raise HistoricalArchiveIntegrityError("candles.jsonl row identity is not deterministic")
    return candle


def read_historical_archive(archive_dir: Path) -> HistoricalArchive:
    """Read and fully verify one sealed archive directory."""
    if archive_dir.is_symlink() or not archive_dir.is_dir():
        raise HistoricalArchiveConflictError("archive directory is missing or is a symlink")
    manifest_path = archive_dir / MANIFEST_FILE_NAME
    candles_path = archive_dir / CANDLES_FILE_NAME
    if candles_path.is_symlink() or not candles_path.is_file():
        raise HistoricalArchiveConflictError("candles.jsonl is missing or is a symlink")
    try:
        payload = manifest_path.read_bytes()
    except OSError as error:
        raise HistoricalArchiveConflictError("manifest.json is unreadable") from error
    try:
        manifest = HistoricalArchiveManifest.model_validate_json(payload)
    except ValueError as error:
        raise HistoricalArchiveConflictError("manifest.json is malformed") from error
    if payload != canonical_analytical_json(manifest.model_dump(mode="json")):
        raise HistoricalArchiveConflictError("manifest.json is not in canonical encoding")
    digest, size = _sha256_file(candles_path)
    if digest != manifest.candles_sha256 or size != manifest.candles_bytes:
        raise HistoricalArchiveConflictError("candles.jsonl differs from its manifest")
    if size > MAX_SEALED_ARCHIVE_BYTES or len(payload) > MAX_SEALED_ARCHIVE_BYTES:
        raise HistoricalArchiveConflictError("sealed archive exceeds the byte budget")
    try:
        lines = candles_path.read_bytes().splitlines()
    except OSError as error:
        raise HistoricalArchiveConflictError("candles.jsonl is unreadable") from error
    candles: list[HistoricalCandle] = []
    for line in lines:
        if not line:
            raise HistoricalArchiveConflictError("candles.jsonl contains an empty line")
        try:
            decoded = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HistoricalArchiveConflictError("candles.jsonl contains invalid JSON") from error
        candles.append(
            _candle_from_payload(
                decoded,
                scope=manifest.asset,
                retrieved_at=manifest.retrieved_at,
            )
        )
    try:
        archive = HistoricalArchive(
            archive_id=manifest.archive_id,
            manifest=manifest,
            candles=tuple(candles),
        )
    except ValidationError as error:
        raise HistoricalArchiveConflictError("archive content contradicts its manifest") from error
    return archive


def _sealed_archive_bytes(archive: HistoricalArchive) -> tuple[bytes, bytes]:
    candles_bytes = _candle_bytes(archive.candles)
    return candles_bytes, _manifest_bytes(archive)


def seal_historical_archive(
    archive: HistoricalArchive, *, output_dir: Path
) -> HistoricalArchiveReceipt:
    """Atomically seal one archive under output_dir or reuse an identical artifact."""
    resolved_output = _validate_output_dir(output_dir)
    resolved_output.mkdir(parents=True, exist_ok=True)
    archive_dir = resolved_output / str(archive.archive_id)
    if archive_dir.exists():
        existing = read_historical_archive(archive_dir)
        if (
            existing.archive_id != archive.archive_id
            or existing.manifest.semantic_sha256 != archive.manifest.semantic_sha256
            or existing.manifest.request != archive.manifest.request
            or existing.manifest.asset != archive.manifest.asset
        ):
            raise HistoricalArchiveConflictError("destination contains a different archive")
        return _receipt(existing, resolved_output, status="reused")

    candles_bytes, manifest_bytes = _sealed_archive_bytes(archive)
    if (
        len(candles_bytes) > MAX_SEALED_ARCHIVE_BYTES
        or len(manifest_bytes) > MAX_SEALED_ARCHIVE_BYTES
    ):
        raise HistoricalArchiveRequestError("archive exceeds the sealed byte budget")
    temporary = resolved_output / f".{archive.archive_id}.tmp-{uuid4().hex}"
    temporary.mkdir()
    try:
        (temporary / CANDLES_FILE_NAME).write_bytes(candles_bytes)
        (temporary / MANIFEST_FILE_NAME).write_bytes(manifest_bytes)
        sealed = read_historical_archive(temporary)
        try:
            temporary.rename(archive_dir)
        except OSError as error:
            if archive_dir.exists():
                competing = read_historical_archive(archive_dir)
                if competing.manifest.semantic_sha256 == archive.manifest.semantic_sha256:
                    _remove_temporary(temporary)
                    return _receipt(competing, resolved_output, status="reused")
            raise HistoricalArchiveConflictError(
                "archive destination appeared and is not the same artifact"
            ) from error
    except BaseException:
        if temporary.exists():
            _remove_temporary(temporary)
        raise
    return _receipt(sealed, resolved_output, status="sealed")


def _receipt(
    archive: HistoricalArchive,
    output_dir: Path,
    *,
    status: Literal["sealed", "reused"],
) -> HistoricalArchiveReceipt:
    return HistoricalArchiveReceipt(
        archive_id=archive.archive_id,
        semantic_sha256=archive.manifest.semantic_sha256,
        candles_sha256=archive.manifest.candles_sha256,
        candles_bytes=archive.manifest.candles_bytes,
        requested_days=archive.manifest.counts.requested_days,
        bar_count=archive.manifest.counts.bar_count,
        absent_days=archive.manifest.counts.absent_days,
        request_count=len(archive.manifest.request_urls),
        retrieved_at=archive.manifest.retrieved_at,
        output_dir=str(output_dir),
        archive_dir=str(output_dir / str(archive.archive_id)),
        status=status,
        availability_policy=archive.manifest.availability_policy,
        provider_documentation_url=archive.manifest.provider_documentation_url,
        provider_terms_url=archive.manifest.provider_terms_url,
    )


def _remove_temporary(temporary: Path) -> None:
    for child in temporary.iterdir():
        if child.is_file() and not child.is_symlink():
            child.unlink()
    temporary.rmdir()


def _is_archive_claim(directory: Path) -> bool:
    """Distinguish an archive candidate from an unrelated sibling artifact."""
    if (directory / CANDLES_FILE_NAME).exists():
        return True
    return (directory / MANIFEST_FILE_NAME).exists() and not (directory / ROWS_FILE_NAME).exists()


def _find_reusable_archive(
    output_dir: Path,
    *,
    scope: HistoricalAssetIdentity,
    request: HistoricalArchiveRequest,
) -> HistoricalArchive | None:
    """Return the single sealed archive for this request without any network access."""
    if not output_dir.exists():
        return None
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise HistoricalArchiveConflictError("output_dir is not a plain directory")
    matches: list[HistoricalArchive] = []
    for child in sorted(output_dir.iterdir(), key=lambda item: item.name):
        if child.name.startswith("."):
            continue
        if child.is_symlink() or not child.is_dir():
            raise HistoricalArchiveConflictError("output_dir contains an unexpected entry")
        if not _is_archive_claim(child):
            continue
        existing = read_historical_archive(child)
        if existing.manifest.asset != scope:
            raise HistoricalArchiveConflictError("output_dir contains another archive scope")
        if existing.manifest.request == request:
            matches.append(existing)
    if len(matches) > 1:
        raise HistoricalArchiveConflictError("output_dir holds multiple receptions for the request")
    return matches[0] if matches else None


def acquire_historical_archive(
    client: CoinbaseExchangeClient,
    request: HistoricalArchiveRequest,
    *,
    output_dir: Path,
    catalog: AssetCatalogService | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> HistoricalArchiveReceipt:
    """Fetch one bounded range, then seal it or reuse an identical archive offline."""
    if not output_dir.is_absolute():
        raise HistoricalArchiveRequestError("output_dir must be an absolute path")
    resolved_output = _validate_output_dir(output_dir)
    scope = resolve_historical_scope(request.asset_id, catalog=catalog)
    validate_archive_range(scope, request)
    reusable = _find_reusable_archive(
        resolved_output,
        scope=scope.asset,
        request=request,
    )
    if reusable is not None:
        return _receipt(reusable, resolved_output, status="reused")
    start, end = request.interval()
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise HistoricalArchiveRequestError("clock must return a timezone-aware instant")
    if end > now.astimezone(UTC):
        raise HistoricalArchiveRequestError("future candle ranges are not allowed")
    try:
        result = client.fetch_daily_candles(scope.product_id, start, end)
    except CoinbaseExchangeError as error:
        raise HistoricalArchiveError(
            f"historical acquisition failed without a seal: {error}"
        ) from error
    if len(result.request_urls) > MAX_ARCHIVE_REQUESTS:
        raise HistoricalArchiveIntegrityError("acquisition exceeded the bounded request count")
    required_pages = (request.requested_days() + MAX_CANDLES_PER_REQUEST - 1) // (
        MAX_CANDLES_PER_REQUEST
    )
    if len(result.request_urls) > required_pages:
        raise HistoricalArchiveIntegrityError("acquisition requested more pages than necessary")
    if result.requested_start != start or result.requested_end != end:
        raise HistoricalArchiveIntegrityError("provider result interval differs from the request")
    candles = tuple(
        _candle_record(scope.asset, candle, retrieved_at=result.retrieved_at)
        for candle in result.candles
    )
    archive = build_historical_archive(
        scope.asset,
        request,
        request_urls=result.request_urls,
        retrieved_at=result.retrieved_at,
        candles=candles,
    )
    return seal_historical_archive(archive, output_dir=resolved_output)


__all__ = [
    "MAX_SEALED_ARCHIVE_BYTES",
    "HistoricalArchiveConflictError",
    "HistoricalArchiveError",
    "HistoricalArchiveIntegrityError",
    "HistoricalArchiveReceipt",
    "HistoricalArchiveRequestError",
    "HistoricalArchiveScopeError",
    "ResolvedHistoricalScope",
    "acquire_historical_archive",
    "build_historical_archive",
    "read_historical_archive",
    "resolve_historical_scope",
    "seal_historical_archive",
    "validate_archive_range",
]
