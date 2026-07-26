#!/usr/bin/env python3
"""Probe official SMV/BVL phase-zero sources without persisting their contents."""

import json
import sys

from investment_analyst.providers.http import HttpRequestError, UrlLibHttpTransport
from investment_analyst.providers.peru.official_sources import (
    PeruOfficialSourceProbe,
    PeruOfficialSourceProbeError,
)


def main() -> int:
    """Print a bounded, body-free JSON availability report."""
    try:
        report = PeruOfficialSourceProbe(UrlLibHttpTransport()).run()
    except (HttpRequestError, PeruOfficialSourceProbeError, ValueError) as error:
        print(f"Peru official-source probe failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
