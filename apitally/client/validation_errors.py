from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class ValidationError:
    consumer: Optional[str]
    method: str
    path: str
    loc: Tuple[str, ...]
    msg: str
    type: str


class ValidationErrorCounter:
    def __init__(self) -> None:
        self.error_counts: Counter[ValidationError] = Counter()
        self._lock = threading.Lock()

    def add_validation_errors(
        self, consumer: Optional[str], method: str, path: str, detail: List[Dict[str, Any]]
    ) -> None:
        with self._lock:
            for error in detail:
                try:
                    validation_error = ValidationError(
                        consumer=consumer,
                        method=method.upper(),
                        path=path,
                        loc=tuple(str(loc) for loc in error["loc"]),
                        msg=error["msg"],
                        type=error["type"],
                    )
                    self.error_counts[validation_error] += 1
                except (KeyError, TypeError):  # pragma: no cover
                    pass

    def get_and_reset_validation_errors(self) -> List[Dict[str, Any]]:
        data: List[Dict[str, Any]] = []
        with self._lock:
            for validation_error, count in self.error_counts.items():
                data.append(
                    {
                        "consumer": validation_error.consumer,
                        "method": validation_error.method,
                        "path": validation_error.path,
                        "loc": validation_error.loc,
                        "msg": validation_error.msg,
                        "type": validation_error.type,
                        "error_count": count,
                    }
                )
            self.error_counts.clear()
        return data
