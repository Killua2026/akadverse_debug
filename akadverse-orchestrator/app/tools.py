"""Tool registry and tool invocation contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
import json
import logging
import time
from typing import Any

import httpx

from .config import OrchestratorSettings
from .models import ToolDefinition, ToolResult

logger = logging.getLogger(__name__)


def _log_event(level: int, event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    try:
        logger.log(level, json.dumps(payload, ensure_ascii=True, default=str))
    except Exception:
        logger.log(level, "%s %s", event, fields)


class ToolRegistry:
    """Stores the tool definitions exposed to Gemini and the invoker."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    @classmethod
    def from_settings(cls, settings: OrchestratorSettings) -> "ToolRegistry":
        registry = cls()
        for tool in settings.tools:
            registry.register(tool)
        return registry

    def register(self, tool: ToolDefinition) -> None:
        # Later registrations intentionally replace earlier ones so settings can override defaults.
        self._tools[tool.name] = tool

    def get(self, tool_name: str) -> ToolDefinition:
        try:
            return self._tools[tool_name]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {tool_name}") from exc

    def list_tools(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def to_gemini_tool_schema(self) -> list[dict[str, Any]]:
        return [tool.model_dump() for tool in self._tools.values()]


class ToolInvoker(ABC):
    """Abstract invocation boundary for HTTP now and Red Panda later."""

    @abstractmethod
    async def invoke(
        self,
        tool_name: str,
        params: dict[str, Any],
        session_id: str,
        request_id: str | None = None,
    ) -> ToolResult:
        raise NotImplementedError


class HttpToolInvoker(ToolInvoker):
    """Async HTTP tool invoker with timeout and retry support."""

    def __init__(self, registry: ToolRegistry, default_headers: dict[str, str] | None = None) -> None:
        self._registry = registry
        self._default_headers = default_headers or {}

    async def _post_once(
        self,
        client: httpx.AsyncClient,
        tool: ToolDefinition,
        params: dict[str, Any],
        session_id: str,
        request_id: str | None = None,
    ) -> httpx.Response:
        if not tool.endpoint:
            raise ValueError(f"Tool {tool.name} is missing an endpoint")

        headers = {**self._default_headers, "X-Akadverse-Session-Id": session_id, "X-Akadverse-Tool-Name": tool.name}
        _log_event(
            logging.INFO,
            "tool.http.request",
            request_id=request_id,
            session_id=session_id,
            tool_name=tool.name,
            endpoint=tool.endpoint,
            request_format=tool.request_format,
            timeout_seconds=tool.timeout_seconds,
            retry_count=tool.retry_count,
            param_keys=list(params.keys())[:10],
            param_sizes={key: len(str(value)) for key, value in list(params.items())[:10]},
        )
        if tool.request_format == "form":
            return await client.post(str(tool.endpoint), data=params, headers=headers)

        payload = {
            "session_id": session_id,
            "tool_name": tool.name,
            "params": params,
        }
        return await client.post(str(tool.endpoint), json=payload, headers=headers)

    async def invoke(
        self,
        tool_name: str,
        params: dict[str, Any],
        session_id: str,
        request_id: str | None = None,
    ) -> ToolResult:
        try:
            tool = self._registry.get(tool_name)
        except KeyError as exc:
            logger.warning("Tool request rejected because the tool is unknown: %s", tool_name)
            return ToolResult(
                tool_name=tool_name,
                content=str({"error": str(exc)}),
                success=False,
                metadata={"session_id": session_id},
            )

        timeout = httpx.Timeout(tool.timeout_seconds)
        attempts = max(tool.retry_count, 0) + 1

        try:
            async with httpx.AsyncClient(timeout=timeout, headers=self._default_headers) as client:
                response: httpx.Response | None = None

                for attempt in range(attempts):
                    try:
                        attempt_started = time.perf_counter()
                        response = await self._post_once(
                            client,
                            tool,
                            params,
                            session_id,
                            request_id=request_id,
                        )
                        response.raise_for_status()
                        _log_event(
                            logging.INFO,
                            "tool.http.response",
                            request_id=request_id,
                            session_id=session_id,
                            tool_name=tool_name,
                            status_code=response.status_code,
                            latency_ms=round((time.perf_counter() - attempt_started) * 1000, 2),
                            content_type=response.headers.get("content-type", ""),
                            body_size_bytes=len(response.content or b""),
                            success=True,
                        )
                        break
                    except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError) as exc:
                        _log_event(
                            logging.WARNING,
                            "tool.http.error",
                            request_id=request_id,
                            session_id=session_id,
                            tool_name=tool_name,
                            attempt=attempt + 1,
                            attempts_total=attempts,
                            error_type=type(exc).__name__,
                            status_code=(
                                exc.response.status_code
                                if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None
                                else None
                            ),
                            timeout=isinstance(exc, httpx.TimeoutException),
                            endpoint=tool.endpoint,
                        )
                        logger.warning(
                            "Tool %s attempt %s/%s failed: %s",
                            tool_name,
                            attempt + 1,
                            attempts,
                            exc,
                        )
                        if attempt >= attempts - 1:
                            raise

                if response is None:
                    raise RuntimeError(f"Tool {tool_name} did not return a response")

                content_type = response.headers.get("content-type", "").lower()
                try:
                    if "application/json" in content_type:
                        body: Any = response.json()
                    else:
                        body = response.text
                except ValueError as exc:
                    logger.warning("Tool %s returned an invalid response body", tool_name)
                    return ToolResult(
                        tool_name=tool_name,
                        content=str({"error": f"Invalid response body: {exc}"}),
                        success=False,
                        metadata={"status_code": response.status_code, "session_id": session_id},
                    )

                if isinstance(body, (dict, list, int, float, bool)) or body is None:
                    content = json.dumps(body, ensure_ascii=False)
                else:
                    content = str(body)

                return ToolResult(
                    tool_name=tool_name,
                    content=content,
                    success=True,
                    metadata={"status_code": response.status_code, "session_id": session_id},
                )
        except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError, ValueError, RuntimeError) as exc:
            logger.exception("Tool invocation failed for %s", tool_name)
            return ToolResult(
                tool_name=tool_name,
                content=str({"error": f"Tool unavailable: {exc}"}),
                success=False,
                metadata={"session_id": session_id},
            )
        except Exception as exc:
            logger.exception("Unexpected tool invocation failure for %s", tool_name)
            return ToolResult(
                tool_name=tool_name,
                content=str({"error": f"Unexpected tool failure: {exc}"}),
                success=False,
                metadata={"session_id": session_id},
            )


class MockToolInvoker(ToolInvoker):
    """Deterministic invoker useful for early router tests."""

    async def invoke(
        self,
        tool_name: str,
        params: dict[str, Any],
        session_id: str,
        request_id: str | None = None,
    ) -> ToolResult:
        return ToolResult(
            tool_name=tool_name,
            content=str({"tool": tool_name, "params": params, "session_id": session_id}),
            success=True,
        )
