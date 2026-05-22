"""Gemini router loop for tool-aware orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from urllib.parse import urljoin, urlparse
from typing import Any, cast

from google import genai
from google.genai import types

from .config import OrchestratorSettings
from .downloads import extract_slide_filename
from .models import Message, RouteResult, ToolCall, ToolResult
from .tools import ToolInvoker, ToolRegistry

logger = logging.getLogger(__name__)

RouterOutcome = RouteResult


class Router:
    """Coordinates Gemini responses, tool calls, and the final assistant reply."""

    def __init__(
        self,
        settings: OrchestratorSettings,
        registry: ToolRegistry,
        invoker: ToolInvoker,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._invoker = invoker
        self._client = genai.Client(api_key=settings.google_api_key) if settings.google_api_key else None

    def _hash_preview(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:12]

    def _log_event(self, level: int, event: str, **fields: Any) -> None:
        payload = {"event": event, **fields}
        try:
            logger.log(level, json.dumps(payload, ensure_ascii=True, default=str))
        except Exception:
            logger.log(level, "%s %s", event, fields)

    def _extract_usage(self, response: Any) -> dict[str, Any]:
        usage = getattr(response, "usage_metadata", None) or getattr(response, "usageMetadata", None)
        if usage is None:
            return {}

        def _read(*names: str) -> Any:
            for name in names:
                value = getattr(usage, name, None)
                if value is not None:
                    return value
            if isinstance(usage, dict):
                for name in names:
                    if name in usage:
                        return usage[name]
            return None

        return {
            "prompt_tokens": _read("prompt_token_count", "promptTokenCount", "input_tokens", "inputTokenCount"),
            "candidate_tokens": _read(
                "candidates_token_count",
                "candidatesTokenCount",
                "output_tokens",
                "outputTokenCount",
            ),
            "total_tokens": _read("total_token_count", "totalTokenCount"),
            "cached_content_tokens": _read("cached_content_token_count", "cachedContentTokenCount"),
        }

    def _parse_retry_delay_seconds(self, message: str) -> float | None:
        match = re.search(r"please retry in\s+([0-9]+(?:\.[0-9]+)?)s", message, flags=re.IGNORECASE)
        if not match:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    def _model_candidates(self) -> list[str]:
        """Build an ordered list of Gemini models to try for this session."""

        # Try the selected model first, then fall back to the configured
        # model chain so quota and availability tuning can be done in config.
        candidates: list[str] = []
        for candidate in [self._settings.model_name, *self._settings.model_fallbacks]:
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def _is_retryable_model_error(self, exc: Exception) -> bool:
        """Return True when the error looks like a model availability failure."""

        message = str(exc).lower()
        retryable_markers = [
            "not found",
            "unavailable",
            "404",
            "model",
        ]
        return any(marker in message for marker in retryable_markers)

    def _is_quota_error(self, exc: Exception) -> bool:
        """Return True when Gemini reports a quota or rate-limit exhaustion."""

        message = str(exc).lower()
        quota_markers = [
            "resource_exhausted",
            "quota exceeded",
            "rate limit",
            "generate_content_free_tier_requests",
            "please retry in",
            "429",
        ]
        status_code = getattr(exc, "status_code", None)
        if status_code == 429:
            return True
        return any(marker in message for marker in quota_markers)

    def _fallback_reply(self, last_error: Exception | None, quota_exhausted: bool) -> str:
        """Choose a clear user-facing message based on the last Gemini failure."""

        if quota_exhausted:
            return (
                "Gemini quota is exhausted right now. Please wait for the limit to reset "
                "or switch to a higher-quota API key, then try again."
            )

        if last_error is not None and self._is_retryable_model_error(last_error):
            return (
                "The selected Gemini model is unavailable right now. "
                "The orchestrator tried fallback models, but none were reachable. "
                "Please try again in a moment."
            )

        return (
            "I’m having trouble reaching the reasoning model right now. "
            "Please try again in a moment."
        )

    def _system_instruction(self, system_prompt: str) -> str:
        tool_names = ", ".join(tool.name for tool in self._registry.list_tools()) or "no registered tools"
        return (
            f"{system_prompt.strip()}\n\n"
            f"Available tools: {tool_names}.\n"
            "Only call a tool when the user clearly needs it. "
            "If the message is a greeting or ordinary conversation, answer directly."
        )

    def _build_function_declarations(self) -> list[types.FunctionDeclaration]:
        declarations: list[types.FunctionDeclaration] = []
        for tool in self._registry.list_tools():
            declarations.append(
                types.FunctionDeclaration(
                    name=tool.name,
                    description=tool.description,
                    parameters=self._json_schema_to_gemini_schema(tool.parameters),
                )
            )
        return declarations

    def _json_schema_to_gemini_schema(self, schema: dict[str, Any]) -> types.Schema:
        schema_type = schema.get("type")
        gemini_type = None
        if isinstance(schema_type, str):
            try:
                gemini_type = types.Type[schema_type.upper()]
            except Exception:
                gemini_type = None

        properties_value = schema.get("properties", {})
        gemini_properties: dict[str, types.Schema] = {}
        if isinstance(properties_value, dict):
            for property_name, property_schema in properties_value.items():
                if isinstance(property_schema, dict):
                    gemini_properties[property_name] = self._json_schema_to_gemini_schema(property_schema)

        items_value = schema.get("items")
        gemini_items = self._json_schema_to_gemini_schema(items_value) if isinstance(items_value, dict) else None

        schema_kwargs: dict[str, Any] = {}
        for key in (
            "minItems",
            "example",
            "propertyOrdering",
            "pattern",
            "minimum",
            "default",
            "anyOf",
            "maxLength",
            "title",
            "minLength",
            "minProperties",
            "maxItems",
            "maximum",
            "nullable",
            "maxProperties",
            "description",
            "enum",
            "format",
            "required",
        ):
            if key in schema:
                schema_kwargs[key] = schema[key]

        if gemini_type is not None:
            schema_kwargs["type"] = gemini_type
        if gemini_properties:
            schema_kwargs["properties"] = gemini_properties
        if gemini_items is not None:
            schema_kwargs["items"] = gemini_items

        return types.Schema(**schema_kwargs)

    def _to_gemini_content(self, message: Message) -> types.Content | None:
        if message.role == "system":
            return None

        if message.role == "tool":
            tool_name = message.tool_calls[0].name if message.tool_calls else message.tool_call_id or "tool"
            tool_call_id = message.tool_calls[0].id if message.tool_calls else message.tool_call_id
            return types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=tool_call_id,
                            name=tool_name,
                            response={"result": message.content},
                        )
                    )
                ],
            )

        if message.role == "assistant" and message.tool_calls:
            parts: list[types.Part] = []
            for tool_call in message.tool_calls:
                parts.append(
                    types.Part(
                        function_call=types.FunctionCall(
                            id=tool_call.id,
                            name=tool_call.name,
                            args=tool_call.arguments,
                        )
                    )
                )
            return types.Content(role="model", parts=parts)

        return types.Content(role="user" if message.role == "user" else "model", parts=[types.Part(text=message.content)])

    def _extract_text(self, response: Any) -> str:
        text = getattr(response, "text", None)
        if text:
            return str(text).strip()

        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return ""

        candidate = candidates[0]
        content = getattr(candidate, "content", None)
        if not content:
            return ""

        parts = getattr(content, "parts", None) or []
        texts: list[str] = []
        for part in parts:
            part_text = getattr(part, "text", None)
            if part_text:
                texts.append(str(part_text))
        return "\n".join(texts).strip()

    def _extract_tool_calls(self, response: Any) -> list[ToolCall]:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return []

        content = getattr(candidates[0], "content", None)
        if not content:
            return []

        parts = getattr(content, "parts", None) or []
        tool_calls: list[ToolCall] = []
        for part in parts:
            # Gemini can return either snake_case or camelCase function-call payloads.
            function_call = getattr(part, "function_call", None) or getattr(part, "functionCall", None)
            if not function_call:
                continue

            call_id = getattr(function_call, "id", None) or ""
            call_name = getattr(function_call, "name", None) or ""
            try:
                call_args = getattr(function_call, "args", None) or {}
                if not isinstance(call_args, dict):
                    call_args = dict(call_args)
            except Exception:
                logger.warning("Skipping malformed tool call arguments for %s", call_name or "unknown")
                continue

            if call_name:
                tool_calls.append(ToolCall(id=str(call_id), name=str(call_name), arguments=call_args))

        return tool_calls

    def _tool_result_to_response(self, result: ToolResult) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool_name": result.tool_name,
            "result": result.content,
            "success": result.success,
            "metadata": result.metadata,
        }

        try:
            parsed = json.loads(result.content)
        except (TypeError, ValueError):
            return payload

        if isinstance(parsed, dict):
            payload["result"] = parsed.get("result", result.content)
            action_url = parsed.get("download_url") or parsed.get("file_url")
            if isinstance(action_url, str) and action_url.strip():
                payload["action"] = {
                    "type": "download",
                    "url": self._proxy_action_url(action_url.strip(), result.tool_name),
                    "label": self._action_label(result.tool_name),
                }

        return payload

    def _action_label(self, tool_name: str) -> str:
        label = tool_name.replace("_", " ").strip()
        if not label:
            return "Download"
        return f"Download {label.title()}"

    def _resolve_action_url(self, action_url: str, tool_name: str) -> str:
        if not action_url:
            return action_url

        parsed = urlparse(action_url)
        if parsed.scheme and parsed.netloc:
            return action_url

        try:
            tool = self._registry.get(tool_name)
        except KeyError:
            return action_url

        if not tool.endpoint:
            return action_url

        return urljoin(str(tool.endpoint), action_url)

    def _extract_download_filename(self, action_url: str) -> str | None:
        return extract_slide_filename(action_url)

    def _proxy_action_url(self, action_url: str, tool_name: str) -> str:
        resolved_url = self._resolve_action_url(action_url, tool_name)
        if tool_name != "slide_generator":
            return resolved_url

        filename = self._extract_download_filename(resolved_url)
        if not filename:
            return resolved_url
        return f"/downloads/slide/{filename}"

    def _tool_result_action(self, result: ToolResult) -> dict[str, Any] | None:
        try:
            parsed = json.loads(result.content)
        except (TypeError, ValueError):
            return None

        if not isinstance(parsed, dict):
            return None

        action_url = parsed.get("download_url") or parsed.get("file_url")
        if not isinstance(action_url, str) or not action_url.strip():
            return None

        resolved_url = self._proxy_action_url(action_url.strip(), result.tool_name)

        return {
            "type": "download",
            "url": resolved_url,
            "label": self._action_label(result.tool_name),
        }

    def _download_ready_reply(self, tool_name: str) -> str:
        if tool_name == "slide_generator":
            return "Your slide deck is ready. Click the download button below."

        return "Your file is ready. Click the download button below."

    async def _invoke_tool(self, tool_call: ToolCall, session_id: str, request_id: str | None = None) -> ToolResult:
        try:
            try:
                return await self._invoker.invoke(
                    tool_call.name,
                    tool_call.arguments,
                    session_id,
                    request_id=request_id,
                )
            except TypeError:
                # Backward compatibility for invokers that still implement the 3-argument signature.
                return await self._invoker.invoke(tool_call.name, tool_call.arguments, session_id)
        except Exception as exc:
            logger.exception("Tool invocation failed for %s", tool_call.name)
            return ToolResult(
                tool_name=tool_call.name,
                content=str({"error": f"Tool unavailable: {exc}"}),
                success=False,
                metadata={"session_id": session_id},
            )

    async def route(
        self,
        messages: list[Message],
        system_prompt: str,
        session_id: str,
        request_id: str | None = None,
    ) -> RouteResult:
        if self._client is None:
            return RouteResult(reply="Gemini is not configured. Set GOOGLE_API_KEY to enable the router.")

        gemini_contents: list[types.Content] = [
            gemini_content
            for message in messages
            if (gemini_content := self._to_gemini_content(message)) is not None
        ]
        function_declarations = self._build_function_declarations()
        tool_config = types.Tool(function_declarations=function_declarations) if function_declarations else None

        conversation: list[types.Content] = list(gemini_contents)
        tool_results: list[ToolResult] = []
        first_tool_used: str | None = None
        action: dict[str, Any] | None = None

        # Cap the loop so a tool-heavy prompt cannot spin forever.
        for iteration in range(1, 6):
            response: Any | None = None
            last_error: Exception | None = None
            quota_exhausted = False

            for model_name in self._model_candidates():
                system_instruction = self._system_instruction(system_prompt)
                attempt_started = time.perf_counter()
                self._log_event(
                    logging.INFO,
                    "gemini.turn.attempt.start",
                    request_id=request_id,
                    session_id=session_id,
                    turn_index=iteration,
                    model_name=model_name,
                    conversation_parts_count=len(conversation),
                    tool_declarations_count=len(function_declarations),
                    system_instruction_len=len(system_instruction),
                    temperature=0.3,
                )
                try:
                    config_kwargs: dict[str, Any] = {
                        "systemInstruction": system_instruction,
                        "temperature": 0.3,
                    }
                    if tool_config is not None:
                        config_kwargs["tools"] = [tool_config]

                    response = self._client.models.generate_content(
                        model=model_name,
                        contents=cast(Any, conversation),
                        config=types.GenerateContentConfig(**config_kwargs),
                    )
                    usage = self._extract_usage(response)
                    finish_reason = None
                    candidates = getattr(response, "candidates", None) or []
                    if candidates:
                        finish_reason = getattr(candidates[0], "finish_reason", None) or getattr(
                            candidates[0], "finishReason", None
                        )
                    self._log_event(
                        logging.INFO,
                        "gemini.turn.attempt.success",
                        request_id=request_id,
                        session_id=session_id,
                        turn_index=iteration,
                        model_name=model_name,
                        latency_ms=round((time.perf_counter() - attempt_started) * 1000, 2),
                        finish_reason=finish_reason,
                        candidate_count=len(candidates),
                        response_id=getattr(response, "response_id", None),
                        **usage,
                    )
                    # If the selected model works, stop trying fallbacks immediately.
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    raw_message = str(exc)
                    lowered = raw_message.lower()
                    retry_delay_seconds = self._parse_retry_delay_seconds(raw_message)
                    quota_metric = "generate_content_free_tier_requests" if "generate_content_free_tier_requests" in lowered else None
                    if self._is_quota_error(exc):
                        quota_exhausted = True
                    self._log_event(
                        logging.WARNING,
                        "gemini.turn.attempt.error",
                        request_id=request_id,
                        session_id=session_id,
                        turn_index=iteration,
                        model_name=model_name,
                        latency_ms=round((time.perf_counter() - attempt_started) * 1000, 2),
                        error_type=type(exc).__name__,
                        status_code=getattr(exc, "status_code", None),
                        is_quota_error=self._is_quota_error(exc),
                        is_retryable_model_error=self._is_retryable_model_error(exc),
                        retry_delay_seconds_parsed=retry_delay_seconds,
                        quota_metric=quota_metric,
                        raw_error_hash=self._hash_preview(raw_message),
                    )
                    if self._is_retryable_model_error(exc):
                        logger.warning("Gemini model %s failed on attempt %s: %s", model_name, iteration, exc)
                    else:
                        logger.warning(
                            "Gemini model %s failed on attempt %s with a non-model error: %s",
                            model_name,
                            iteration,
                            exc,
                        )
                    # Keep probing alternate models so one unavailable endpoint
                    # does not immediately surface as a user-facing failure.
                    continue

            if response is None:
                logger.error("Gemini model call failed after model fallbacks: %s", last_error)
                self._log_event(
                    logging.ERROR,
                    "gemini.turn.exhausted",
                    request_id=request_id,
                    session_id=session_id,
                    turn_index=iteration,
                    models_tried=self._model_candidates(),
                    quota_exhausted_any=quota_exhausted,
                    selected_fallback_message_kind=(
                        "quota"
                        if quota_exhausted
                        else "model_unavailable"
                        if last_error is not None and self._is_retryable_model_error(last_error)
                        else "generic"
                    ),
                )
                return RouteResult(
                    reply=self._fallback_reply(last_error, quota_exhausted),
                    tool_used=first_tool_used,
                    action=action,
                )

            tool_calls = self._extract_tool_calls(response)
            self._log_event(
                logging.INFO,
                "gemini.turn.tool_calls",
                request_id=request_id,
                session_id=session_id,
                turn_index=iteration,
                tool_calls_count=len(tool_calls),
                tool_names=[tool_call.name for tool_call in tool_calls],
                tool_args_keys=[list(tool_call.arguments.keys())[:10] for tool_call in tool_calls],
            )
            if not tool_calls:
                final_text = self._extract_text(response)
                if not final_text:
                    final_text = "I could not produce a response right now. Please try again."

                return RouteResult(reply=final_text, tool_used=first_tool_used, action=action)

            assistant_parts: list[types.Part] = []
            for tool_call in tool_calls:
                if first_tool_used is None:
                    first_tool_used = tool_call.name
                assistant_parts.append(
                    types.Part(
                        function_call=types.FunctionCall(
                            id=tool_call.id,
                            name=tool_call.name,
                            args=tool_call.arguments,
                        )
                    )
                )
            conversation.append(types.Content(role="model", parts=assistant_parts))

            tool_response_parts: list[types.Part] = []
            for tool_call in tool_calls:
                tool_result = await self._invoke_tool(tool_call, session_id, request_id=request_id)
                tool_results.append(tool_result)
                if action is None:
                    action = self._tool_result_action(tool_result)
                if action is not None:
                    return RouteResult(
                        reply=self._download_ready_reply(first_tool_used or tool_call.name),
                        tool_used=first_tool_used or tool_call.name,
                        action=action,
                    )
                tool_response_parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=tool_call.id,
                            name=tool_call.name,
                            response=self._tool_result_to_response(tool_result),
                        )
                    )
                )

            conversation.append(types.Content(role="user", parts=tool_response_parts))

        return RouteResult(
            reply=(
                "I completed the tool steps, but the model kept requesting more actions. "
                "Please rephrase your request and try again."
            ),
            tool_used=first_tool_used,
            action=action,
        )

    async def process(
        self,
        messages: list[Message],
        system_prompt: str,
        session_id: str,
        request_id: str | None = None,
    ) -> RouteResult:
        """Compatibility wrapper for older call sites."""

        return await self.route(
            messages=messages,
            system_prompt=system_prompt,
            session_id=session_id,
            request_id=request_id,
        )
