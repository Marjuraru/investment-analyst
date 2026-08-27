from __future__ import annotations

import socket
import threading
from pathlib import Path
from uuid import uuid4

from investment_analyst.application.runtime_lifecycle import notify_ready


def test_notify_ready_is_a_noop_without_socket(monkeypatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    notify_ready()


def test_notify_ready_writes_systemd_payload(monkeypatch) -> None:
    path = Path("/tmp") / f"ia-{uuid4().hex}.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listener.bind(str(path))
    received: list[bytes] = []
    thread = threading.Thread(target=lambda: received.append(listener.recv(32)))
    thread.start()
    monkeypatch.setenv("NOTIFY_SOCKET", str(path))
    notify_ready()
    thread.join(timeout=1)
    listener.close()
    path.unlink(missing_ok=True)
    assert received == [b"READY=1"]
