"""Select supported Form 13F filings from persisted manager Submissions."""

from dataclasses import dataclass
from datetime import UTC, date, datetime

from investment_analyst.evidence.sec_documents.models import (
    INSTITUTIONAL_HOLDINGS_FORMS,
    normalize_cik,
)
from investment_analyst.providers.institutional_holdings.sec_manager_submissions import (
    MANAGER_SUBMISSIONS_SCHEMA_VERSION,
    manager_submissions_source_id,
)


@dataclass(frozen=True, slots=True)
class InstitutionalHoldingsFiling:
    accession: str
    filing_date: date
    report_date: date | None
    accepted_at: datetime
    form: str
    primary_document: str
    manager_name: str


def institutional_holdings_filings(
    record, filer_cik: str
) -> tuple[InstitutionalHoldingsFiling, ...]:
    cik = normalize_cik(filer_cik)
    if (
        record.asset_id is not None
        or record.source.source_id != manager_submissions_source_id(cik)
        or record.schema_version != MANAGER_SUBMISSIONS_SCHEMA_VERSION
    ):
        raise ValueError("invalid manager Submissions record")
    try:
        document = record.payload["document"]
        if normalize_cik(str(document["cik"])) != cik:
            raise ValueError("manager Submissions CIK conflicts")
        manager_name = str(document["name"]).strip()
        recent = document["filings"]["recent"]
        accessions = recent["accessionNumber"]
        forms = recent["form"]
        report_dates = recent.get("reportDate", (None,) * len(accessions))
        rows = zip(
            accessions,
            recent["filingDate"],
            report_dates,
            recent["acceptanceDateTime"],
            forms,
            recent["primaryDocument"],
            strict=True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("malformed manager Submissions") from error
    if not manager_name:
        raise ValueError("malformed manager Submissions")
    filings: list[InstitutionalHoldingsFiling] = []
    for accession, filing_date, report_date, accepted_at, form, primary_document in rows:
        if form not in INSTITUTIONAL_HOLDINGS_FORMS:
            continue
        try:
            if not isinstance(accepted_at, str) or not accepted_at.strip():
                raise ValueError("missing acceptance timestamp")
            accepted = datetime.fromisoformat(accepted_at.replace("Z", "+00:00"))
            if accepted.tzinfo is None or accepted.utcoffset() is None:
                raise ValueError("acceptance timestamp must include timezone")
            accepted = accepted.astimezone(UTC)
            filings.append(
                InstitutionalHoldingsFiling(
                    accession=str(accession),
                    filing_date=date.fromisoformat(str(filing_date)),
                    report_date=_report_date(report_date),
                    accepted_at=accepted,
                    form=str(form),
                    primary_document=str(primary_document),
                    manager_name=manager_name,
                )
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("invalid institutional holdings filing") from error
    return tuple(sorted(filings, key=lambda item: (item.accepted_at, item.accession)))


def _report_date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("report date must be a string")
    return date.fromisoformat(value)
