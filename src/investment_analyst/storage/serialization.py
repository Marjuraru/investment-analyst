"""Canonical JSON serialization helpers for Pydantic models."""

import hashlib
import json
import math
from collections.abc import Iterator
from decimal import Decimal
from typing import BinaryIO

from pydantic import BaseModel


def _reject_non_finite(value: object) -> None:
    """Reject non-finite numbers before JSON conversion can turn them into strings."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("canonical JSON does not allow non-finite Decimal values")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON does not allow NaN or Infinity")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_non_finite(key)
            _reject_non_finite(item)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _reject_non_finite(item)


def canonical_json_bytes(model: BaseModel) -> bytes:
    """Serialize a model to reproducible compact UTF-8 JSON."""
    _reject_non_finite(model.model_dump(mode="python"))
    document = model.model_dump(mode="json", round_trip=True)
    text = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return text.encode("utf-8")


def canonical_json_text(model: BaseModel) -> str:
    """Serialize a model to reproducible canonical JSON text."""
    return canonical_json_bytes(model).decode("utf-8")


def model_from_json[ModelT: BaseModel](model_type: type[ModelT], data: bytes | str) -> ModelT:
    """Reconstruct and validate a Pydantic model from JSON."""
    return model_type.model_validate_json(data)


def sha256_hex(data: bytes) -> str:
    """Calculate a lowercase SHA-256 digest for stored bytes."""
    return hashlib.sha256(data).hexdigest()


def canonical_json_chunks(model: BaseModel) -> Iterator[bytes]:
    """Same canonical document, bounded UTF-8 writes; one model's heap is not bounded.

    Pydantic's JSON projection and the standard encoder preserve the ordinary
    round-trip/number contract. A large encoder token may occupy model-sized
    memory; it is never encoded or written as one unbounded bytes buffer.
    """
    _reject_non_finite(model.model_dump(mode="python"))
    encoder = json.JSONEncoder(
        allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    for token in encoder.iterencode(model.model_dump(mode="json", round_trip=True)):
        for offset in range(0, len(token), 262_144):
            yield token[offset : offset + 262_144].encode("utf-8")


def canonical_json_to_stream(model: BaseModel, stream: BinaryIO) -> tuple[str, int]:
    """Spool one canonical model and return its actual incremental SHA/length."""
    digest = hashlib.sha256()
    length = 0
    for chunk in canonical_json_chunks(model):
        if stream.write(chunk) != len(chunk):
            raise OSError("canonical spool write was incomplete")
        digest.update(chunk)
        length += len(chunk)
    return digest.hexdigest(), length


def canonical_json_digest(model: BaseModel) -> str:
    """Hash one canonical model without accumulating its serialized bytes."""
    digest = hashlib.sha256()
    for chunk in canonical_json_chunks(model):
        digest.update(chunk)
    return digest.hexdigest()
