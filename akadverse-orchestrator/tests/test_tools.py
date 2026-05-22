"""Tool registry and invoker tests."""

from __future__ import annotations

import asyncio

import httpx

from app.models import ToolDefinition
from app.tools import HttpToolInvoker, ToolRegistry


def _registry_with_quiz_tool() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="quiz_generator",
            description="Generate quiz",
            parameters={
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
            },
            endpoint="http://tools.local/quiz",
            timeout_seconds=5,
            retry_count=0,
        )
    )
    return registry


def test_http_tool_invoker_sends_expected_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_post(self, url, json=None, data=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["data"] = data
        captured["headers"] = headers
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"ok": True, "tool": "quiz_generator"}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    invoker = HttpToolInvoker(_registry_with_quiz_tool())

    result = asyncio.run(
        invoker.invoke("quiz_generator", {"topic": "photosynthesis"}, "S-123")
    )

    assert captured["url"] == "http://tools.local/quiz"
    assert captured["json"] == {
        "session_id": "S-123",
        "tool_name": "quiz_generator",
        "params": {"topic": "photosynthesis"},
    }
    assert captured["data"] is None
    assert result.success is True
    assert result.content == '{"ok": true, "tool": "quiz_generator"}'


def test_http_tool_invoker_uses_form_payload_when_requested(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_post(self, url, json=None, data=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["data"] = data
        captured["headers"] = headers
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"ok": True, "tool": "slide_generator"}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="slide_generator",
            description="Generate slides",
            parameters={"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]},
            endpoint="http://tools.local/slides/from-text",
            request_format="form",
            timeout_seconds=5,
            retry_count=0,
        )
    )
    invoker = HttpToolInvoker(registry)

    result = asyncio.run(
        invoker.invoke("slide_generator", {"content": "Intro text", "subject": "AI"}, "S-456")
    )

    assert captured["url"] == "http://tools.local/slides/from-text"
    assert captured["json"] is None
    assert captured["data"] == {"content": "Intro text", "subject": "AI"}
    assert captured["headers"]["X-Akadverse-Session-Id"] == "S-456"
    assert captured["headers"]["X-Akadverse-Tool-Name"] == "slide_generator"
    assert result.success is True
    assert result.content == '{"ok": true, "tool": "slide_generator"}'


def test_http_tool_invoker_handles_timeout(monkeypatch) -> None:
    async def fake_post(self, url, json=None, data=None, headers=None):
        raise httpx.TimeoutException("request timed out")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    invoker = HttpToolInvoker(_registry_with_quiz_tool())

    result = asyncio.run(
        invoker.invoke("quiz_generator", {"topic": "biology"}, "S-555")
    )

    assert result.success is False
    assert "Tool unavailable" in result.content


def test_http_tool_invoker_handles_http_error_status(monkeypatch) -> None:
    async def fake_post(self, url, json=None, data=None, headers=None):
        request = httpx.Request("POST", url)
        return httpx.Response(500, json={"error": "upstream failure"}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    invoker = HttpToolInvoker(_registry_with_quiz_tool())

    result = asyncio.run(
        invoker.invoke("quiz_generator", {"topic": "chemistry"}, "S-888")
    )

    assert result.success is False
    assert "Tool unavailable" in result.content
