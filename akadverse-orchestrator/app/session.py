"""Session history management for the orchestrator."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import Message


class SessionManager(ABC):
    """Abstract session history store."""

    @abstractmethod
    async def get_history(self, session_id: str) -> list[Message]:
        raise NotImplementedError

    @abstractmethod
    async def append_message(self, session_id: str, message: Message) -> None:
        raise NotImplementedError


class InMemorySessionManager(SessionManager):
    """Simple bounded in-memory session store for the first implementation pass."""

    def __init__(self, max_messages: int = 20) -> None:
        self._max_messages = max_messages
        self._sessions: dict[str, list[Message]] = {}

    async def get_history(self, session_id: str) -> list[Message]:
        return list(self._sessions.get(session_id, []))

    async def append_message(self, session_id: str, message: Message) -> None:
        history = self._sessions.setdefault(session_id, [])
        history.append(message)
        if len(history) > self._max_messages:
            self._sessions[session_id] = history[-self._max_messages :]
