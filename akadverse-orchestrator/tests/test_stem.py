"""Stem client tests."""

from __future__ import annotations

import asyncio

from app.stem import InMemoryStemClient


def test_store_retrieve_and_update_context() -> None:
    stem = InMemoryStemClient()

    async def scenario() -> tuple[dict[str, object], dict[str, object]]:
        await stem.save_context("S1", {"active_course": "MTH201", "level": 200})
        first = await stem.get_context("S1")

        await stem.save_context("S1", {"active_course": "CSC301", "level": 300})
        second = await stem.get_context("S1")
        return first, second

    first, second = asyncio.run(scenario())
    assert first == {"active_course": "MTH201", "level": 200}
    assert second == {"active_course": "CSC301", "level": 300}
