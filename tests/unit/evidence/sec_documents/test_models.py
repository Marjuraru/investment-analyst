import hashlib
import json
from datetime import UTC, date, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from investment_analyst.evidence.sec_documents.models import (
    SecDocumentAcquisitionRevision,
    SecDocumentMetadataRevision,
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
    SecTerminalScriptDifference,
    create_sec_terminal_script_difference,
    sec_document_metadata_sha256,
    verify_sec_terminal_script_difference,
)


def _document() -> SecLogicalDocument:
    filing = SecFiling(
        filing_id=SecFiling.expected_id("320193", "0000320193-25-000001"),
        filer_cik="320193",
        accession="0000320193-25-000001",
        form="10-K",
        filing_date=date(2025, 1, 31),
        report_date=date(2024, 12, 31),
        accepted_at=datetime(2025, 1, 31, 18, tzinfo=UTC),
        is_amendment=False,
    )
    return SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, "annual.htm"),
        filing=filing,
        name="annual.htm",
    )


def test_document_and_revision_ids_are_deterministic_and_separate() -> None:
    document = _document()
    checksum = "a" * 64
    revision_id = SecDocumentRevision.expected_id(
        document.document_id, checksum, "sec-document-revision-v1"
    )
    revision = SecDocumentRevision(
        revision_id=revision_id,
        asset_id="equity:us:aapl",
        document=document,
        raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=SecDocumentRevision.expected_raw_record_id(
            document.filing.filing_id
        ),
        content_sha256=checksum,
        content_size_bytes=3,
        available_at=datetime(2025, 2, 1, tzinfo=UTC),
        retrieved_at=datetime(2025, 2, 1, tzinfo=UTC),
        source_url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000001/annual.htm",
    )

    assert document.document_id != revision.revision_id
    assert revision.revision_id != revision.raw_record_id
    assert revision.document.filing.filer_cik == "0000320193"


def test_revision_rejects_backdated_availability_and_invalid_primary_path() -> None:
    document = _document()
    with pytest.raises(ValueError, match="available_at"):
        SecDocumentRevision(
            revision_id=SecDocumentRevision.expected_id(
                document.document_id, "b" * 64, "sec-document-revision-v1"
            ),
            asset_id="equity:us:aapl",
            document=document,
            raw_record_id=SecDocumentRevision.expected_raw_record_id(
                SecDocumentRevision.expected_id(
                    document.document_id, "b" * 64, "sec-document-revision-v1"
                )
            ),
            discovery_raw_record_id=document.filing.filing_id,
            content_sha256="b" * 64,
            content_size_bytes=1,
            available_at=datetime(2025, 1, 1, tzinfo=UTC),
            retrieved_at=datetime(2025, 1, 2, tzinfo=UTC),
            source_url="https://www.sec.gov/Archives/x",
        )
    with pytest.raises(ValueError, match="primary document name"):
        SecLogicalDocument(
            document_id=document.document_id,
            filing=document.filing,
            name="../annual.htm",
        )


def test_v2_revision_uses_filing_acceptance_independently_of_retrieval() -> None:
    document = _document()
    revision_id = SecDocumentRevision.expected_id(
        document.document_id, "c" * 64, "sec-document-revision-v2"
    )
    revision = SecDocumentRevision(
        revision_id=revision_id,
        asset_id="equity:us:aapl",
        document=document,
        raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=document.filing.filing_id,
        content_sha256="c" * 64,
        content_size_bytes=1,
        available_at=document.filing.accepted_at,
        retrieved_at=datetime(2025, 2, 2, tzinfo=UTC),
        source_url="https://www.sec.gov/Archives/x",
        revision_schema_version="sec-document-revision-v2",
    )

    assert revision.available_at != revision.retrieved_at

    with pytest.raises(ValueError, match="SEC filing acceptance"):
        SecDocumentRevision(**{**revision.model_dump(), "available_at": revision.retrieved_at})


def test_metadata_revision_identity_hashes_full_utc_metadata_and_prior() -> None:
    document = _document()
    prior_id = UUID("11111111-1111-4111-8111-111111111111")
    content_sha256 = "d" * 64
    metadata_sha256 = sec_document_metadata_sha256(document)
    identity = {
        "schema_version": "sec-document-revision-v3",
        "document_id": str(document.document_id),
        "content_sha256": content_sha256,
        "metadata_sha256": metadata_sha256,
        "prior_revision_id": str(prior_id),
    }
    canonical = json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    revision_id = uuid5(
        uuid5(NAMESPACE_URL, "investment-analyst:sec-document-metadata-revision:v1"),
        canonical,
    )
    retrieved_at = datetime(2025, 2, 4, tzinfo=UTC)
    revision = SecDocumentMetadataRevision(
        revision_id=revision_id,
        asset_id="equity:us:aapl",
        document=document,
        prior_revision_id=prior_id,
        raw_record_id=SecDocumentMetadataRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=UUID("22222222-2222-4222-8222-222222222222"),
        content_sha256=content_sha256,
        content_size_bytes=3,
        metadata_sha256=metadata_sha256,
        metadata_observed_at=datetime(2025, 2, 3, tzinfo=UTC),
        available_at=retrieved_at,
        retrieved_at=retrieved_at,
        source_url="https://www.sec.gov/Archives/x",
    )

    changed_filing = document.filing.model_copy(
        update={"accepted_at": datetime(2025, 1, 31, 19, tzinfo=UTC)}
    )
    changed_document = document.model_copy(update={"filing": changed_filing})
    changed_metadata_sha256 = sec_document_metadata_sha256(changed_document)
    changed_revision_id = SecDocumentMetadataRevision.expected_id(
        document.document_id, content_sha256, changed_metadata_sha256, prior_id
    )

    assert revision.revision_id == revision_id
    assert revision.metadata_sha256 == metadata_sha256
    assert changed_metadata_sha256 != metadata_sha256
    assert changed_revision_id != revision_id


def test_metadata_revision_rejects_unknown_fields_and_incomplete_availability() -> None:
    document = _document()
    prior_id = UUID("33333333-3333-4333-8333-333333333333")
    content_sha256 = "e" * 64
    metadata_sha256 = sec_document_metadata_sha256(document)
    revision_id = SecDocumentMetadataRevision.expected_id(
        document.document_id, content_sha256, metadata_sha256, prior_id
    )
    observed_at = datetime(2025, 2, 3, tzinfo=UTC)
    retrieved_at = datetime(2025, 2, 4, tzinfo=UTC)
    values = {
        "revision_id": revision_id,
        "asset_id": "equity:us:aapl",
        "document": document,
        "prior_revision_id": prior_id,
        "raw_record_id": SecDocumentMetadataRevision.expected_raw_record_id(revision_id),
        "discovery_raw_record_id": UUID("44444444-4444-4444-8444-444444444444"),
        "content_sha256": content_sha256,
        "content_size_bytes": 3,
        "metadata_sha256": metadata_sha256,
        "metadata_observed_at": observed_at,
        "available_at": retrieved_at,
        "retrieved_at": retrieved_at,
        "source_url": "https://www.sec.gov/Archives/x",
    }

    with pytest.raises(ValueError, match="extra"):
        SecDocumentMetadataRevision(**{**values, "unreviewed": True})
    with pytest.raises(ValueError, match="availability"):
        SecDocumentMetadataRevision(
            **{**values, "available_at": datetime(2025, 2, 3, 12, tzinfo=UTC)}
        )


def test_terminal_script_difference_accepts_insert_remove_and_rotated_path() -> None:
    core = b"<html><body>official filing</body></html>\n"
    original = b'<script type="text/javascript"  src="/old/path"></script>'
    rotated = b'<script type="text/javascript"  src="/new/rotated/path-token"></script>'
    old_body = core.replace(b"</body>", original + b"</body>")
    inserted_body = core.replace(b"</body>", rotated + b"</body>")

    replacement = create_sec_terminal_script_difference(
        old_body,
        inserted_body,
        document_name="annual.htm",
    )
    assert replacement.old_script == original.decode("ascii")
    assert replacement.new_script == rotated.decode("ascii")
    assert replacement.insertion_offset == core.index(b"</body>")
    assert replacement.core_size_bytes == len(core)
    verify_sec_terminal_script_difference(
        old_body,
        inserted_body,
        document_name="annual.htm",
        proof=replacement,
    )

    insertion = create_sec_terminal_script_difference(
        core,
        inserted_body,
        document_name="annual.html",
    )
    removal = create_sec_terminal_script_difference(
        inserted_body,
        core,
        document_name="annual.html",
    )
    assert insertion.old_script is None
    assert insertion.new_script == rotated.decode("ascii")
    assert removal.old_script == rotated.decode("ascii")
    assert removal.new_script is None


@pytest.mark.parametrize(
    ("path", "valid"),
    [
        ("/safe/path", True),
        ("/", False),
        ("//host/script.js", False),
        ("/a/../script", False),
        ("/a//script", False),
        ("/a/./script", False),
        ("/a.b/script", False),
        ("https://sec.gov/script", False),
        ("/a?query", False),
        ("/a#fragment", False),
        ("/a\\b", False),
    ],
)
def test_terminal_script_path_grammar(path: str, valid: bool) -> None:
    element = f'<script type="text/javascript"  src="{path}"></script>'
    if valid:
        SecTerminalScriptDifference(
            old_script=None,
            new_script=element,
            insertion_offset=0,
            core_size_bytes=1,
            core_sha256="a" * 64,
        )
    else:
        with pytest.raises(ValueError):
            SecTerminalScriptDifference(
                old_script=None,
                new_script=element,
                insertion_offset=0,
                core_size_bytes=1,
                core_sha256="a" * 64,
            )


def test_terminal_script_difference_rejects_any_other_byte_change_or_bad_position() -> None:
    original = b"<html><body>official filing</body></html>\n"
    script = b'<script type="text/javascript"  src="/safe/file"></script>'
    terminal = original.replace(b"</body>", script + b"</body>")
    with pytest.raises(ValueError, match="differs outside"):
        create_sec_terminal_script_difference(
            original,
            terminal.replace(b"official filing", b"altered filing"),
            document_name="annual.htm",
        )
    with pytest.raises(ValueError, match="HTML"):
        create_sec_terminal_script_difference(
            original,
            terminal,
            document_name="submission.xml",
        )
    with pytest.raises(ValueError, match="does not end"):
        create_sec_terminal_script_difference(
            original,
            terminal.replace(b"</body></html>", b"</html></body>"),
            document_name="annual.htm",
        )
    with pytest.raises(ValueError):
        create_sec_terminal_script_difference(
            original,
            original.replace(b"</body>", script + b"\t</body>"),
            document_name="annual.htm",
        )
    with pytest.raises(ValueError):
        create_sec_terminal_script_difference(
            original,
            original.replace(b"</body>", script + b" " + script + b"</body>"),
            document_name="annual.htm",
        )
    with pytest.raises(ValueError):
        create_sec_terminal_script_difference(
            original,
            original.replace(
                b"</body>",
                b'<script type="text/javascript"  src="/safe/file" defer></script></body>',
            ),
            document_name="annual.htm",
        )
    with pytest.raises(ValueError):
        create_sec_terminal_script_difference(
            original,
            original.replace(b"</body>", b"<script>inline()</script></body>"),
            document_name="annual.htm",
        )


def test_acquisition_revision_uses_full_content_and_prior_hashes_in_identity() -> None:
    document = _document()
    current = document.model_copy(
        update={
            "filing": document.filing.model_copy(
                update={"accepted_at": datetime(2025, 2, 1, 18, tzinfo=UTC)}
            )
        }
    )
    old_content = b"<html><body>official filing</body></html>\n"
    script = b'<script type="text/javascript"  src="/rotated"></script>'
    new_content = old_content.replace(b"</body>", script + b"</body>")
    proof = create_sec_terminal_script_difference(
        old_content,
        new_content,
        document_name=current.name,
    )
    content_sha256 = hashlib.sha256(new_content).hexdigest()
    prior_content_sha256 = hashlib.sha256(old_content).hexdigest()
    metadata_sha256 = sec_document_metadata_sha256(current)
    prior_id = UUID("55555555-5555-4555-8555-555555555555")
    expected = {
        "schema_version": "sec-document-revision-v4",
        "document_id": str(current.document_id),
        "content_sha256": content_sha256,
        "metadata_sha256": metadata_sha256,
        "prior_revision_id": str(prior_id),
        "prior_content_sha256": prior_content_sha256,
    }
    canonical = json.dumps(expected, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    revision_id = uuid5(
        uuid5(NAMESPACE_URL, "investment-analyst:sec-document-acquisition-revision:v1"),
        canonical,
    )
    retrieved_at = datetime(2025, 2, 4, tzinfo=UTC)
    revision = SecDocumentAcquisitionRevision(
        revision_id=revision_id,
        asset_id="equity:us:aapl",
        document=current,
        prior_revision_id=prior_id,
        raw_record_id=SecDocumentAcquisitionRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=UUID("66666666-6666-4666-8666-666666666666"),
        content_sha256=content_sha256,
        content_size_bytes=len(new_content),
        prior_content_sha256=prior_content_sha256,
        metadata_sha256=metadata_sha256,
        metadata_observed_at=datetime(2025, 2, 3, tzinfo=UTC),
        available_at=retrieved_at,
        retrieved_at=retrieved_at,
        source_url="https://www.sec.gov/Archives/x",
        terminal_script_difference=proof,
    )

    assert revision.revision_id == revision_id
    assert revision.content_size_bytes == len(new_content)
    assert (
        revision.terminal_script_difference.core_sha256 == hashlib.sha256(old_content).hexdigest()
    )
