from typing import Dict, Optional, Union

import psutil


_is_first_interval = True


def get_cpu_memory_usage() -> Optional[Dict[str, Union[float, int]]]:
    global _is_first_interval
    process = psutil.Process()
    data = {
        "cpu_percent": process.cpu_percent(),
        "memory_rss": process.memory_info().rss,
    }
    if _is_first_interval:
        _is_first_interval = False
        return None
    return data
