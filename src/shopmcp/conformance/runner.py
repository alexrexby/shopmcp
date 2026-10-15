"""Run the conformance checks: against a server built from a config here, on a free local
port, or against a running endpoint (`--url`). The checks themselves do not know which."""

import socket
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from starlette.types import ASGIApp

from shopmcp.config import StoreConfig
from shopmcp.conformance.checks import CheckResult, Expect, run

START_TIMEOUT = 60.0  # seconds: a local start indexes the feed first
# A live catalog API is crawled politely, page by page: a whole read (FEED_SECONDS of the
# feed adapters, 30 min) and the start around it.
ONLINE_START_TIMEOUT = 31 * 60.0


def bound_socket() -> socket.socket:
    """A socket already bound to a free 127.0.0.1 port. Handed to uvicorn as is: asking
    for a free port number and binding it later races with any other process doing the
    same (two test runs side by side)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock


@contextmanager
def serve(app: ASGIApp, start_timeout: float = START_TIMEOUT) -> Iterator[str]:
    """`app` under uvicorn on 127.0.0.1:<free port> in a thread; yields the base URL."""
    import uvicorn

    sock = bound_socket()
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", proxy_headers=False)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + start_timeout
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError(
                    f"the local server did not start in {start_timeout:g} s (see the log"
                    " above; a big catalog takes longer: --start-timeout)"
                )
            time.sleep(0.05)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


@contextmanager
def serve_config(
    cfg: StoreConfig, config_dir: Path, start_timeout: float = START_TIMEOUT
) -> Iterator[str]:
    """The store's own app, index in a temporary directory; yields the MCP endpoint URL.

    127.0.0.1 is added to allowed_hosts: a production config may list only its public host.
    """
    from shopmcp.server.app import build_app

    server = cfg.server.model_copy(
        update={"allowed_hosts": [*cfg.server.allowed_hosts, "127.0.0.1:*"]}
    )
    local = cfg.model_copy(update={"server": server})
    with tempfile.TemporaryDirectory(prefix=f"shopmcp-check-{cfg.id}-") as data:
        app = build_app(local, data_dir=Path(data), config_dir=config_dir)
        with serve(app, start_timeout) as base:
            yield f"{base}{cfg.server.path}"


def check_config(
    cfg: StoreConfig, config_dir: Path, start_timeout: float = START_TIMEOUT
) -> list[CheckResult]:
    with serve_config(cfg, config_dir, start_timeout) as url:
        return run(url, Expect.from_config(cfg), local=True)


def check_url(url: str, cfg: StoreConfig | None = None) -> list[CheckResult]:
    return run(url, Expect.from_config(cfg) if cfg else Expect(), local=False)


def report(results: Sequence[CheckResult]) -> str:
    width = max(len(r.title) for r in results)
    lines = [f"{r.id:<4} {r.status:<4} {r.title:<{width}}  {r.detail}" for r in results]
    failed = sum(r.status == "fail" for r in results)
    skipped = sum(r.status == "skip" for r in results)
    lines.append(f"{len(results) - failed - skipped} ok, {failed} failed, {skipped} skipped")
    return "\n".join(lines)
