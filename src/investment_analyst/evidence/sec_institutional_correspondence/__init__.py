"""Isolated, row-scoped 13F correspondence evidence.

This module proves that the exact CUSIP of one as-filed information-table row already matches the
catalog CUSIP of a manager-universe candidate for the same reported period. It never establishes a
perpetual corporate identity, never extends its own validity window and never replaces the manual
``instrument-correspondence-v1`` declaration.
"""

from investment_analyst.evidence.sec_institutional_correspondence.models import (
    ROW_CORRESPONDENCE_POLICY_VERSION,
    ROW_CORRESPONDENCE_SCHEMA_VERSION,
    ROW_CORRESPONDENCE_SOURCE_ID,
    SecInstitutionalRowCorrespondence,
)
from investment_analyst.evidence.sec_institutional_correspondence.repository import (
    SecInstitutionalRowCorrespondenceRepository,
    SecInstitutionalRowCorrespondenceRepositoryError,
    row_correspondence_from_raw_record,
    row_correspondence_to_raw_record,
    verify_sec_institutional_row_correspondence_records,
)
from investment_analyst.evidence.sec_institutional_correspondence.service import (
    RowCorrespondenceResolution,
    SecInstitutionalRowCorrespondenceError,
    SecInstitutionalRowCorrespondenceService,
)

__all__ = [
    "ROW_CORRESPONDENCE_POLICY_VERSION",
    "ROW_CORRESPONDENCE_SCHEMA_VERSION",
    "ROW_CORRESPONDENCE_SOURCE_ID",
    "RowCorrespondenceResolution",
    "SecInstitutionalRowCorrespondence",
    "SecInstitutionalRowCorrespondenceError",
    "SecInstitutionalRowCorrespondenceRepository",
    "SecInstitutionalRowCorrespondenceRepositoryError",
    "SecInstitutionalRowCorrespondenceService",
    "row_correspondence_from_raw_record",
    "row_correspondence_to_raw_record",
    "verify_sec_institutional_row_correspondence_records",
]
