from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Set, Union


class Consumer:
    def __init__(self, identifier: str, name: Optional[str] = None, group: Optional[str] = None) -> None:
        self.identifier = str(identifier).strip()[:128]
        self.name = str(name).strip()[:64] if name else None
        self.group = str(group).strip()[:64] if group else None

    @classmethod
    def from_string_or_object(cls, consumer: Optional[Union[str, Consumer]]) -> Optional[Consumer]:
        if not consumer:
            return None
        if isinstance(consumer, Consumer):
            return consumer
        consumer = str(consumer).strip()
        if not consumer:
            return None
        return cls(identifier=consumer)

    def update(self, name: str | None = None, group: str | None = None) -> bool:
        name = str(name).strip()[:64] if name else None
        group = str(group).strip()[:64] if group else None
        updated = False
        if name and name != self.name:
            self.name = name
            updated = True
        if group and group != self.group:
            self.group = group
            updated = True
        return updated


class ConsumerRegistry:
    def __init__(self) -> None:
        self.consumers: Dict[str, Consumer] = {}
        self.updated: Set[str] = set()
        self._lock = threading.Lock()

    def add_or_update_consumer(self, consumer: Optional[Consumer]) -> None:
        if not consumer or (not consumer.name and not consumer.group):
            return  # Only register consumers with name or group set
        with self._lock:
            if consumer.identifier not in self.consumers:
                self.consumers[consumer.identifier] = consumer
                self.updated.add(consumer.identifier)
            elif self.consumers[consumer.identifier].update(name=consumer.name, group=consumer.group):
                self.updated.add(consumer.identifier)

    def get_and_reset_updated_consumers(self) -> List[Dict[str, Any]]:
        data: List[Dict[str, Any]] = []
        with self._lock:
            for identifier in self.updated:
                if consumer := self.consumers.get(identifier):
                    data.append(
                        {
                            "identifier": consumer.identifier,
                            "name": str(consumer.name)[:64] if consumer.name else None,
                            "group": str(consumer.group)[:64] if consumer.group else None,
                        }
                    )
            self.updated.clear()
        return data
