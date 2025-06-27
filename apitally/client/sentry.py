import asyncio
import contextlib
from typing import Callable, Set


_tasks: Set[asyncio.Task] = set()


def get_sentry_event_id_async(cb: Callable[[str], None], raise_on_error: bool = False) -> None:
    try:
        import sentry_sdk
        from sentry_sdk.scope import Scope
    except ImportError:
        if raise_on_error:
            raise
        return  # pragma: no cover
    if not hasattr(Scope, "get_isolation_scope") or not hasattr(Scope, "_last_event_id"):
        if raise_on_error:
            raise RuntimeError("sentry-sdk < 2.2.0 is not supported")
        return  # pragma: no cover
    if not sentry_sdk.is_initialized():
        if raise_on_error:
            raise RuntimeError("sentry-sdk not initialized")
        return

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
