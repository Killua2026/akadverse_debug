"""FastAPI application for the AkadVerse orchestrator."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .composer import ResponseComposer
from .config import OrchestratorSettings, load_settings
from .models import ChatRequest, HealthResponse, Message, RouteResult
from .router import Router
from .session import InMemorySessionManager, SessionManager
from .stem import InMemoryStemClient, StemClient
from .tools import HttpToolInvoker, ToolInvoker, ToolRegistry

logger = logging.getLogger(__name__)


def _hash_preview(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _log_event(level: int, event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    try:
        logger.log(level, json.dumps(payload, ensure_ascii=True, default=str))
    except Exception:
        logger.log(level, "%s %s", event, fields)

app = FastAPI(title="AkadVerse Orchestrator", version="0.1.0")

# Load settings early so the app uses the configured model and tool registry.
try:
    settings: OrchestratorSettings = load_settings()
except Exception:
    logger.exception("Failed to load orchestrator settings; using defaults")
    settings = OrchestratorSettings()

# The orchestrator is assembled once at import time so request handling stays lightweight.
session_manager: SessionManager = InMemorySessionManager(max_messages=settings.session_limit)
stem_client: StemClient = InMemoryStemClient()
tool_registry: ToolRegistry = ToolRegistry.from_settings(settings)
tool_invoker: ToolInvoker = HttpToolInvoker(tool_registry)
router: Router = Router(settings=settings, registry=tool_registry, invoker=tool_invoker)
composer = ResponseComposer()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _system_prompt_from_context(context: dict[str, Any]) -> str:
    if not context:
        return "You are the AkadVerse orchestrator. Keep responses concise and helpful."

    # Keep contextual facts in a simple, readable block for the router.
    lines = ["You are the AkadVerse orchestrator. Keep responses concise and helpful."]
    for key, value in context.items():
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    # Expose the exact Gemini model selected at startup so operators can see
    # whether discovery landed on a fallback or the preferred model.
    return HealthResponse(
        status="healthy",
        selected_model=settings.model_name,
        llm_ready=settings.google_api_key is not None,
        session_ready=session_manager is not None,
        stem_ready=stem_client is not None,
        tools_ready=True,
    )


@app.post("/chat")
async def chat(request: ChatRequest) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    started = time.perf_counter()
    _log_event(
        logging.INFO,
        "chat.request.start",
        request_id=request_id,
        session_id=request.session_id,
        user_id_present=bool(request.user_id),
        agent_hint_present=bool(request.agent_hint),
        message_len=len(request.message),
        message_sha256_12=_hash_preview(request.message),
    )

    try:
        history = await session_manager.get_history(request.session_id)
    except Exception:
        logger.exception("Failed to load session history for %s", request.session_id)
        history = []

    try:
        stem_context = await stem_client.get_context(request.session_id)
    except Exception:
        logger.exception("Failed to load stem context for %s", request.session_id)
        stem_context = {}

    _log_event(
        logging.INFO,
        "chat.request.envelope",
        request_id=request_id,
        session_id=request.session_id,
        message_len=len(request.message),
        history_count=len(history),
        stem_context_keys_count=len(stem_context),
        stem_context_keys_sample=list(stem_context.keys())[:10],
    )

    system_prompt = _system_prompt_from_context(stem_context)
    user_message = Message(role="user", content=request.message)
    conversation = [*history, user_message]

    _log_event(
        logging.INFO,
        "chat.router.call",
        request_id=request_id,
        session_id=request.session_id,
        conversation_messages=len(conversation),
        system_prompt_len=len(system_prompt),
        configured_model=settings.model_name,
        tool_registry_count=len(tool_registry.list_tools()),
    )

    try:
        outcome: RouteResult = await router.route(
            messages=conversation,
            system_prompt=system_prompt,
            session_id=request.session_id,
            request_id=request_id,
        )
        response_payload = composer.compose(outcome)
    except Exception:
        logger.exception("Router failure for session %s", request.session_id)
        response_payload = {
            "reply": "I'm having trouble processing that request right now. Please try again.",
            "tool_used": None,
            "action": None,
        }

    # Persist the exchange after a reply is ready so memory issues never block the response.
    try:
        await session_manager.append_message(request.session_id, user_message)
        await session_manager.append_message(
            request.session_id,
            Message(role="assistant", content=str(response_payload.get("reply", ""))),
        )
    except Exception:
        logger.exception("Failed to persist chat history for %s", request.session_id)

    _log_event(
        logging.INFO,
        "chat.response.summary",
        request_id=request_id,
        session_id=request.session_id,
        reply_len=len(str(response_payload.get("reply", ""))),
        tool_used=response_payload.get("tool_used"),
        has_action=bool(response_payload.get("action")),
        request_latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )

    return response_payload
