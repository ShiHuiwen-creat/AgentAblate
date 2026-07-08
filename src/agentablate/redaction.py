import os
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"
_SECRET_SUFFIXES = ("TOKEN", "KEY", "SECRET")


def _is_secret_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.upper()
    return normalized in _SECRET_SUFFIXES or normalized.endswith(
        tuple(f"_{suffix}" for suffix in _SECRET_SUFFIXES)
    )


class Redactor:
    """Remove credential values and credential-shaped structured fields."""

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._refresh()

    def _refresh(self) -> None:
        self._values.update(
            value
            for key, value in os.environ.items()
            if value and _is_secret_key(key)
        )

    def __repr__(self) -> str:
        return "Redactor()"

    def text(self, value: str) -> str:
        self._refresh()
        for secret in sorted(self._values, key=len, reverse=True):
            value = value.replace(secret, REDACTED)
        return value

    def data(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: REDACTED if _is_secret_key(key) else self.data(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.data(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.data(item) for item in value)
        if isinstance(value, str):
            return self.text(value)
        return value
