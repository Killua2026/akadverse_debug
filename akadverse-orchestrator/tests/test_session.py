"""Session manager tests."""

from __future__ import annotations

import asyncio

from app.models import Message
from app.session import InMemorySessionManager


def test_append_messages_and_ordering() -> None:
    manager = InMemorySessionManager(max_messages=20)

    async def scenario() -> list[Message]:
        await manager.append_message("S1", Message(role="user", content="first"))
        await manager.append_message("S1", Message(role="assistant", content="second"))
        return await manager.get_history("S1")

    history = asyncio.run(scenario())
    assert [message.content for message in history] == ["first", "second"]


def test_session_history_cap_is_enforced() -> None:
    manager = InMemorySessionManager(max_messages=20)

    async def scenario() -> list[Message]:
        for index in range(25):
            await manager.append_message("S2", Message(role="user", content=f"m{index}"))
        return await manager.get_history("S2")

    history = asyncio.run(scenario())
    assert len(history) == 20
    assert history[0].content == "m5"
    assert history[-1].content == "m24"
