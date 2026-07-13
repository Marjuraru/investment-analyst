"""Shared validation primitives for auditable data contracts."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def _normalize_datetime_to_utc(value: datetime) -> datetime:
    """Reject naive datetimes and normalize aware values to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include timezone information")
    return value.astimezone(UTC)


UTCDateTime = Annotated[datetime, AfterValidator(_normalize_datetime_to_utc)]
NonEmptyStr = Annotated[str, Field(min_length=1)]


class ContractModel(BaseModel):
    """Base model with strict fields, trimmed strings, and assignment validation."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )
