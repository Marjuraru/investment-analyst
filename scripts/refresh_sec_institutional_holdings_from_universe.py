#!/usr/bin/env python3
"""Adquiere filings 13F dirigidos y reanudables desde el universo oficial de gestores.

Resuelve el snapshot más reciente del universo `sec-13f-manager-universe-v1` disponible al corte,
procesa una página determinista de gestores ya seleccionados, importa sólo los accessions 13F
pendientes del período de reporte exacto descubierto en Submissions oficial fresco y completa la
semántica integrada al mismo corte. La identidad configurada en `SEC_USER_AGENT` nunca se imprime
ni se persiste.
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.sec_institutional_holdings_refresh import (
    SecInstitutionalHoldingsDirectedRefreshApplication,
)
from investment_analyst.application.sec_institutional_holdings_refresh_models import (
    MAX_DIRECTED_ACCESSIONS_PER_MANAGER,
    MAX_DIRECTED_MANAGERS_PER_PAGE,
    SecInstitutionalHoldingsDirectedRefreshRequest,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity


def _aware_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be a valid ISO 8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone information")
    return parsed.astimezone(UTC)


def _bounded(minimum: int, maximum: int):
    def convert(value: str) -> int:
        try:
            number = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError("value must be an integer") from error
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"value must be between {minimum} and {maximum}")
        return number

    return convert


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    parser.add_argument("--known-at", required=True, type=_aware_datetime)
    parser.add_argument("--manager-offset", type=_bounded(0, 1_000_000), default=0)
    parser.add_argument(
        "--manager-limit", type=_bounded(1, MAX_DIRECTED_MANAGERS_PER_PAGE), default=1
    )
    parser.add_argument(
        "--accessions-per-manager",
        type=_bounded(1, MAX_DIRECTED_ACCESSIONS_PER_MANAGER),
        default=1,
    )
    return parser


def main() -> int:
    """Run one bounded directed 13F page and print its compact audit summary."""
    arguments = _parser().parse_args()
    user_agent = os.environ.get("SEC_USER_AGENT", "")
    if not user_agent.strip():
        print("SEC_USER_AGENT is required and was not provided.", file=sys.stderr)
        return 2
    try:
        result = SecInstitutionalHoldingsDirectedRefreshApplication.create_default().refresh(
            request=SecInstitutionalHoldingsDirectedRefreshRequest(
                known_at=arguments.known_at,
                manager_offset=arguments.manager_offset,
                manager_limit=arguments.manager_limit,
                accessions_per_manager=arguments.accessions_per_manager,
            ),
            location=storage_location_from_namespace(arguments),
            sec_identity=SecEdgarIdentity(user_agent),
        )
    except Exception as error:
        print(f"SEC directed 13F refresh failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result.to_json_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
