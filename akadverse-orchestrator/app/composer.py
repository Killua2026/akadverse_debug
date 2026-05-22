"""Response composition for the final assistant output. 
For the first version, we simply passed the router’s final text through unchanged. 
Later we can add markdown rendering or special formatting.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .models import RouteResult


class ResponseComposer:
    """Converts router output into the outward-facing API payload."""

    def compose(self, route_result: RouteResult | Sequence[object] | None = None) -> dict[str, Any]:
        if isinstance(route_result, RouteResult):
            return {
                "reply": route_result.reply,
                "tool_used": route_result.tool_used,
                "action": route_result.action,
            }

        if isinstance(route_result, Sequence):
            # Backward compatibility for older call sites that still pass tool result collections.
            return {"reply": "", "tool_used": None, "action": None}

        return {"reply": "", "tool_used": None, "action": None}
