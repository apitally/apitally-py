from __future__ import annotations

import logging
import time
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any, Callable, List, Optional

from starlette_apitally.schema import ApitallyPayload, RequestResponseItem


logger = logging.getLogger(__name__)

# Function to register an on-exit callback for both Python and IPython runtimes
try:
    ipython = get_ipython()  # type: ignore

    def register_exit(func: Callable[..., Any], *args, **kwargs) -> Callable[..., Any]:
        def callback():
            func()
            ipython.events.unregister("post_execute", callback)

        ipython.events.register("post_execute", callback)
        return func

except NameError:
    from atexit import register as register_exit


class BufferedSender:
    def __init__(self, send_every: int = 10) -> None:
        self.send_every = send_every
        self.queue: Queue[RequestResponseItem] = Queue()
        self.thread: Optional[Thread] = None
        self.stop_event: Event = Event()

    def add(self, item: RequestResponseItem) -> None:
        self.queue.put(item)
        self._ensure_thread_alive()

    def aggregate(self, items: List[RequestResponseItem]) -> ApitallyPayload:
        return {}

    def send(self, data: ApitallyPayload) -> None:
        pass

    def _ensure_thread_alive(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.stop_event.clear()
            self.thread = Thread(target=self._thread_worker)
            self.thread.start()
            register_exit(self._stop_thread)

    def _thread_worker(self) -> None:
        while not self.stop_event.is_set():
            time.sleep(self.send_every)
            items = []
            try:
                while True:
                    items.append(self.queue.get(block=False))
            except Empty:
                pass
            finally:
                if len(items) > 0:
                    try:
                        data = self.aggregate(items)
                        self.send(data)
                    except Exception as e:
                        logger.exception(e)
                    for _ in range(len(items)):
                        self.queue.task_done()

    def _stop_thread(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
            self.thread = None
