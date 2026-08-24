from investment_analyst.core.models import SourceDefinition, SourceType


def test_documents_is_an_explicit_source_type() -> None:
    source = SourceDefinition(
        source_id="sec-edgar:primary-documents",
        provider_name="U.S. Securities and Exchange Commission",
        dataset_name="EDGAR primary filing documents",
        source_type=SourceType.DOCUMENTS,
        base_url="https://www.sec.gov",
        is_official=True,
    )

    assert source.source_type is SourceType.DOCUMENTS
