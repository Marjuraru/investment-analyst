"""Content-addressed, append-only evidence prefixes for daily market bars."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import DataFrequency, DataQuality

DAILY_EVIDENCE_POLICY = "market-daily-evidence-prefix-v1"
_PREFIX_ID_LABEL = "market-daily-evidence-prefix-id-v1"


class DailyEvidenceError(ValueError):
    """Raised when a daily evidence prefix is incomplete or inconsistent."""


class DailyEvidenceFieldGroup(StrEnum):
    """Fields that share one daily evidence chain."""

    CLOSE = "close"
    HIGH_LOW_CLOSE = "high_low_close"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _uuid8(digest_hex: str) -> UUID:
    raw = bytearray(bytes.fromhex(digest_hex)[:16])
    raw[6] = (raw[6] & 0x0F) | 0x80
    raw[8] = (raw[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(raw))


def observation_rows_digest(rows: Sequence[Sequence[str | None]]) -> str:
    """Digest ordered typed observation projections without hydrating domain models."""
    if not rows:
        raise DailyEvidenceError("daily bar evidence requires observation rows")
    if any(not row for row in rows):
        raise DailyEvidenceError("daily observation projection rows must not be empty")
    return _sha256([list(row) for row in rows])


def _prefix_preimage(
    *,
    asset_id: str,
    source_id: str,
    field_group: DailyEvidenceFieldGroup,
    timestamp: datetime,
    observation_ids: Sequence[UUID],
    observation_digest: str,
    parent_prefix_id: UUID | None,
    parent_hash: str | None,
    length: int,
    available_at: datetime,
    quality: DataQuality,
) -> dict[str, object]:
    return {
        "asset_id": asset_id,
        "available_at": timestamp_text(available_at),
        "field_group": field_group.value,
        "frequency": DataFrequency.DAY_1.value,
        "length": length,
        "observation_digest": observation_digest,
        "observation_ids": [str(item) for item in observation_ids],
        "parent_hash": parent_hash,
        "parent_prefix_id": str(parent_prefix_id) if parent_prefix_id is not None else None,
        "policy_version": DAILY_EVIDENCE_POLICY,
        "quality": quality.value,
        "source_id": source_id,
        "timestamp": timestamp_text(timestamp),
    }


def timestamp_text(value: datetime) -> str:
    """Return a timezone-aware timestamp as canonical UTC text."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise DailyEvidenceError("daily evidence timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def daily_evidence_identity(prefix_hash: str) -> UUID:
    """Return the domain-separated UUIDv8 identity for one prefix node."""
    if len(prefix_hash) != 64 or any(c not in "0123456789abcdef" for c in prefix_hash):
        raise DailyEvidenceError("daily evidence prefix hash must be lowercase SHA-256")
    return _uuid8(_sha256({"label": _PREFIX_ID_LABEL, "prefix_hash": prefix_hash}))


class DailyEvidencePrefix(ContractModel):
    """One content-addressed node in a daily field-group evidence chain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prefix_id: UUID
    policy_version: str = DAILY_EVIDENCE_POLICY
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    frequency: DataFrequency = DataFrequency.DAY_1
    field_group: DailyEvidenceFieldGroup
    timestamp: UTCDateTime
    observation_ids: tuple[UUID, ...] = Field(min_length=1, max_length=3)
    observation_digest: NonEmptyStr
    parent_prefix_id: UUID | None = None
    parent_hash: NonEmptyStr | None = None
    length: int = Field(ge=1)
    available_at: UTCDateTime
    quality: DataQuality
    prefix_hash: NonEmptyStr

    @model_validator(mode="after")
    def validate_content_address(self) -> DailyEvidencePrefix:
        """Verify local shape and the deterministic chain-node identity."""
        if self.frequency is not DataFrequency.DAY_1:
            raise ValueError("daily evidence prefixes require DAY_1 frequency")
        expected_fields = 1 if self.field_group is DailyEvidenceFieldGroup.CLOSE else 3
        if len(self.observation_ids) != expected_fields:
            raise ValueError("observation ID count does not match the daily field group")
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("daily evidence observation IDs must be unique")
        for label, value in (
            ("observation_digest", self.observation_digest),
            ("prefix_hash", self.prefix_hash),
        ):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError(f"{label} must be lowercase SHA-256")
        if (self.parent_prefix_id is None) != (self.parent_hash is None):
            raise ValueError("daily evidence parent ID and hash must be supplied together")
        if self.parent_prefix_id is None:
            if self.length != 1:
                raise ValueError("daily evidence root must have length one")
        elif self.length < 2 or self.parent_prefix_id == self.prefix_id:
            raise ValueError("daily evidence child must follow a distinct parent")
        if self.available_at < self.timestamp:
            raise ValueError("daily evidence must not be available before its bar timestamp")
        preimage = _prefix_preimage(
            asset_id=self.asset_id,
            source_id=self.source_id,
            field_group=self.field_group,
            timestamp=self.timestamp,
            observation_ids=self.observation_ids,
            observation_digest=self.observation_digest,
            parent_prefix_id=self.parent_prefix_id,
            parent_hash=self.parent_hash,
            length=self.length,
            available_at=self.available_at,
            quality=self.quality,
        )
        expected_hash = _sha256({"label": DAILY_EVIDENCE_POLICY, **preimage})
        if self.prefix_hash != expected_hash:
            raise ValueError("daily evidence prefix hash does not match its content")
        if self.prefix_id != daily_evidence_identity(expected_hash):
            raise ValueError("daily evidence prefix ID does not match its content")
        return self


def make_daily_evidence_prefix(
    *,
    asset_id: str,
    source_id: str,
    field_group: DailyEvidenceFieldGroup,
    timestamp: datetime,
    observation_ids: Sequence[UUID],
    observation_digest: str,
    current_available_at: datetime,
    quality: DataQuality,
    parent: DailyEvidencePrefix | None = None,
) -> DailyEvidencePrefix:
    """Create a root or append one bar to an existing evidence chain."""
    if parent is not None:
        if (
            parent.asset_id != asset_id
            or parent.source_id != source_id
            or parent.field_group is not field_group
            or parent.frequency is not DataFrequency.DAY_1
        ):
            raise DailyEvidenceError("daily evidence parent is outside the requested scope")
        if timestamp <= parent.timestamp:
            raise DailyEvidenceError("daily evidence timestamps must advance strictly")
    if current_available_at < timestamp:
        raise DailyEvidenceError("bar evidence must not be available before its timestamp")
    available_at = max(
        current_available_at,
        parent.available_at if parent is not None else current_available_at,
    )
    cumulative_quality = _combined_quality(parent.quality, quality) if parent else quality
    ids = tuple(observation_ids)
    length = parent.length + 1 if parent is not None else 1
    parent_id = parent.prefix_id if parent is not None else None
    parent_hash = parent.prefix_hash if parent is not None else None
    preimage = _prefix_preimage(
        asset_id=asset_id,
        source_id=source_id,
        field_group=field_group,
        timestamp=timestamp,
        observation_ids=ids,
        observation_digest=observation_digest,
        parent_prefix_id=parent_id,
        parent_hash=parent_hash,
        length=length,
        available_at=available_at,
        quality=cumulative_quality,
    )
    prefix_hash = _sha256({"label": DAILY_EVIDENCE_POLICY, **preimage})
    return DailyEvidencePrefix(
        prefix_id=daily_evidence_identity(prefix_hash),
        asset_id=asset_id,
        source_id=source_id,
        field_group=field_group,
        timestamp=timestamp,
        observation_ids=ids,
        observation_digest=observation_digest,
        parent_prefix_id=parent_id,
        parent_hash=parent_hash,
        length=length,
        available_at=available_at,
        quality=cumulative_quality,
        prefix_hash=prefix_hash,
    )


def _combined_quality(left: DataQuality, right: DataQuality) -> DataQuality:
    """Propagate the strictest quality through one recurrence chain."""
    for candidate in (
        DataQuality.SUSPECT,
        DataQuality.PARTIAL,
        DataQuality.DELAYED,
        DataQuality.VALID,
    ):
        if candidate in (left, right):
            return candidate
    raise DailyEvidenceError("daily evidence has an unknown quality")


def verify_daily_evidence_chain(
    prefixes: Sequence[DailyEvidencePrefix],
) -> DailyEvidencePrefix:
    """Verify one ordered prefix chain iteratively and return its tip."""
    if not prefixes:
        raise DailyEvidenceError("daily evidence chain must not be empty")
    seen: set[UUID] = set()
    previous: DailyEvidencePrefix | None = None
    for prefix in prefixes:
        if prefix.prefix_id in seen:
            raise DailyEvidenceError("daily evidence chain contains a cycle or duplicate node")
        seen.add(prefix.prefix_id)
        if previous is None:
            if prefix.parent_prefix_id is not None or prefix.length != 1:
                raise DailyEvidenceError("daily evidence chain does not begin at its root")
        elif (
            prefix.parent_prefix_id != previous.prefix_id
            or prefix.parent_hash != previous.prefix_hash
            or prefix.length != previous.length + 1
            or prefix.asset_id != previous.asset_id
            or prefix.source_id != previous.source_id
            or prefix.field_group is not previous.field_group
            or prefix.timestamp <= previous.timestamp
            or prefix.available_at < previous.available_at
        ):
            raise DailyEvidenceError("daily evidence chain has a broken parent or scope")
        previous = prefix
    if previous is None:
        raise DailyEvidenceError("daily evidence chain must not be empty")
    return previous


__all__ = [
    "DAILY_EVIDENCE_POLICY",
    "DailyEvidenceError",
    "DailyEvidenceFieldGroup",
    "DailyEvidencePrefix",
    "daily_evidence_identity",
    "make_daily_evidence_prefix",
    "observation_rows_digest",
    "verify_daily_evidence_chain",
]
