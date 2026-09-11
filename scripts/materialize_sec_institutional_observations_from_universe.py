#!/usr/bin/env python3
"""Materializa observaciones institucionales PIT desde el universo oficial de gestores 13F.

Resuelve el snapshot más reciente del universo `sec-13f-manager-universe-v1` disponible al corte,
procesa la misma página determinista de gestores que `SEC-CORPUS-28`, prueba una correspondencia
row-scoped por cada coincidencia exacta de CUSIP contra la evidencia 13F ya persistida y completa
las observaciones institucionales para cada activo de la página. No realiza red y abre el workspace
una sola vez. `known-at` es el único dato temporal: CIK, activo, CUSIP, report ID y fila no son
libres.
"""

import argparse
import json
import sys
from datetime import UTC, datetime

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.sec_institutional_observation_materialization import (
    SecInstitutionalObservationMaterializationApplication,
)
from investment_analyst.application.sec_institutional_observation_materialization_models import (
    MAX_MATERIALIZATION_MANAGERS_PER_PAGE,
    SecInstitutionalObservationMaterializationRequest,
)


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
        "--manager-limit", type=_bounded(1, MAX_MATERIALIZATION_MANAGERS_PER_PAGE), default=1
    )
    return parser


def main() -> int:
    """Run one directed materialization page and print its compact audit summary."""
    arguments = _parser().parse_args()
    try:
        result = SecInstitutionalObservationMaterializationApplication.create_default().materialize(
            SecInstitutionalObservationMaterializationRequest(
                known_at=arguments.known_at,
                manager_offset=arguments.manager_offset,
                manager_limit=arguments.manager_limit,
            ),
            location=storage_location_from_namespace(arguments),
        )
    except Exception as error:
        print(f"SEC institutional observation materialization failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result.to_json_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
