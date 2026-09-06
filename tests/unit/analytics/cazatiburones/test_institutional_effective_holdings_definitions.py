from investment_analyst.analytics.cazatiburones import (
    institutional_effective_holdings_definitions as definitions,
)


def test_effective_holdings_policy_version_is_exact() -> None:
    assert (
        definitions.SEC_13F_EFFECTIVE_PUBLIC_HOLDINGS_POLICY_VERSION
        == "sec-13f-effective-public-holdings-policy-v1"
    )
