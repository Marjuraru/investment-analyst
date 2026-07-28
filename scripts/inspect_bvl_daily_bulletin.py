#!/usr/bin/env python3
"""Inspect selected quotations from the official BVL daily bulletin."""

import argparse
import json
import sys
from collections.abc import Sequence

from investment_analyst.providers.http import HttpRequestError, UrlLibHttpTransport
from investment_analyst.providers.peru.bvl_daily_bulletin import (
    DEFAULT_WATCHLIST,
    BvlDailyBulletinError,
    BvlDailyBulletinReader,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Lee cotizaciones seleccionadas del boletín diario oficial BVL sin persistir datos."
        )
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=DEFAULT_WATCHLIST,
        metavar="NEMONICO",
        help="nemónicos BVL; por defecto usa la lista inicial documentada",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Print a body-free JSON inspection report."""
    arguments = _parser().parse_args(argv)
    try:
        report = BvlDailyBulletinReader(UrlLibHttpTransport()).inspect(arguments.symbols)
    except (BvlDailyBulletinError, HttpRequestError, ValueError) as error:
        print(f"BVL daily bulletin inspection failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
