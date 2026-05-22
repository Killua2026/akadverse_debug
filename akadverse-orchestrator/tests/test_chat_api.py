"""Chat API tests for the AkadVerse orchestrator."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.router import RouterOutcome


def test_health_returns_200() -> None:
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["selected_model"]


def test_chat_greeting_returns_non_tool_reply(monkeypatch) -> None:
    async def fake_process(messages, system_prompt, session_id, request_id=None):
        return RouterOutcome(reply="Hello!", tool_used=None, action=None)

    monkeypatch.setattr("app.main.router.route", fake_process)
    client = TestClient(app)

    response = client.post("/chat", json={"session_id": "CHAT-1", "message": "hello"})

    assert response.status_code == 200
    assert response.json() == {"reply": "Hello!", "tool_used": None, "action": None}


def test_chat_quiz_prompt_triggers_tool_path(monkeypatch) -> None:
    async def fake_process(messages, system_prompt, session_id, request_id=None):
        return RouterOutcome(reply="Here is your quiz.", tool_used="quiz_generator", action=None)

    monkeypatch.setattr("app.main.router.route", fake_process)
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={"session_id": "CHAT-2", "message": "Generate a quiz on photosynthesis"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "reply": "Here is your quiz.",
        "tool_used": "quiz_generator",
        "action": None,
    }


def test_chat_slide_prompt_exposes_download_action(monkeypatch) -> None:
    async def fake_process(messages, system_prompt, session_id, request_id=None):
        return RouterOutcome(
            reply="Your slide deck is ready. Click the download button below.",
            tool_used="slide_generator",
            action={
                "type": "download",
                "url": "/downloads/slide/presentation.pptx",
                "label": "Download Slide Generator",
            },
        )

    monkeypatch.setattr("app.main.router.route", fake_process)
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={"session_id": "CHAT-3", "message": "Make me a slide deck"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "reply": "Your slide deck is ready. Click the download button below.",
        "tool_used": "slide_generator",
        "action": {
            "type": "download",
            "url": "/downloads/slide/presentation.pptx",
            "label": "Download Slide Generator",
        },
    }


def test_slide_download_proxy_returns_attachment(monkeypatch) -> None:
    async def fake_get(self, url, follow_redirects=True):
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            content=b"pptx-bytes",
            headers={"content-type": "application/vnd.openxmlformats-officedocument.presentationml.presentation"},
            request=request,
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    client = TestClient(app)

    response = client.get("/downloads/slide/presentation.pptx")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == "attachment; filename*=UTF-8''presentation.pptx"
    assert response.content == b"pptx-bytes"


def test_slide_download_proxy_returns_404_for_missing_file(monkeypatch) -> None:
    async def fake_get(self, url, follow_redirects=True):
        request = httpx.Request("GET", url)
        return httpx.Response(404, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    client = TestClient(app)

    response = client.get("/downloads/slide/missing.pptx")

    assert response.status_code == 404
    assert response.json() == {"detail": "File not found or expired"}


def test_slide_download_proxy_rejects_invalid_filename() -> None:
    client = TestClient(app)
    response = client.get("/downloads/slide/invalid.txt")

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid filename"}
