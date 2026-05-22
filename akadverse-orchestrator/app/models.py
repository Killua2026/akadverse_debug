"""Shared data models for the AkadVerse orchestrator."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant", "tool"]


class ToolCall(BaseModel):
    """Represents a single model-issued tool call."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """Canonical conversation message used across the orchestrator."""

    model_config = ConfigDict(extra="forbid")

    role: Role
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


class Session(BaseModel):
    """Stores the ordered message history for one session."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    user_id: str | None = None
    messages: list[Message] = Field(default_factory=list)


class ToolDefinition(BaseModel):
    """Metadata exported to Gemini and the tool registry."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    parameters: dict[str, Any]
    endpoint: str | None = None
    request_format: Literal["json", "form"] = "json"
    timeout_seconds: float = 30.0
    retry_count: int = 0


class ToolResult(BaseModel):
    """Structured result returned by a tool invocation."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    content: str
    success: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    """Incoming chat request from the unified frontend."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    message: str
    user_id: str | None = None
    agent_hint: str | None = None


class ChatResponse(BaseModel):
    """Outgoing chat response from the orchestrator."""

    model_config = ConfigDict(extra="forbid")

    reply: str
    tool_used: str | None = None
    action: dict[str, Any] | None = None


class RouteResult(BaseModel):
    """Final router output used by the composer and API layer."""

    model_config = ConfigDict(extra="forbid")

    reply: str
    tool_used: str | None = None
    action: dict[str, Any] | None = None


class HealthResponse(BaseModel):
    """Health payload returned by the API."""

    model_config = ConfigDict(extra="forbid")

    status: str = "healthy"
    selected_model: str
    llm_ready: bool = False
    session_ready: bool = False
    stem_ready: bool = False
    tools_ready: bool = False
