"""Compatibility wrapper for the AkadVerse orchestrator FastAPI app."""

from __future__ import annotations

from app.main import app

__all__ = ["app"]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)