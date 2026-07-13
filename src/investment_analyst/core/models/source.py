"""Source definitions and record-level source references."""

from typing import Annotated

from pydantic import Field

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import SourceType

Sha256Checksum = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{64}$")]


class SourceDefinition(ContractModel):
    """Provider-independent description of an external dataset."""

    source_id: NonEmptyStr
    provider_name: NonEmptyStr
    dataset_name: NonEmptyStr
    source_type: SourceType
    base_url: NonEmptyStr | None = None
    is_official: bool
    coverage_notes: NonEmptyStr | None = None


class SourceReference(ContractModel):
    """Traceable reference to the source of a specific record or value."""

    source_id: NonEmptyStr
    record_key: NonEmptyStr | None = None
    retrieved_at: UTCDateTime
    raw_uri: NonEmptyStr | None = None
    checksum_sha256: Sha256Checksum | None = None
