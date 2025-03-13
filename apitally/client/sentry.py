import asyncio
import contextlib
from typing import Callable, Set


_tasks: Set[asyncio.Task] = set()


def get_sentry_event_id_async(cb: Callable[[str], None]) -> None:
    try:
        from sentry_sdk.hub import Hub
        from sentry_sdk.scope import Scope
    except ImportError:
        return  # pragma: no cover
    if not hasattr(Scope, "get_isolation_scope") or not hasattr(Scope, "_last_event_id"):
        # sentry-sdk < 2.2.0 is not supported
        return  # pragma: no cover
    if Hub.current.client is None:
        return  # sentry-sdk not initialized

    scope = Scope.get_isolation_scope()
    if event_id := scope._last_event_id:
        cb(event_id)
        return

    async def _wait_for_sentry_event_id(scope: Scope) -> None:
        i = 0
        while not (event_id := scope._last_event_id) and i < 100:
            i += 1
            await asyncio.sleep(0.001)
        if event_id:
            cb(event_id)

    with contextlib.suppress(RuntimeError):  # ignore no running loop
        loop = asyncio.get_running_loop()
        task = loop.create_task(_wait_for_sentry_event_id(scope))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
