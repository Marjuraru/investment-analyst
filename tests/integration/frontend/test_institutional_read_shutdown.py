"""Stop the local HTTP server while an institutional read is in progress."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from investment_analyst.frontend import local_web as local_web_module

_CHILD = r"""
import json
import signal
import sys
import threading
import time

from investment_analyst.frontend.local_web import AaplLocalHttpServer

class FakeApplication:
    def cazatiburones_institutional_observations(self, parameters):
        print("READING", flush=True)
        time.sleep(2.0)
        return {
            "observations": [],
            "total_matching": 0,
            "truncated": False,
        }

server = AaplLocalHttpServer(("127.0.0.1", 0), FakeApplication())
stop_event = threading.Event()

def request_shutdown(signum, frame):
    del signum, frame
    stop_event.set()
    threading.Thread(target=server.shutdown, daemon=True).start()

signal.signal(signal.SIGTERM, request_shutdown)
signal.signal(signal.SIGINT, request_shutdown)
server_thread = threading.Thread(target=server.serve_forever, daemon=True)
server_thread.start()
print(f"PORT={server.server_address[1]}", flush=True)
server_thread.join()
server.server_close()
print("STOPPED", flush=True)
"""


def _start_child() -> subprocess.Popen[str]:
    environment = os.environ.copy()
    source_root = str(Path(local_web_module.__file__).parents[2])
    environment["PYTHONPATH"] = (
        f"{source_root}{os.pathsep}{environment['PYTHONPATH']}"
        if environment.get("PYTHONPATH")
        else source_root
    )
    return subprocess.Popen(
        [sys.executable, "-u", "-c", _CHILD],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
    )


def _lines(process: subprocess.Popen[str]) -> queue.Queue[str]:
    output: queue.Queue[str] = queue.Queue()

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            output.put(line.rstrip())
        output.put("EOF")

    threading.Thread(target=read, daemon=True).start()
    return output


def _wait_for(output: queue.Queue[str], expected: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"child did not emit {expected!r}")
        try:
            line = output.get(timeout=remaining)
        except queue.Empty as error:
            raise AssertionError(f"child did not emit {expected!r}") from error
        if line == expected or line.startswith(expected):
            return


def _port(output: queue.Queue[str]) -> int:
    deadline = time.monotonic() + 10
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("child did not publish a port")
        try:
            line = output.get(timeout=remaining)
        except queue.Empty as error:
            raise AssertionError("child did not publish a port") from error
        if line.startswith("PORT="):
            return int(line.removeprefix("PORT="))


def test_server_stops_while_an_institutional_read_is_in_progress() -> None:
    process = _start_child()
    output = _lines(process)
    try:
        port = _port(output)
        request_error: list[BaseException] = []

        def request() -> None:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}"
                    "/api/v1/cazatiburones/institutional-observations"
                    "?asset_id=equity%3Aus%3Aaapl&known_at=2026-11-09T21%3A30%3A00Z"
                    "&projection=summary",
                    timeout=10,
                ).read()
            except BaseException as error:  # noqa: BLE001 - the child may close the socket
                request_error.append(error)

        threading.Thread(target=request, daemon=True).start()
        _wait_for(output, "READING", timeout=10)
        process.send_signal(15)
        _wait_for(output, "STOPPED", timeout=10)
        process.wait(timeout=5)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_server_stops_while_idle() -> None:
    process = _start_child()
    output = _lines(process)
    try:
        _port(output)
        process.send_signal(15)
        _wait_for(output, "STOPPED", timeout=10)
        process.wait(timeout=5)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
