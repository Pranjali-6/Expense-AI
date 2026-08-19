"""Collecting metrics from more than one process.

The API is one process; the workers are several, and Celery's prefork pool adds
more. A ``Counter`` incremented in a forked child lives in that child's memory
and is invisible to whatever is serving ``/metrics`` — so a naive setup exports
a beautiful zero for every extraction that ever ran.

``prometheus_client`` solves this with multiprocess mode: each process writes
its samples to mmap files in a shared directory, and exposition reads the whole
directory. It is enabled by setting ``PROMETHEUS_MULTIPROC_DIR``, which the
worker containers do and the API container does not — the API is a single
uvicorn process here, and multiprocess mode has a real cost (gauges lose their
identity unless a mode is declared, and dead processes must be reaped). This
module therefore does the right thing in either configuration rather than
insisting on one.

**Counters here are process-lifetime, and that is worth stating.** A worker
restart resets them. The durable record of what happened lives in PostgreSQL —
transactions, statement health, AI classifications, privacy counters — and the
state collector below reads *that* for anything a person would want to trust
after an outage. Prometheus counters answer "what is the system doing now";
the database answers "what has it done".
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client import multiprocess

MULTIPROC_ENV = "PROMETHEUS_MULTIPROC_DIR"


def multiprocess_dir() -> Path | None:
    """The shared sample directory, if this process is configured for one."""
    value = os.environ.get(MULTIPROC_ENV)
    return Path(value) if value else None


def prepare_multiprocess_dir() -> None:
    """Create the directory and clear anything a previous run left behind.

    Stale files from a dead process would otherwise be exported forever, which
    shows up as an extraction that never finishes and a worker that is somehow
    still busy after being replaced.
    """
    directory = multiprocess_dir()
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.db"):
        stale.unlink(missing_ok=True)


def usable() -> bool:
    """Whether this process can actually write samples where it was told to.

    Checked before the worker declares multiprocess mode, because
    prometheus_client raises at *increment* time rather than at import — which
    turns an unwritable directory into a crash inside a Celery task, and a
    crash-looping worker. Observability is allowed to be unavailable; the queue
    is not.
    """
    directory = multiprocess_dir()
    if directory is None:
        return True
    probe = directory / ".write-probe"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.touch()
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def mark_process_dead(pid: int) -> None:
    """Reap a forked child's gauge samples when it exits."""
    if multiprocess_dir() is not None:
        multiprocess.mark_process_dead(pid)


def render() -> tuple[bytes, str]:
    """The exposition payload for whichever mode this process is in."""
    directory = multiprocess_dir()
    if directory is None:
        return generate_latest(), CONTENT_TYPE_LATEST

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry, path=str(directory))
    return generate_latest(registry), CONTENT_TYPE_LATEST


def serve(port: int) -> Any:
    """Start a metrics HTTP server for a process that has no HTTP server.

    Celery workers need one — they are scraped like any other target, and the
    alternative (a push gateway) turns a pull-based system into a push-based
    one for no benefit here.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — required by BaseHTTPRequestHandler
            if self.path.split("?")[0] not in {"/metrics", "/"}:
                self.send_response(404)
                self.end_headers()
                return
            payload, content_type = render()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: Any) -> None:
            """Silence the default stderr access log.

            It writes outside our logging pipeline, which means outside the
            redaction filter. A scrape URL carries nothing sensitive today, but
            a log line that bypasses the filter is a hole waiting for one.
            """

    server = HTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="metrics")
    thread.start()
    return server
