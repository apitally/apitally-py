from typing import Optional, Union

import psutil


_is_first_interval = True
_process = psutil.Process()


def get_cpu_memory_usage() -> Optional[dict[str, Union[float, int]]]:
    global _is_first_interval
    try:
        data = {
            "cpu_percent": _process.cpu_percent(),
            "memory_rss": _process.memory_info().rss,
        }
        if _is_first_interval:
            _is_first_interval = False
            return None
        return data
    except psutil.Error:  # pragma: no cover
        return None
