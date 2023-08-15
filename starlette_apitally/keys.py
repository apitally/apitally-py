from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import scrypt
from typing import Any, Dict, List, Optional, Set


@dataclass(frozen=True)
class Key:
    key_id: int
    name: str = ""
    scopes: List[str] = field(default_factory=list)
    expires_at: Optional[datetime] = None

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at < datetime.now()

    def check_scopes(self, scopes: List[str]) -> bool:
        return all(scope in self.scopes for scope in scopes)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Key:
        return cls(
            key_id=data["key_id"],
            name=data.get("name", ""),
            scopes=data.get("scopes", []),
            expires_at=(
                datetime.now() + timedelta(seconds=data["expires_in_seconds"])
                if data["expires_in_seconds"] is not None
                else None
            ),
        )


class Keys:
    def __init__(self) -> None:
        self.salt: Optional[str] = None
        self.keys: Dict[str, Key] = {}
        self.used_key_ids: Set[int] = set()

    def get(self, api_key: str) -> Optional[Key]:
        hash = self.hash_api_key(api_key)
        key = self.keys.get(hash)
        if key is None or key.is_expired:
            return None
        self.used_key_ids.add(key.key_id)
        return key

    def hash_api_key(self, api_key: str) -> str:
        if self.salt is None:
            raise RuntimeError("Apitally keys not initialized")
        return scrypt(api_key.encode(), salt=bytes.fromhex(self.salt), n=256, r=4, p=1, dklen=32).hex()

    def update(self, keys: Dict[str, Dict[str, Any]]) -> None:
        self.keys = {hash: Key.from_dict(data) for hash, data in keys.items()}

    def get_and_reset_used_key_ids(self) -> List[int]:
        data = list(self.used_key_ids)
        self.used_key_ids.clear()
        return data
