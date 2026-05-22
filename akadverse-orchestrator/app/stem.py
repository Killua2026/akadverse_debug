"""Stem context abstraction for shared user and session state."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class StemClient(ABC):
    """Abstract interface for stem-backed context storage."""

    @abstractmethod
    async def get_context(self, session_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def save_context(self, session_id: str, context: dict[str, Any]) -> None:
        raise NotImplementedError


class InMemoryStemClient(StemClient):
    """In-memory stem client used until Redis or Red Panda is wired in."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    async def get_context(self, session_id: str) -> dict[str, Any]:
        return dict(self._store.get(session_id, {}))

    async def save_context(self, session_id: str, context: dict[str, Any]) -> None:
        self._store[session_id] = dict(context)


class RedisStemClient(StemClient):
    """Redis-backed stem client placeholder for the next implementation step."""

    async def get_context(self, session_id: str) -> dict[str, Any]:
        raise NotImplementedError("RedisStemClient will be implemented after the in-memory pass.")

    async def save_context(self, session_id: str, context: dict[str, Any]) -> None:
        raise NotImplementedError("RedisStemClient will be implemented after the in-memory pass.")
