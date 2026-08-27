"""Small, dependency-free lifecycle primitives for the local service."""

from __future__ import annotations

import os
import socket
import time
import urllib.error
import urllib.request


def wait_for_overview_ready(port: int, *, deadline_seconds: float = 120.0) -> None:
    """Require a loopback overview response before enabling background work."""
    deadline = time.monotonic() + deadline_seconds
    url = f"http://127.0.0.1:{port}/api/v1/overview"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("local overview did not become ready before startup deadline")
        try:
            with urllib.request.urlopen(url, timeout=min(remaining, 2.0)) as response:  # noqa: S310
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(min(0.05, remaining))


def notify_ready() -> None:
    """Send systemd readiness when the caller configured a Unix notify socket."""
    value = os.environ.get("NOTIFY_SOCKET")
    if not value:
        return
    address = "\0" + value[1:] if value.startswith("@") else value
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        client.connect(address)
        client.sendall(b"READY=1")
