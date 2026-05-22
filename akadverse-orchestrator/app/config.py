"""Configuration loading for the orchestrator."""

from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file if present

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from google import genai

from .models import ToolDefinition

logger = logging.getLogger(__name__)

DEFAULT_MODEL_FALLBACKS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]


def get_valid_model_name(
    api_key: str,
    preferred_model: str | None = None,
    fallback_models: list[str] | None = None,
) -> str:
    """Discover the best available Gemini model dynamically with safe fallback."""

    fallback_priority = list(fallback_models or DEFAULT_MODEL_FALLBACKS)
    default_model = fallback_priority[0] if fallback_priority else "gemini-2.5-flash"
    if not api_key:
        return preferred_model or default_model

    try:
        client = genai.Client(api_key=api_key)
        available_models = [m.name.replace("models/", "") for m in client.models.list() if getattr(m, "name", None)]

        priority: list[str] = []
        for candidate in [preferred_model, *fallback_priority]:
            if candidate and candidate not in priority:
                priority.append(candidate)

        for candidate in priority:
            if candidate in available_models:
                return candidate

        return available_models[0] if available_models else default_model
    except Exception:
        logger.exception("Model discovery failed")
        return default_model


def _split_csv(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return list(default)
    items = [item.strip() for item in value.split(",")]
    return [item for item in items if item]


@dataclass(slots=True)
class OrchestratorSettings:
    """Runtime settings for the orchestrator service."""

    llm_provider: str = "gemini"
    model_name: str = "gemini-2.5-flash"
    model_fallbacks: list[str] = field(default_factory=lambda: list(DEFAULT_MODEL_FALLBACKS))
    google_api_key: str | None = None
    openai_api_key: str | None = None
    tools_config_path: Path = Path("config.yaml")
    session_limit: int = 20
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    stem_backend: str = "memory"
    tools: list[ToolDefinition] = field(default_factory=list)


def _load_yaml_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        logger.info("Config file %s not found; using environment defaults", config_path)
        return {}

    try:
        with config_path.open("r", encoding="utf-8") as file_handle:
            loaded = yaml.safe_load(file_handle) or {}
    except (OSError, yaml.YAMLError, ValueError):
        logger.exception("Failed to read config file %s; using environment defaults", config_path)
        return {}

    if not isinstance(loaded, dict):
        logger.warning("Config file %s must contain a mapping at the top level; using defaults", config_path)
        return {}

    return loaded


def _build_tool_definitions(raw_tools: Any) -> list[ToolDefinition]:
    if not raw_tools:
        return []

    if not isinstance(raw_tools, list):
        logger.warning("config.yaml tools section must be a list; ignoring malformed tool config")
        return []

    tools: list[ToolDefinition] = []
    for raw_tool in raw_tools:
        if not isinstance(raw_tool, dict):
            logger.warning("Skipping non-mapping tool entry in config")
            continue

        if not raw_tool.get("name"):
            logger.warning("Skipping tool entry without a name")
            continue

        try:
            parameters = raw_tool.get("parameters", {})
            if not isinstance(parameters, dict):
                parameters = {}

            tools.append(
                ToolDefinition(
                    name=str(raw_tool["name"]),
                    description=str(raw_tool.get("description", "")),
                    parameters=dict(parameters),
                    endpoint=raw_tool.get("endpoint"),
                    request_format=str(raw_tool.get("request_format", "json")),
                    timeout_seconds=float(raw_tool.get("timeout_seconds", raw_tool.get("timeout", 30.0))),
                    retry_count=int(raw_tool.get("retry_count", raw_tool.get("retry", 0))),
                )
            )
        except (TypeError, ValueError, KeyError):
            logger.exception("Skipping invalid tool definition: %s", raw_tool)
            continue

    return tools


def load_settings(config_path: str | Path | None = None) -> OrchestratorSettings:
    """Load orchestrator settings from environment and YAML config."""

    # Treat config loading as optional so one bad file does not block service startup.
    resolved_config_path = Path(config_path or os.getenv("AKADVERSE_ORCHESTRATOR_CONFIG", "config.yaml"))
    raw_config = _load_yaml_config(resolved_config_path)

    llm_section = raw_config.get("llm", {})
    stem_section = raw_config.get("stem", {})
    session_section = raw_config.get("session", {})

    if not isinstance(llm_section, dict):
        llm_section = {}
    if not isinstance(stem_section, dict):
        stem_section = {}
    if not isinstance(session_section, dict):
        session_section = {}

    llm_provider = os.getenv("LLM_PROVIDER", str(llm_section.get("provider", "gemini")))
    configured_model = os.getenv("LLM_MODEL_NAME", str(llm_section.get("model_name", "gemini-2.5-flash")))
    raw_fallbacks = os.getenv("LLM_MODEL_FALLBACKS")
    if raw_fallbacks is not None:
        model_fallbacks = _split_csv(raw_fallbacks, DEFAULT_MODEL_FALLBACKS)
    else:
        configured_fallbacks = llm_section.get("model_fallbacks", DEFAULT_MODEL_FALLBACKS)
        if isinstance(configured_fallbacks, list):
            model_fallbacks = [str(item).strip() for item in configured_fallbacks if str(item).strip()]
        else:
            model_fallbacks = list(DEFAULT_MODEL_FALLBACKS)
    if not model_fallbacks:
        model_fallbacks = list(DEFAULT_MODEL_FALLBACKS)
    google_api_key = os.getenv("GOOGLE_API_KEY")
    openai_api_key = os.getenv("OPENAI_API_KEY")
    try:
        session_limit = int(os.getenv("SESSION_LIMIT", str(session_section.get("max_messages", 20))))
    except (TypeError, ValueError):
        logger.warning("Invalid SESSION_LIMIT value; falling back to 20")
        session_limit = 20

    cors_origins = _split_csv(os.getenv("CORS_ORIGINS"), raw_config.get("cors_origins", ["*"]))
    stem_backend = os.getenv("STEM_BACKEND", str(stem_section.get("backend", "memory")))
    tools = _build_tool_definitions(raw_config.get("tools", []))
    model_name = get_valid_model_name(google_api_key or "", configured_model, model_fallbacks)

    return OrchestratorSettings(
        llm_provider=llm_provider,
        model_name=model_name,
        model_fallbacks=model_fallbacks,
        google_api_key=google_api_key,
        openai_api_key=openai_api_key,
        tools_config_path=resolved_config_path,
        session_limit=session_limit,
        cors_origins=cors_origins,
        stem_backend=stem_backend,
        tools=tools,
    )
