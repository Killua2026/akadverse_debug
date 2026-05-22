"""Helpers for slide download URL/filename safety."""

from __future__ import annotations

import posixpath
from urllib.parse import unquote, urlparse

ALLOWED_SLIDE_EXTENSIONS = (".pptx", ".json")
ALLOWED_FILENAME_CHARS = set("._-")


def validate_slide_filename(filename: str) -> str | None:
    if not filename:
        return None
    if "/" in filename or "\\" in filename:
        return None
    if not filename.lower().endswith(ALLOWED_SLIDE_EXTENSIONS):
        return None
    if not all(char.isalnum() or char in ALLOWED_FILENAME_CHARS for char in filename):
        return None
    return filename


def extract_slide_filename(action_url: str) -> str | None:
    if not action_url:
        return None
    parsed = urlparse(action_url)
    raw_path = parsed.path or action_url
    return validate_slide_filename(posixpath.basename(unquote(raw_path)))
