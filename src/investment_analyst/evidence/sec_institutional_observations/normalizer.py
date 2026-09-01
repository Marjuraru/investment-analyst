"""Pure as-filed 13F row projection; no storage, XML, or network access."""

import json
from datetime import UTC, datetime, time
from decimal import Decimal

from investment_analyst.core.models import NormalizedObservation, SourceReference
from investment_analyst.core.models.enums import DataFrequency, DataQuality
from investment_analyst.evidence.instrument_correspondence.models import InstrumentCorrespondence
from investment_analyst.evidence.sec_institutional_semantics.models import (
    InstitutionalHoldingsSemantics,
    InstitutionalSemanticsRow,
)

from .definitions import SOURCE_ID, TRANSFORMATION_VERSION, monetary_value
from .identity import observation_id


def normalize_row(
    item: InstitutionalHoldingsSemantics,
    row: InstitutionalSemanticsRow,
    correspondence: InstrumentCorrespondence,
    *,
    normalized_at: datetime,
) -> tuple[NormalizedObservation, ...]:
    if item.report_period is None:
        return ()
    if normalized_at.tzinfo is None or normalized_at < max(
        item.available_at, correspondence.available_at
    ):
        raise ValueError("normalized_at must be UTC and not precede available evidence")
    available_at = max(item.available_at, correspondence.available_at)
    value, quality = monetary_value(row.value_as_reported, available_at=item.available_at)
    option = row.put_call.upper() if row.put_call is not None else None
    if option not in {None, "PUT", "CALL"}:
        return ()
    fields: list[tuple[str, Decimal, str]] = [
        (
            "institutional_option_fair_value" if option else "institutional_reported_fair_value",
            value,
            "USD",
        )
    ]
    if row.quantity_type == "SH":
        fields.append(
            (
                "institutional_option_underlying_shares"
                if option
                else "institutional_reported_shares",
                row.quantity,
                "shares",
            )
        )
    elif row.quantity_type == "PRN":
        fields.append(
            (
                "institutional_reported_principal_amount",
                row.quantity,
                "sec_13f_principal_as_reported",
            )
        )
    elif row.quantity_type is not None:
        fields = fields
    period_end = datetime.combine(item.report_period, time.min, tzinfo=UTC)
    key_base = {
        "artifact_id": str(item.artifact_id),
        "row_id": str(row.row_id),
        "correspondence_id": str(correspondence.correspondence_id),
        "cover_revision_id": str(item.cover_revision.revision_id),
        "information_table_revision_id": str(item.information_table_revision.revision_id),
        "cusip": row.cusip,
        "title_of_class": row.title_of_class,
        "put_call": row.put_call,
        "quantity_type": row.quantity_type,
        "transformation_version": TRANSFORMATION_VERSION,
    }
    return tuple(
        NormalizedObservation(
            observation_id=observation_id(
                item.artifact_id, row.row_id, correspondence.correspondence_id, field
            ),
            raw_record_id=item.raw_record_id,
            asset_id=correspondence.asset_id,
            field_name=field,
            value=amount,
            unit=unit,
            frequency=DataFrequency.QUARTERLY,
            period_end=period_end,
            available_at=available_at,
            normalized_at=normalized_at.astimezone(UTC),
            source=SourceReference(
                source_id=SOURCE_ID,
                record_key=json.dumps(
                    {**key_base, "field_name": field}, sort_keys=True, separators=(",", ":")
                ),
                retrieved_at=item.parsed_at,
                raw_uri=item.cover_revision.source_url,
                checksum_sha256=item.cover_revision.content_sha256,
            ),
            quality=DataQuality(quality),
            transformation_version=TRANSFORMATION_VERSION,
        )
        for field, amount, unit in fields
    )
