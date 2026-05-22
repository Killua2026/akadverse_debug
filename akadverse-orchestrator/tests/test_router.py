"""Router tests for the AkadVerse orchestrator."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

from app.config import OrchestratorSettings
from app.models import Message, ToolDefinition, ToolResult
from app.router import Router
from app.tools import ToolInvoker, ToolRegistry


class FakeModels:
    def __init__(self, responses: list[Any]):
        self._responses: list[Any] = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClient:
    def __init__(self, responses: list[Any]):
        self.models: FakeModels = FakeModels(responses)


def _attach_fake_client(router: Router, fake_client: FakeClient) -> FakeClient:
    # The production router keeps a real Gemini client in a private slot.
    # In tests we deliberately swap in a lightweight fake, so we cast the
    # assignment through Any instead of weakening the router's runtime typing.
    router._client = cast(Any, fake_client)
    return fake_client


class RecordingInvoker(ToolInvoker):
    def __init__(self, result: ToolResult):
        self.calls = []
        self._result = result

    async def invoke(self, tool_name: str, params: dict, session_id: str) -> ToolResult:
        self.calls.append({"tool_name": tool_name, "params": params, "session_id": session_id})
        return self._result


class FailingInvoker(ToolInvoker):
    async def invoke(self, tool_name: str, params: dict, session_id: str) -> ToolResult:
        raise RuntimeError("downstream tool timeout")


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="quiz_generator",
            description="Generate a quiz",
            parameters={
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                },
                "required": ["topic"],
            },
            endpoint="http://localhost:8016/quiz",
        )
    )
    registry.register(
        ToolDefinition(
            name="slide_generator",
            description="Generate slides",
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "subject": {"type": "string"},
                },
                "required": ["content"],
            },
            endpoint="http://localhost:8009/slides/from-text",
            request_format="form",
        )
    )
    return registry


def _text_response(text: str):
    return SimpleNamespace(text=text, candidates=[])


def _tool_call_response(name: str, args: dict, call_id: str = "call-1"):
    part = SimpleNamespace(function_call=SimpleNamespace(id=call_id, name=name, args=args))
    content = SimpleNamespace(parts=[part])
    candidate = SimpleNamespace(content=content)
    return SimpleNamespace(text=None, candidates=[candidate])


def test_greeting_returns_direct_text_reply_without_tool_call() -> None:
    settings = OrchestratorSettings(model_name="gemini-2.5-flash", google_api_key="fake-key")
    invoker = RecordingInvoker(
        ToolResult(tool_name="quiz_generator", content="{\"quiz\": []}", success=True)
    )
    router = Router(settings=settings, registry=_registry(), invoker=invoker)
    _attach_fake_client(router, FakeClient([_text_response("Hello! How can I help today?")]))

    outcome = asyncio.run(
        router.route(
            messages=[Message(role="user", content="hello")],
            system_prompt="You are AkadVerse assistant.",
            session_id="S1",
        )
    )

    assert outcome.reply == "Hello! How can I help today?"
    assert outcome.tool_used is None
    assert outcome.action is None
    assert invoker.calls == []


def test_quiz_request_triggers_tool_and_returns_final_text() -> None:
    settings = OrchestratorSettings(model_name="gemini-2.5-flash", google_api_key="fake-key")
    invoker = RecordingInvoker(
        ToolResult(tool_name="quiz_generator", content="{\"quiz\": [\"Q1\", \"Q2\"]}", success=True)
    )
    router = Router(settings=settings, registry=_registry(), invoker=invoker)
    _attach_fake_client(
        router,
        FakeClient(
        [
            _tool_call_response("quiz_generator", {"topic": "photosynthesis"}),
            _text_response("I generated a quiz on photosynthesis with two questions."),
        ]
        ),
    )

    outcome = asyncio.run(
        router.route(
            messages=[Message(role="user", content="Generate a quiz on photosynthesis")],
            system_prompt="You are AkadVerse assistant.",
            session_id="S2",
        )
    )

    assert len(invoker.calls) == 1
    assert invoker.calls[0]["tool_name"] == "quiz_generator"
    assert invoker.calls[0]["params"] == {"topic": "photosynthesis"}
    assert outcome.reply == "I generated a quiz on photosynthesis with two questions."
    assert outcome.tool_used == "quiz_generator"
    assert outcome.action is None


def test_tool_failure_is_converted_to_graceful_model_fallback() -> None:
    settings = OrchestratorSettings(model_name="gemini-2.5-flash", google_api_key="fake-key")
    router = Router(settings=settings, registry=_registry(), invoker=FailingInvoker())
    fake_client = _attach_fake_client(
        router,
        FakeClient(
        [
            _tool_call_response("quiz_generator", {"topic": "electric circuits"}),
            _text_response("I could not access quiz generation right now, please try again."),
        ]
    )
    )

    outcome = asyncio.run(
        router.route(
            messages=[Message(role="user", content="Give me a circuits quiz")],
            system_prompt="You are AkadVerse assistant.",
            session_id="S3",
        )
    )

    assert outcome.reply == "I could not access quiz generation right now, please try again."
    assert outcome.tool_used == "quiz_generator"
    assert outcome.action is None

    second_call_contents = fake_client.models.calls[1]["contents"]
    last_content = second_call_contents[-1]
    function_response = last_content.parts[0].function_response
    assert "Tool unavailable" in str(function_response.response)


def test_tool_action_url_is_exposed_in_route_result() -> None:
    settings = OrchestratorSettings(model_name="gemini-2.5-flash", google_api_key="fake-key")
    invoker = RecordingInvoker(
        ToolResult(
            tool_name="slide_generator",
            content=json.dumps(
                {
                    "result": "Your slide deck is ready.",
                    "download_url": "/slides/download/presentation.pptx",
                }
            ),
            success=True,
        )
    )
    router = Router(settings=settings, registry=_registry(), invoker=invoker)
    fake_client = _attach_fake_client(
        router,
        FakeClient(
        [
            _tool_call_response("slide_generator", {"content": "Metamorphosis", "subject": "Metamorphosis"}),
        ]
        ),
    )

    outcome = asyncio.run(
        router.route(
            messages=[Message(role="user", content="Make me a slide deck")],
            system_prompt="You are AkadVerse assistant.",
            session_id="S4",
        )
    )

    assert outcome.tool_used == "slide_generator"
    assert outcome.reply == "Your slide deck is ready. Click the download button below."
    assert outcome.action == {
        "type": "download",
        "url": "http://localhost:8009/slides/download/presentation.pptx",
        "label": "Download Slide Generator",
    }
    assert len(fake_client.models.calls) == 1


def test_router_falls_back_to_next_model_when_first_choice_fails() -> None:
    settings = OrchestratorSettings(model_name="gemini-2.5-flash", google_api_key="fake-key")
    invoker = RecordingInvoker(
        ToolResult(tool_name="quiz_generator", content="{\"quiz\": []}", success=True)
    )
    router = Router(settings=settings, registry=_registry(), invoker=invoker)
    fake_client = _attach_fake_client(
        router,
        FakeClient(
        [
            RuntimeError("404 model not found"),
            _text_response("Recovered on fallback model."),
        ]
        ),
    )

    outcome = asyncio.run(
        router.route(
            messages=[Message(role="user", content="hello")],
            system_prompt="You are AkadVerse assistant.",
            session_id="S5",
        )
    )

    assert outcome.reply == "Recovered on fallback model."
    assert fake_client.models.calls[0]["model"] == "gemini-2.5-flash"
    assert fake_client.models.calls[1]["model"] in {"gemini-2.0-flash", "gemini-2.0-flash-lite"}


def test_router_returns_quota_specific_message_when_gemini_is_rate_limited() -> None:
    settings = OrchestratorSettings(model_name="gemini-2.5-flash", google_api_key="fake-key")
    invoker = RecordingInvoker(
        ToolResult(tool_name="quiz_generator", content="{\"quiz\": []}", success=True)
    )
    router = Router(settings=settings, registry=_registry(), invoker=invoker)
    _attach_fake_client(
        router,
        FakeClient(
            [
                RuntimeError(
                    "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: generate_content_free_tier_requests"
                ),
                RuntimeError(
                    "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: generate_content_free_tier_requests"
                ),
            ]
        ),
    )

    outcome = asyncio.run(
        router.route(
            messages=[Message(role="user", content="hello")],
            system_prompt="You are AkadVerse assistant.",
            session_id="S6",
        )
    )

    assert "quota is exhausted" in outcome.reply.lower()
