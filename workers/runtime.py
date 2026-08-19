"""Running one async task body per Celery task, and tearing it down correctly.

Every worker task is a synchronous Celery function wrapping an async body, so
each one opens an event loop, does its work and closes it. The obvious shape —
``asyncio.run(body())`` and then ``asyncio.run(dispose_engine())`` in a
``finally`` — is wrong in a way that is easy to miss because it still produces
the right answer.

The engine's connections are bound to the loop that opened them. Disposing it
from a *second* loop asks asyncpg to close sockets belonging to a loop that no
longer exists, and every task logs ``RuntimeError: Event loop is closed`` at
ERROR level on its way out. The work succeeded; the log says otherwise. That is
the specific failure this module exists to prevent — not a crash, but a stream
of red herrings that trains everyone to skim past worker errors.

The fix is to dispose *inside* the same loop, in a ``finally`` on the coroutine
rather than on the task function.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


async def _with_teardown(body: Coroutine[Any, Any, T]) -> T:
    from app.core.redis_client import close_redis
    from app.db.session import dispose_engine

    try:
        return await body
    finally:
        # Both clients bind their connections to the running loop. Closing them
        # here means it is still the loop that owns them.
        await dispose_engine()
        await close_redis()


def run(body: Coroutine[Any, Any, T]) -> T:
    """Run a task body and close its resources before the loop goes."""
    return asyncio.run(_with_teardown(body))
