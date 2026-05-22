"""
Akadverse - Slide Generator AI
==============================
Same input logic as Notes Creator but outputs a PowerPoint presentation (.pptx)
instead of text notes.

Accepts: plain text, PDFs, PPTX files, URLs, images
Outputs: downloadable .pptx presentation

LLM: Gemini via LangChain - structures content into slides
PPTX: python-pptx - builds the actual PowerPoint file
"""

import asyncio
import json
import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime
from typing import Any, Optional, TypedDict, Literal, cast

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

load_dotenv()

logger = logging.getLogger(__name__)
os.environ.setdefault("USER_AGENT", "AkadVerseBot/1.0")


def _log_event(level: int, event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    try:
        logger.log(level, json.dumps(payload, ensure_ascii=True, default=str))
    except Exception:
        logger.log(level, "%s %s", event, fields)


def _extract_langchain_token_usage(response: Any) -> dict[str, Any]:
    metadata = getattr(response, "response_metadata", None)
    if not isinstance(metadata, dict):
        return {}

    usage = metadata.get("token_usage") or metadata.get("usage_metadata") or {}
    if not isinstance(usage, dict):
        return {}

    return {
        "prompt_tokens": usage.get("prompt_tokens") or usage.get("input_tokens"),
        "candidate_tokens": usage.get("completion_tokens") or usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def _absolute_download_url(base_url: str, filename: str) -> str:
    base = str(base_url).rstrip("/") + "/"
    return f"{base}slides/download/{filename}"

MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "10"))
THEME_CLASSIFIER_CONFIDENCE_THRESHOLD = float(os.getenv("THEME_CLASSIFIER_CONFIDENCE_THRESHOLD", "0.60"))
THEME_CLASSIFIER_CACHE_TTL_SEC = int(os.getenv("THEME_CLASSIFIER_CACHE_TTL_SEC", "300"))
THEME_CLASSIFIER_CONTENT_PREVIEW_CHARS = int(os.getenv("THEME_CLASSIFIER_CONTENT_PREVIEW_CHARS", "1800"))

class ThemePalette(TypedDict):
    name: str
    keywords: list[str]
    bg_dark: Any
    bg_slide: Any
    accent: Any
    accent_soft: Any
    title_text: Any
    body_text: Any
    muted: Any


class ThemeCacheRecord(TypedDict):
    domain: str
    confidence: float
    source: str
    expires_at: float


class ThemeSelection(TypedDict):
    domain: str
    confidence: float
    source: Literal["gemini_classifier", "keyword_fallback", "default"]
    theme: ThemePalette


_THEME_CLASSIFIER_CACHE: dict[str, ThemeCacheRecord] = {}

# ─────────────────────────────────────────────
# LLM - Gemini via LangChain
# ─────────────────────────────────────────────
try:
    from langchain_google_genai import ChatGoogleGenerativeAI
except Exception as e:
    llm = None
    LLM_AVAILABLE = False
    LLM_MODEL = "mock"
    ChatGoogleGenerativeAI = None  # type: ignore
    logger.warning("LLM import failed, running in mock mode: %s", e)

try:
    from google import genai
except Exception as e:
    genai = None  # type: ignore
    logger.warning("google-genai not available, using fallback model discovery: %s", e)


def _is_text_model(model_name: str) -> bool:
    """Allow only Gemini text generation models."""
    normalized = model_name.lower()
    if not normalized.startswith("gemini"):
        return False

    blocked_tokens = ["embedding", "image", "vision", "audio", "speech", "transcribe", "tts"]
    return not any(token in normalized for token in blocked_tokens)


def discover_gemini_model(api_key: str) -> str:
    """Discover the best available Gemini model dynamically."""
    if not api_key or genai is None:
        return "gemini-2.0-flash-lite"

    try:
        client = genai.Client(api_key=api_key)
        discovered_models: list[str] = []

        for model in client.models.list():
            model_name = getattr(model, "name", "")
            if not isinstance(model_name, str) or not model_name:
                continue

            normalized_name = model_name.replace("models/", "")
            if _is_text_model(normalized_name):
                discovered_models.append(normalized_name)

        priority_order = [
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-2.5-pro",
            "gemini-pro",
        ]

        for preferred_model in priority_order:
            if preferred_model in discovered_models:
                return preferred_model

        if discovered_models:
            return discovered_models[0]
    except Exception as e:
        logger.warning("Gemini model discovery failed, falling back to default: %s", e)

    return "gemini-2.0-flash-lite"


def initialize_llm() -> None:
    """Initialize Gemini through LangChain with graceful fallback."""
    global llm, LLM_AVAILABLE, LLM_MODEL

    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not api_key:
        logger.warning("GOOGLE_API_KEY is not set. Running in mock mode.")
        llm = None
        LLM_AVAILABLE = False
        LLM_MODEL = "mock"
        return

    if ChatGoogleGenerativeAI is None:
        logger.warning("langchain-google-genai is not installed. Running in mock mode.")
        llm = None
        LLM_AVAILABLE = False
        LLM_MODEL = "mock"
        return

    model_name = discover_gemini_model(api_key)

    try:
        llm = ChatGoogleGenerativeAI(
            model=model_name,
            temperature=0.3,
            google_api_key=api_key,
            max_retries=1,
        )
        LLM_MODEL = model_name
        LLM_AVAILABLE = True
        logger.info("Gemini ready via LangChain: %s", LLM_MODEL)
    except Exception as e:
        llm = None
        LLM_AVAILABLE = False
        LLM_MODEL = "mock"
        logger.warning("Gemini initialization failed, running in mock mode: %s", e)


initialize_llm()

# ─────────────────────────────────────────────
# PPTX BUILDER - python-pptx
# ─────────────────────────────────────────────
try:
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN
    PPTX_AVAILABLE = True
except ImportError:
    PPTX_AVAILABLE = False
    logger.warning("python-pptx not installed. Install with: pip install python-pptx")

# ─────────────────────────────────────────────
# DOCUMENT LOADERS (shared with Notes Creator)
# ─────────────────────────────────────────────
try:
    from langchain_community.document_loaders import PyPDFLoader, WebBaseLoader, UnstructuredPowerPointLoader
    LOADERS_AVAILABLE = True
except ImportError:
    LOADERS_AVAILABLE = False

try:
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

try:
    from google.genai import types
except ImportError:
    types = None  # type: ignore[assignment]

GEMINI_VISION_AVAILABLE = genai is not None and types is not None


# ─────────────────────────────────────────────
# SLIDE STRUCTURE PROMPT
# ─────────────────────────────────────────────

SLIDE_PROMPT = """You are an expert presentation designer. Convert the content below into a structured PowerPoint presentation outline.

Return ONLY valid JSON in this exact format (no markdown, no explanation):
{{
  "title": "Presentation Title",
  "subtitle": "Optional subtitle or subject",
  "slides": [
    {{
      "slide_number": 1,
      "title": "Slide Title",
            "intro": "One to two sentence overview for the slide",
            "sections": [
                {{
                    "heading": "Section heading",
                    "body": "One to three sentences of explanation.",
                    "bullets": ["Supporting detail 1", "Supporting detail 2"]
                }}
            ],
      "speaker_note": "Brief note for the presenter"
    }}
  ]
}}

Rules:
- Create {num_slides} slides (excluding title slide)
- Each slide: 1 clear title + 3-5 bullet points
- Each slide should feel descriptive and instructional, not like a flat bullet dump
- Prefer 2-3 sections per slide with a short heading and explanatory body text
- If a point needs emphasis, put it in a section heading, not in markdown bold inside body text
- Bullet points should be concise supporting details, not the main content
- First slide should be an overview/agenda
- Last slide should be a summary/key takeaways
- Speaker notes should add context not in the bullets

Subject: {subject}
Content:
{content}"""


# ─────────────────────────────────────────────
# CONTENT LOADERS (reused from Notes Creator)
# ─────────────────────────────────────────────

def load_content(source_type: str, content: Optional[str] = None,
                 file_path: Optional[str] = None, url: Optional[str] = None) -> str:
    if source_type == "text":
        return content or ""
    elif source_type == "url":
        if not url:
            return ""
        if not LOADERS_AVAILABLE:
            return f"[MOCK] Content from {url}"
        loader = WebBaseLoader(url)
        docs = loader.load()
        return "\n".join([d.page_content for d in docs])
    elif source_type == "pdf":
        if not file_path:
            return ""
        if not LOADERS_AVAILABLE:
            return f"[MOCK] PDF content from {file_path}"
        loader = PyPDFLoader(file_path)
        pages = loader.load()
        return "\n\n".join([p.page_content for p in pages])
    elif source_type == "pptx":
        if not file_path:
            return ""
        if not LOADERS_AVAILABLE:
            return f"[MOCK] PPTX content from {file_path}"
        loader = UnstructuredPowerPointLoader(file_path)
        docs = loader.load()
        return "\n\n".join([d.page_content for d in docs])
    elif source_type == "image":
        if not file_path:
            return ""
        if not OCR_AVAILABLE:
            return "[MOCK] Text extracted from image via OCR"
        img = Image.open(file_path)
        return pytesseract.image_to_string(img, config="--psm 6")
    return ""


def _normalize_extracted_text(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text or "").strip()


def _score_ocr_output(text: str, word_confidences: list[float]) -> dict[str, float | int]:
    normalized = _normalize_extracted_text(text)
    words = re.findall(r"[A-Za-z0-9']+", normalized)
    alpha_count = sum(1 for char in normalized if char.isalpha())
    total_count = max(len(normalized), 1)
    alpha_ratio = alpha_count / total_count
    mean_confidence = sum(word_confidences) / len(word_confidences) if word_confidences else 0.0

    return {
        "characters": len(normalized),
        "words": len(words),
        "alpha_ratio": round(alpha_ratio, 3),
        "mean_confidence": round(mean_confidence, 2),
    }


def _should_use_gemini_vision(text: str, quality: dict[str, float | int]) -> bool:
    normalized = _normalize_extracted_text(text)
    word_count = int(quality.get("words", 0))
    character_count = int(quality.get("characters", 0))
    alpha_ratio = float(quality.get("alpha_ratio", 0.0))
    mean_confidence = float(quality.get("mean_confidence", 0.0))

    if not normalized:
        return True
    if word_count < 5:
        return True
    if character_count < 35:
        return True
    if alpha_ratio < 0.55:
        return True
    if mean_confidence and mean_confidence < 55:
        return True
    return False


def _extract_text_with_tesseract(image_path: str) -> tuple[str, dict[str, float | int]]:
    if not OCR_AVAILABLE:
        return "", {"characters": 0, "words": 0, "alpha_ratio": 0.0, "mean_confidence": 0.0}

    img = Image.open(image_path)
    grayscale = img.convert("L")
    raw_text = pytesseract.image_to_string(grayscale, config="--psm 6")

    try:
        data = pytesseract.image_to_data(grayscale, output_type=pytesseract.Output.DICT, config="--psm 6")
        confidences = [float(value) for value in data.get("conf", []) if str(value).strip() not in {"", "-1"}]
    except Exception:
        confidences = []

    cleaned_text = _normalize_extracted_text(raw_text)
    return cleaned_text, _score_ocr_output(cleaned_text, confidences)


def _extract_text_with_gemini_vision(image_path: str, request_id: str | None = None) -> str:
    if not GEMINI_VISION_AVAILABLE or genai is None or types is None:
        raise RuntimeError("Gemini Vision is not available in this environment")

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is required for Gemini Vision fallback")

    with open(image_path, "rb") as image_file:
        image_bytes = image_file.read()

    image_extension = os.path.splitext(image_path)[1].lower()
    mime_type = {
        ".png": "image/png",
        ".webp": "image/webp",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".heic": "image/heic",
        ".heif": "image/heif",
    }.get(image_extension, "image/jpeg")

    client = genai.Client(api_key=api_key)
    prompt = (
        "Extract all readable text from this image. Preserve line breaks, names, "
        "headings, and obvious structure. If the image contains handwriting or a "
        "low-quality scan, infer the most likely text from context and return only "
        "the extracted text without commentary."
    )

    model_name = LLM_MODEL if LLM_AVAILABLE and LLM_MODEL.startswith("gemini") else "gemini-2.5-flash"
    started = time.perf_counter()
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                prompt,
            ],
        )
        _log_event(
            logging.INFO,
            "slide.vision.gemini",
            request_id=request_id,
            model_name=model_name,
            mime_type=mime_type,
            image_bytes=len(image_bytes),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            status="ok",
        )
    except Exception as exc:
        _log_event(
            logging.WARNING,
            "slide.vision.gemini",
            request_id=request_id,
            model_name=model_name,
            mime_type=mime_type,
            image_bytes=len(image_bytes),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            status="error",
            error_type=type(exc).__name__,
        )
        raise

    vision_text = _normalize_extracted_text(response.text or "")
    if not vision_text:
        raise RuntimeError("Gemini Vision returned no extractable text")
    return vision_text


def extract_text_from_image(image_path: str) -> tuple[str, str, dict[str, float | int]]:
    if not OCR_AVAILABLE and not GEMINI_VISION_AVAILABLE:
        return "[MOCK IMAGE TEXT] OCR skipped.", "mock", {"characters": 0, "words": 0, "alpha_ratio": 0.0, "mean_confidence": 0.0}

    if not OCR_AVAILABLE:
        vision_text = _extract_text_with_gemini_vision(image_path)
        quality = _score_ocr_output(vision_text, [])
        return vision_text, "gemini_vision", quality

    tesseract_text, quality = _extract_text_with_tesseract(image_path)
    if tesseract_text and not _should_use_gemini_vision(tesseract_text, quality):
        return tesseract_text, "tesseract", quality

    if GEMINI_VISION_AVAILABLE:
        try:
            vision_text = _extract_text_with_gemini_vision(image_path)
            vision_quality = _score_ocr_output(vision_text, [])
            return vision_text, "gemini_vision", vision_quality
        except Exception as vision_error:
            logger.warning("Gemini Vision fallback failed: %s", vision_error)
            if tesseract_text:
                return tesseract_text, "tesseract_fallback", quality
            raise RuntimeError(f"Image extraction failed: {vision_error}") from vision_error

    if tesseract_text:
        return tesseract_text, "tesseract", quality

    raise RuntimeError("Image extraction failed: no usable OCR text found")


def _parse_section_text(text: str) -> dict[str, object]:
    cleaned = (text or "").strip()
    heading_match = re.match(r"^\*\*(.+?)\*\*(.*)$", cleaned)
    if heading_match:
        heading = heading_match.group(1).strip().rstrip(":")
        body = heading_match.group(2).strip().lstrip(":- ")
        return {"heading": heading, "body": body, "bullets": []}

    parts = cleaned.split(":", 1)
    if len(parts) == 2 and len(parts[0].split()) <= 8:
        return {"heading": parts[0].strip().rstrip("."), "body": parts[1].strip(), "bullets": []}

    return {"heading": "", "body": cleaned, "bullets": []}


def _normalize_slide_sections(slide: dict) -> dict:
    normalized = {
        "slide_number": slide.get("slide_number"),
        "title": slide.get("title", "Slide"),
        "intro": slide.get("intro", ""),
        "sections": [],
        "speaker_note": slide.get("speaker_note", ""),
    }

    sections = slide.get("sections")
    if isinstance(sections, list) and sections:
        for section in sections:
            if not isinstance(section, dict):
                continue
            normalized["sections"].append({
                "heading": str(section.get("heading", "")).strip(),
                "body": str(section.get("body", "")).strip(),
                "bullets": [str(item).strip() for item in section.get("bullets", []) if str(item).strip()],
            })
        return normalized

    points = slide.get("points", [])
    if isinstance(points, list):
        for point in points:
            if not isinstance(point, str) or not point.strip():
                continue
            normalized["sections"].append(_parse_section_text(point))

    return normalized


def _normalize_structure(structure: dict) -> dict:
    slides = structure.get("slides", [])
    normalized_slides = []
    if isinstance(slides, list):
        for slide in slides:
            if isinstance(slide, dict):
                normalized_slides.append(_normalize_slide_sections(slide))

    normalized_structure = dict(structure)
    normalized_structure["slides"] = normalized_slides
    return normalized_structure


THEME_PALETTES: dict[str, ThemePalette] = {
    "legal_governance": {
        "name": "legal_governance",
        "keywords": ["law", "legal", "court", "constitution", "judiciary", "statute", "arbitration", "mediation", "conflict resolution", "dispute", "litigation", "tribunal", "criminal law", "civil law"],
        "bg_dark": RGBColor(0x2f, 0x25, 0x14),
        "bg_slide": RGBColor(0xfc, 0xf9, 0xf0),
        "accent": RGBColor(0xb2, 0x7b, 0x2c),
        "accent_soft": RGBColor(0xe8, 0xd5, 0xb1),
        "title_text": RGBColor(0xff, 0xf9, 0xed),
        "body_text": RGBColor(0x3d, 0x31, 0x1a),
        "muted": RGBColor(0x7a, 0x66, 0x44),
    },
    "science": {
        "name": "science",
        "keywords": ["biology", "chemistry", "physics", "medicine", "science", "photosynthesis", "cell", "lab", "experiment"],
        "bg_dark": RGBColor(0x0f, 0x2d, 0x3f),
        "bg_slide": RGBColor(0xf3, 0xf8, 0xfb),
        "accent": RGBColor(0x1d, 0xa1, 0x8f),
        "accent_soft": RGBColor(0xb9, 0xe8, 0xe1),
        "title_text": RGBColor(0xff, 0xff, 0xff),
        "body_text": RGBColor(0x18, 0x2b, 0x3a),
        "muted": RGBColor(0x67, 0x7c, 0x89),
    },
    "math": {
        "name": "math",
        "keywords": ["math", "algebra", "calculus", "geometry", "statistics", "equation", "matrix", "proof"],
        "bg_dark": RGBColor(0x1d, 0x16, 0x4f),
        "bg_slide": RGBColor(0xf7, 0xf5, 0xff),
        "accent": RGBColor(0x67, 0x5a, 0xdd),
        "accent_soft": RGBColor(0xd8, 0xd1, 0xfb),
        "title_text": RGBColor(0xff, 0xff, 0xff),
        "body_text": RGBColor(0x24, 0x1f, 0x52),
        "muted": RGBColor(0x6b, 0x68, 0x8c),
    },
    "history": {
        "name": "history",
        "keywords": ["history", "civilization", "empire", "war", "revolution", "ancient", "colonial", "timeline"],
        "bg_dark": RGBColor(0x4a, 0x2c, 0x17),
        "bg_slide": RGBColor(0xfc, 0xf6, 0xeb),
        "accent": RGBColor(0xc9, 0x84, 0x2b),
        "accent_soft": RGBColor(0xf1, 0xdd, 0xbd),
        "title_text": RGBColor(0xff, 0xf7, 0xed),
        "body_text": RGBColor(0x3a, 0x27, 0x1a),
        "muted": RGBColor(0x82, 0x66, 0x4d),
    },
    "business": {
        "name": "business",
        "keywords": ["business", "management", "finance", "marketing", "strategy", "market", "economics", "leadership"],
        "bg_dark": RGBColor(0x0f, 0x2a, 0x33),
        "bg_slide": RGBColor(0xf5, 0xfa, 0xfb),
        "accent": RGBColor(0xe0, 0x8e, 0x2f),
        "accent_soft": RGBColor(0xfa, 0xdf, 0xbe),
        "title_text": RGBColor(0xff, 0xff, 0xff),
        "body_text": RGBColor(0x18, 0x2c, 0x32),
        "muted": RGBColor(0x6b, 0x7b, 0x80),
    },
    "technology": {
        "name": "technology",
        "keywords": ["technology", "computer", "software", "artificial intelligence", "machine learning", "network", "programming", "algorithm", "database", "cybersecurity"],
        "bg_dark": RGBColor(0x12, 0x1b, 0x3a),
        "bg_slide": RGBColor(0xf7, 0xf9, 0xfc),
        "accent": RGBColor(0x2e, 0x9c, 0xdb),
        "accent_soft": RGBColor(0xc9, 0xe7, 0xf7),
        "title_text": RGBColor(0xff, 0xff, 0xff),
        "body_text": RGBColor(0x1b, 0x25, 0x3a),
        "muted": RGBColor(0x6a, 0x77, 0x8c),
    },
    "health": {
        "name": "health",
        "keywords": ["health", "medical", "medicine", "anatomy", "biology", "nutrition", "wellness", "clinic"],
        "bg_dark": RGBColor(0x11, 0x3d, 0x35),
        "bg_slide": RGBColor(0xf4, 0xfb, 0xf8),
        "accent": RGBColor(0x2f, 0xb8, 0x74),
        "accent_soft": RGBColor(0xc7, 0xec, 0xd8),
        "title_text": RGBColor(0xff, 0xff, 0xff),
        "body_text": RGBColor(0x17, 0x32, 0x2d),
        "muted": RGBColor(0x65, 0x7b, 0x74),
    },
    "arts": {
        "name": "arts",
        "keywords": ["art", "literature", "music", "design", "creative", "culture", "language", "drama"],
        "bg_dark": RGBColor(0x4a, 0x1f, 0x3d),
        "bg_slide": RGBColor(0xff, 0xf7, 0xfb),
        "accent": RGBColor(0xec, 0x6f, 0x91),
        "accent_soft": RGBColor(0xf7, 0xc6, 0xd2),
        "title_text": RGBColor(0xff, 0xff, 0xff),
        "body_text": RGBColor(0x3d, 0x22, 0x34),
        "muted": RGBColor(0x87, 0x5e, 0x72),
    },
    "general_academic": {
        "name": "general_academic",
        "keywords": [],
        "bg_dark": RGBColor(0x1f, 0x2e, 0x3a),
        "bg_slide": RGBColor(0xf5, 0xf7, 0xfa),
        "accent": RGBColor(0x3b, 0x82, 0xb1),
        "accent_soft": RGBColor(0xc9, 0xdd, 0xea),
        "title_text": RGBColor(0xff, 0xff, 0xff),
        "body_text": RGBColor(0x22, 0x34, 0x40),
        "muted": RGBColor(0x68, 0x7a, 0x86),
    },
}

THEME_DOMAINS = tuple(THEME_PALETTES.keys())


def _tokenize_text(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", (text or "").lower()))


def _score_domain_keywords(full_text: str, words: set[str], keywords: list[str]) -> int:
    score = 0
    for keyword in keywords:
        key = keyword.lower().strip()
        if not key:
            continue
        if " " in key:
            if key in full_text:
                score += 3
        elif key in words:
            score += 1
    return score


def _select_domain_from_keywords(subject: str, content: str) -> tuple[str, int]:
    text = f"{subject} {content}".lower()
    words = _tokenize_text(text)

    best_domain = "general_academic"
    best_score = 0
    for domain, palette in THEME_PALETTES.items():
        if domain == "general_academic":
            continue
        score = _score_domain_keywords(text, words, palette["keywords"])
        if score > best_score:
            best_score = score
            best_domain = domain

    return (best_domain, best_score)


def _build_theme_classifier_cache_key(subject: str, content: str) -> str:
    preview = content[:THEME_CLASSIFIER_CONTENT_PREVIEW_CHARS]
    return hashlib.sha1(f"{subject}|{preview}".encode("utf-8", errors="ignore")).hexdigest()


def _extract_json_payload(text: str) -> Optional[dict[str, object]]:
    cleaned = text.strip()
    try:
        parsed = json.loads(cleaned)
        return cast(dict[str, object], parsed) if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return cast(dict[str, object], parsed) if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            return float(value)
    except ValueError:
        return default
    return default


def _normalize_domain_name(label: str) -> str:
    normalized = re.sub(r"[^a-z_]+", "_", (label or "").strip().lower()).strip("_")
    if normalized in THEME_PALETTES:
        return normalized

    aliases = {
        "legal": "legal_governance",
        "law": "legal_governance",
        "government": "legal_governance",
        "tech": "technology",
        "general": "general_academic",
    }
    return aliases.get(normalized, "general_academic")


async def _classify_domain_with_gemini(
    subject: str,
    content: str,
    request_id: str | None = None,
) -> tuple[Optional[str], float]:
    if llm is None or not LLM_AVAILABLE:
        return (None, 0.0)

    preview = content[:THEME_CLASSIFIER_CONTENT_PREVIEW_CHARS]
    domains = ", ".join(THEME_DOMAINS)
    prompt = (
        "You classify academic topics into presentation theme domains. "
        "Return only JSON with keys domain and confidence. "
        "Domain must be one of: " + domains + ". "
        "Confidence must be a number between 0 and 1. "
        "If uncertain, use general_academic with low confidence.\n\n"
        f"Subject: {subject}\n"
        f"Content Preview: {preview}"
    )

    started = time.perf_counter()
    try:
        response = await llm.ainvoke(prompt)
        payload = _extract_json_payload(str(getattr(response, "content", "")))
        if not payload:
            _log_event(
                logging.INFO,
                "slide.theme_classifier.gemini",
                request_id=request_id,
                model_name=LLM_MODEL,
                prompt_len=len(prompt),
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                status="ok",
                domain_result=None,
                confidence=0.0,
                parse_json_success=False,
                **_extract_langchain_token_usage(response),
            )
            return (None, 0.0)

        domain = _normalize_domain_name(str(payload.get("domain", "")))
        confidence = _to_float(payload.get("confidence", 0.0), 0.0)
        confidence = max(0.0, min(1.0, confidence))
        _log_event(
            logging.INFO,
            "slide.theme_classifier.gemini",
            request_id=request_id,
            model_name=LLM_MODEL,
            prompt_len=len(prompt),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            status="ok",
            domain_result=domain,
            confidence=confidence,
            parse_json_success=True,
            **_extract_langchain_token_usage(response),
        )
        return (domain, confidence)
    except Exception as e:
        _log_event(
            logging.WARNING,
            "slide.theme_classifier.gemini",
            request_id=request_id,
            model_name=LLM_MODEL,
            prompt_len=len(prompt),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            status="error",
            error_type=type(e).__name__,
        )
        logger.warning("Gemini theme classifier failed, falling back to keyword classifier: %s", e)
        return (None, 0.0)


async def resolve_theme_selection(
    subject: str,
    content: str,
    request_id: str | None = None,
) -> ThemeSelection:
    cache_key = _build_theme_classifier_cache_key(subject, content)
    cached = _THEME_CLASSIFIER_CACHE.get(cache_key)
    now = datetime.now().timestamp()
    if cached is not None:
        typed_cached = cast(ThemeCacheRecord, cached)
        if typed_cached["expires_at"] > now:
            return {
                "domain": typed_cached["domain"],
                "confidence": typed_cached["confidence"],
                "source": cast(Literal["gemini_classifier", "keyword_fallback", "default"], typed_cached["source"]),
                "theme": THEME_PALETTES[typed_cached["domain"]],
            }

    source: Literal["gemini_classifier", "keyword_fallback", "default"]
    selected_domain: str
    selected_confidence: float

    domain, confidence = await _classify_domain_with_gemini(subject, content, request_id=request_id)
    if domain and confidence >= THEME_CLASSIFIER_CONFIDENCE_THRESHOLD:
        source = "gemini_classifier"
        selected_domain = domain
        selected_confidence = confidence
    else:
        selected_domain, keyword_score = _select_domain_from_keywords(subject, content)
        if selected_domain == "general_academic":
            source = "default"
            selected_confidence = 0.0
        else:
            source = "keyword_fallback"
            selected_confidence = max(confidence, min(0.59, keyword_score / 10.0))

    _THEME_CLASSIFIER_CACHE[cache_key] = {
        "domain": selected_domain,
        "confidence": selected_confidence,
        "source": source,
        "expires_at": now + THEME_CLASSIFIER_CACHE_TTL_SEC,
    }

    return {
        "domain": selected_domain,
        "confidence": selected_confidence,
        "source": source,
        "theme": THEME_PALETTES[selected_domain],
    }


def _parse_md_bold_segments(text: str) -> list[tuple[str, bool]]:
    segments: list[tuple[str, bool]] = []
    pattern = re.compile(r"\*\*(.+?)\*\*")
    last_index = 0
    for match in pattern.finditer(text):
        if match.start() > last_index:
            segments.append((text[last_index:match.start()], False))
        segments.append((match.group(1), True))
        last_index = match.end()
    if last_index < len(text):
        segments.append((text[last_index:], False))
    return [(segment, bold) for segment, bold in segments if segment]


def _add_text_run(paragraph, text: str, *, bold: bool = False, size: int = 18,
                  color: Optional[RGBColor] = None, italic: bool = False) -> None:
    run = paragraph.add_run()
    run.text = text
    run.font.bold = bold
    run.font.italic = italic
    run.font.size = Pt(size)
    if color is not None:
        run.font.color.rgb = color


def _add_rich_text(paragraph, text: str, *, size: int, color: RGBColor,
                   bold: bool = False) -> None:
    if "**" not in text:
        _add_text_run(paragraph, text, bold=bold, size=size, color=color)
        return

    for segment, is_bold in _parse_md_bold_segments(text):
        _add_text_run(paragraph, segment, bold=bold or is_bold, size=size, color=color)


def _add_section_block(text_frame, section: dict[str, object], theme: ThemePalette) -> None:
    heading = str(section.get("heading", "")).strip()
    body = str(section.get("body", "")).strip()
    bullets = section.get("bullets", [])

    if heading:
        paragraph = text_frame.add_paragraph()
        paragraph.space_before = Pt(8)
        paragraph.space_after = Pt(2)
        _add_text_run(paragraph, heading, bold=True, size=18, color=theme["accent"])

    if body:
        paragraph = text_frame.add_paragraph()
        paragraph.space_after = Pt(3)
        _add_rich_text(paragraph, body, size=14, color=theme["body_text"])

    if isinstance(bullets, list):
        for bullet in bullets:
            bullet_text = str(bullet).strip()
            if not bullet_text:
                continue
            paragraph = text_frame.add_paragraph()
            paragraph.level = 1
            paragraph.space_after = Pt(1)
            _add_text_run(paragraph, f"- {bullet_text}", size=13, color=theme["body_text"])


def _build_slide_content_text(slide, slide_data: dict[str, object], theme: ThemePalette) -> None:
    intro = str(slide_data.get("intro", "")).strip()
    sections = slide_data.get("sections", [])

    content_box = slide.shapes.add_textbox(
        Inches(0.55), Inches(1.5), Inches(12.0), Inches(5.35)
    )
    text_frame = content_box.text_frame
    text_frame.word_wrap = True

    if intro:
        paragraph = text_frame.paragraphs[0]
        paragraph.space_after = Pt(6)
        _add_rich_text(paragraph, intro, size=15, color=theme["body_text"])

    if isinstance(sections, list):
        for section in sections:
            if isinstance(section, dict):
                _add_section_block(text_frame, section, theme)


def _add_theme_badge(slide, theme: ThemePalette, title: str) -> None:
    badge = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE,
        Inches(10.0), Inches(0.24), Inches(2.4), Inches(0.38)
    )
    badge.fill.solid()
    badge.fill.fore_color.rgb = theme["accent"]
    badge.line.fill.background()

    badge_text = badge.text_frame
    badge_text.word_wrap = False
    paragraph = badge_text.paragraphs[0]
    paragraph.alignment = PP_ALIGN.CENTER
    _add_text_run(paragraph, title, bold=True, size=10, color=theme["title_text"])


def get_upload_size_bytes(upload_file: UploadFile) -> int:
    """Measure upload size without consuming the stream."""
    try:
        current_position = upload_file.file.tell()
        upload_file.file.seek(0, os.SEEK_END)
        file_size = upload_file.file.tell()
        upload_file.file.seek(current_position)
        return file_size
    except Exception as e:
        logger.warning("Could not determine upload size for %s: %s", upload_file.filename, e)
        return -1


def create_temp_path(suffix: str) -> str:
    """Create a collision-resistant temp file path."""
    safe_suffix = suffix if suffix.startswith(".") else f".{suffix}" if suffix else ".tmp"
    return os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}{safe_suffix}")


def get_slide_count(content: str) -> int:
    """Estimate ideal number of slides based on content length."""
    words = len(content.split())
    if words < 300:
        return 5
    elif words < 800:
        return 8
    elif words < 1500:
        return 12
    else:
        return 15


# ─────────────────────────────────────────────
# SLIDE STRUCTURE GENERATOR
# ─────────────────────────────────────────────

async def generate_slide_structure(
    content: str,
    subject: str = "",
    num_slides: Optional[int] = None,
    request_id: str | None = None,
) -> dict:
    """Use LLM to generate a structured slide outline from content."""

    if not num_slides:
        num_slides = get_slide_count(content)

    if not LLM_AVAILABLE:
        # Mock structure for demo
        return {
            "title": subject or "Study Presentation",
            "subtitle": "Generated by Akadverse Slide Creator",
            "slides": [
                {
                    "slide_number": 1,
                    "title": "Overview",
                    "intro": "This deck summarizes the topic in a clearer teaching format.",
                    "sections": [
                        {
                            "heading": "Purpose",
                            "body": "Use Gemini to structure the source content into a more descriptive presentation.",
                            "bullets": ["Topic-aware theme selection", "Structured slide sections", "Readable emphasis rendering"],
                        }
                    ],
                    "points": [
                        "Set GOOGLE_API_KEY to enable Gemini generation",
                        "Install langchain-google-genai if it is missing",
                        "Then restart this service",
                        "Real slides will be generated from your content"
                    ],
                    "speaker_note": "This is a demo slide. Real content will appear with LLM active."
                },
                {
                    "slide_number": 2,
                    "title": "What This Tool Does",
                    "points": [
                        "Accepts text, PDFs, PPTX, URLs, and images",
                        "Uses AI to structure content into slides",
                        "Generates a professional PowerPoint file",
                        "Downloads directly to your device"
                    ],
                    "speaker_note": "The slide generator shares input logic with the Notes Creator."
                },
                {
                    "slide_number": 3,
                    "title": "Key Takeaways",
                    "points": [
                        "Slide Creator AI is ready and running",
                        "Waiting for LLM to generate real content",
                        "Architecture is complete",
                        "Connect Gemini via LangChain to activate"
                    ],
                    "speaker_note": "Summary slide."
                }
            ]
        }

    # Truncate very long content to fit LLM context
    content_truncated = content[:4000] if len(content) > 4000 else content

    prompt = SLIDE_PROMPT.format(
        num_slides=num_slides,
        subject=subject,
        content=content_truncated
    )

    if llm is None:
        logger.warning("LLM client is unavailable at runtime, using fallback structure.")
        return {
            "title": subject or "Presentation",
            "subtitle": "",
            "slides": [
                {
                    "slide_number": 1,
                    "title": "Content Overview",
                    "points": content_truncated[:500].split("\n")[:5],
                    "speaker_note": ""
                }
            ]
        }

    started = time.perf_counter()
    try:
        response = await llm.ainvoke(prompt)
        raw = str(getattr(response, "content", "")).strip()

        # Strip markdown fences if present
        raw = re.sub(r"```json\s*", "", raw)
        raw = re.sub(r"```\s*", "", raw)

        try:
            structure = json.loads(raw)
            normalized = _normalize_structure(structure)
            _log_event(
                logging.INFO,
                "slide.structure.gemini",
                request_id=request_id,
                model_name=LLM_MODEL,
                prompt_len=len(prompt),
                content_truncated_len=len(content_truncated),
                parse_json_success=True,
                slides_generated_count=len(normalized.get("slides", [])),
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                status="ok",
                **_extract_langchain_token_usage(response),
            )
            return normalized
        except json.JSONDecodeError:
            # Try to extract JSON from response
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                try:
                    normalized = _normalize_structure(json.loads(match.group(0)))
                    _log_event(
                        logging.INFO,
                        "slide.structure.gemini",
                        request_id=request_id,
                        model_name=LLM_MODEL,
                        prompt_len=len(prompt),
                        content_truncated_len=len(content_truncated),
                        parse_json_success=True,
                        slides_generated_count=len(normalized.get("slides", [])),
                        latency_ms=round((time.perf_counter() - started) * 1000, 2),
                        status="ok",
                        **_extract_langchain_token_usage(response),
                    )
                    return normalized
                except Exception:
                    pass
    except Exception as e:
        _log_event(
            logging.WARNING,
            "slide.structure.gemini",
            request_id=request_id,
            model_name=LLM_MODEL,
            prompt_len=len(prompt),
            content_truncated_len=len(content_truncated),
            parse_json_success=False,
            slides_generated_count=0,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            status="error",
            error_type=type(e).__name__,
        )
        logger.warning("Gemini slide generation failed, using fallback structure: %s", e)

    return _normalize_structure({
        "title": subject or "Presentation",
        "subtitle": "",
        "slides": [
            {
                "slide_number": 1,
                "title": "Content Overview",
                "intro": content_truncated[:250],
                "sections": [
                    {
                        "heading": "Source Preview",
                        "body": content_truncated[:500],
                        "bullets": [],
                    }
                ],
                "speaker_note": ""
            }
        ]
    })


# ─────────────────────────────────────────────
# PPTX BUILDER
# ─────────────────────────────────────────────

# Colour scheme
COLORS = {
    "bg_dark":    RGBColor(0x1a, 0x1a, 0x2e),   # Dark navy
    "bg_slide":   RGBColor(0xf5, 0xf0, 0xe8),   # Cream
    "accent":     RGBColor(0x00, 0x7a, 0xc2),   # Blue
    "text_dark":  RGBColor(0x1a, 0x1a, 0x2e),   # Dark
    "text_light": RGBColor(0xff, 0xff, 0xff),   # White
    "bullet":     RGBColor(0x00, 0x7a, 0xc2),   # Blue
}


def build_pptx(structure: dict, output_path: str, theme: ThemePalette) -> str:
    """Build a PowerPoint file from a slide structure dict."""
    if not PPTX_AVAILABLE:
        # Write structure as JSON file for demo
        json_path = output_path.replace(".pptx", "_structure.json")
        with open(json_path, "w") as f:
            json.dump(structure, f, indent=2)
        return json_path

    prs = Presentation()
    prs.slide_width = Inches(13.33)
    prs.slide_height = Inches(7.5)

    # ── Title slide ──
    title_layout = prs.slide_layouts[6]  # blank
    title_slide = prs.slides.add_slide(title_layout)

    # Dark background
    background = title_slide.background
    fill = background.fill
    fill.solid()
    fill.fore_color.rgb = theme["bg_dark"]

    # Title text
    title_box = title_slide.shapes.add_textbox(
        Inches(1), Inches(2.5), Inches(11.33), Inches(1.5)
    )
    tf = title_box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    run = p.add_run()
    run.text = structure.get("title", "Presentation")
    run.font.size = Pt(40)
    run.font.bold = True
    run.font.color.rgb = theme["title_text"]

    # Subtitle
    if structure.get("subtitle"):
        sub_box = title_slide.shapes.add_textbox(
            Inches(1), Inches(4.2), Inches(11.33), Inches(0.8)
        )
        tf2 = sub_box.text_frame
        p2 = tf2.paragraphs[0]
        p2.alignment = PP_ALIGN.CENTER
        run2 = p2.add_run()
        run2.text = structure["subtitle"]
        run2.font.size = Pt(20)
        run2.font.color.rgb = theme["accent"]

    # Akadverse branding
    brand_box = title_slide.shapes.add_textbox(
        Inches(1), Inches(6.5), Inches(11.33), Inches(0.5)
    )
    tf3 = brand_box.text_frame
    p3 = tf3.paragraphs[0]
    p3.alignment = PP_ALIGN.CENTER
    run3 = p3.add_run()
    run3.text = "Generated by Akadverse AI • " + datetime.now().strftime("%B %Y")
    run3.font.size = Pt(12)
    run3.font.color.rgb = theme["accent_soft"]

    _add_theme_badge(title_slide, theme, str(theme["name"]).title())

    # ── Content slides ──
    content_layout = prs.slide_layouts[6]  # blank

    for slide_data in structure.get("slides", []):
        slide = prs.slides.add_slide(content_layout)

        # Light cream background
        bg = slide.background
        bg_fill = bg.fill
        bg_fill.solid()
        bg_fill.fore_color.rgb = theme["bg_slide"]

        # Blue accent bar on left
        left_bar = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            Inches(0), Inches(0),
            Inches(0.15), Inches(7.5)
        )
        left_bar.fill.solid()
        left_bar.fill.fore_color.rgb = theme["accent"]
        left_bar.line.fill.background()

        # Slide title
        title_box = slide.shapes.add_textbox(
            Inches(0.4), Inches(0.3), Inches(12.5), Inches(1.0)
        )
        tf = title_box.text_frame
        p = tf.paragraphs[0]
        run = p.add_run()
        run.text = slide_data.get("title", f"Slide {slide_data.get('slide_number', '')}")
        run.font.size = Pt(28)
        run.font.bold = True
        run.font.color.rgb = theme["body_text"]

        # Divider line
        line = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            Inches(0.4), Inches(1.3),
            Inches(12.5), Inches(0.03)
        )
        line.fill.solid()
        line.fill.fore_color.rgb = theme["accent"]
        line.line.fill.background()

        _build_slide_content_text(slide, slide_data, theme)

        # Slide number
        num_box = slide.shapes.add_textbox(
            Inches(12.0), Inches(7.0), Inches(1.0), Inches(0.4)
        )
        tf_n = num_box.text_frame
        p_n = tf_n.paragraphs[0]
        p_n.alignment = PP_ALIGN.RIGHT
        run_n = p_n.add_run()
        run_n.text = str(slide_data.get("slide_number", ""))
        run_n.font.size = Pt(11)
        run_n.font.color.rgb = theme["muted"]

        # Speaker notes
        if slide_data.get("speaker_note"):
            notes_slide = slide.notes_slide
            notes_tf = notes_slide.notes_text_frame
            if notes_tf is not None:
                notes_tf.text = slide_data["speaker_note"]

    prs.save(output_path)
    return output_path


# ─────────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────────

app = FastAPI(
    title="Akadverse Slide Generator AI",
    description="Convert any content into a professional PowerPoint presentation",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Temp output directory for generated files
OUTPUT_DIR = "generated_slides"
os.makedirs(OUTPUT_DIR, exist_ok=True)


async def process_and_build(
    content: str,
    subject: str,
    num_slides: Optional[int],
    filename_base: str,
    base_url: str,
    request_id: str | None = None,
) -> dict:
    """Core pipeline: content → LLM structure → PPTX file."""
    if not content.strip():
        raise HTTPException(status_code=400, detail="No usable content was provided")

    try:
        # Keep normalization centralized so every endpoint gets consistent structure quality.
        structure = _normalize_structure(
            await generate_slide_structure(content, subject, num_slides, request_id=request_id)
        )
        theme_selection = await resolve_theme_selection(
            subject or str(structure.get("title", "")),
            content,
            request_id=request_id,
        )
        theme = theme_selection["theme"]

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        safe_name = re.sub(r'[^a-zA-Z0-9_]', '_', subject or filename_base or "presentation")
        output_path = os.path.join(OUTPUT_DIR, f"{safe_name}_{timestamp}.pptx")

        built_path = await asyncio.to_thread(build_pptx, structure, output_path, theme)
        file_ext = "pptx" if PPTX_AVAILABLE else "json"

        return {
            "status": "success",
            "title": structure.get("title"),
            "total_slides": len(structure.get("slides", [])) + 1,  # +1 for title slide
            "filename": os.path.basename(built_path),
            "download_url": _absolute_download_url(base_url, os.path.basename(built_path)),
            "structure_preview": structure.get("slides", [])[:3],
            "llm_model": LLM_MODEL,
            "theme_name": theme["name"],
            "theme_domain": theme_selection["domain"],
            "theme_source": theme_selection["source"],
            "theme_confidence": round(float(theme_selection["confidence"]), 3),
            "output_format": file_ext,
            "timestamp": datetime.now().isoformat()
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Slide build pipeline failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail="Slide generation failed due to an internal processing error. Please try again."
        ) from e


@app.post("/slides/from-text")
async def slides_from_text(
    request: Request,
    content: str = Form(...),
    subject: str = Form(""),
    num_slides: int = Form(0),
    user_id: str = Form("")
):
    """Generate slides from plain text."""
    request_id = uuid.uuid4().hex
    _log_event(
        logging.INFO,
        "slide.request.start",
        request_id=request_id,
        subject_len=len(subject),
        content_len=len(content),
        num_slides_requested=num_slides,
        user_id_present=bool(user_id),
    )
    if not content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty")
    if num_slides < 0:
        raise HTTPException(status_code=400, detail="num_slides cannot be negative")
    return await process_and_build(
        content,
        subject,
        num_slides or None,
        "presentation",
        str(request.base_url),
        request_id=request_id,
    )


@app.post("/slides/from-url")
async def slides_from_url(
    request: Request,
    url: str = Form(...),
    subject: str = Form(""),
    num_slides: int = Form(0),
    user_id: str = Form("")
):
    """Generate slides from a URL."""
    if num_slides < 0:
        raise HTTPException(status_code=400, detail="num_slides cannot be negative")

    try:
        content = await asyncio.to_thread(load_content, "url", None, None, url)
    except Exception as e:
        logger.warning("URL extraction failed for %s: %s", url, e)
        raise HTTPException(status_code=400, detail="Could not load or parse the provided URL") from e

    if not content.strip():
        raise HTTPException(status_code=400, detail="Could not extract content from URL")
    return await process_and_build(content, subject, num_slides or None, "web_content", str(request.base_url))


@app.post("/slides/from-document")
async def slides_from_document(
    request: Request,
    file: UploadFile = File(...),
    subject: str = Form(""),
    num_slides: int = Form(0),
    user_id: str = Form("")
):
    """Generate slides from PDF or PPTX document."""
    if num_slides < 0:
        raise HTTPException(status_code=400, detail="num_slides cannot be negative")

    file_size = get_upload_size_bytes(file)
    if file_size < 0:
        raise HTTPException(status_code=400, detail="Could not validate uploaded file size")
    if file_size > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File too large. Max {MAX_UPLOAD_SIZE_MB}MB allowed.")

    filename = (file.filename or "").lower()
    suffix = ".pdf" if filename.endswith(".pdf") else ".pptx" if filename.endswith(".pptx") else ".txt"

    tmp_path = create_temp_path(suffix)
    try:
        with open(tmp_path, "wb") as tmp:
            shutil.copyfileobj(file.file, tmp)
    except Exception as e:
        logger.exception("Failed to persist uploaded document %s: %s", file.filename, e)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=500, detail="Failed to process uploaded document") from e

    try:
        source_type = "pdf" if suffix == ".pdf" else "pptx" if suffix == ".pptx" else "text"
        content = await asyncio.to_thread(load_content, source_type, None, tmp_path)
    except Exception as e:
        logger.warning("Document extraction failed for %s: %s", file.filename, e)
        raise HTTPException(status_code=400, detail="Could not extract text from document") from e
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    if not content.strip():
        raise HTTPException(status_code=400, detail="Could not extract text from document")

    original_filename = file.filename or "uploaded_document"
    filename_stem = os.path.splitext(original_filename)[0]
    resolved_subject = subject or original_filename

    return await process_and_build(content, resolved_subject,
                                   num_slides or None, filename_stem, str(request.base_url))


@app.post("/slides/from-image")
async def slides_from_image(
    request: Request,
    image: UploadFile = File(...),
    subject: str = Form(""),
    num_slides: int = Form(0),
    user_id: str = Form("")
):
    """Generate slides from a photo of text/notes."""
    if num_slides < 0:
        raise HTTPException(status_code=400, detail="num_slides cannot be negative")

    file_size = get_upload_size_bytes(image)
    if file_size < 0:
        raise HTTPException(status_code=400, detail="Could not validate uploaded file size")
    if file_size > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File too large. Max {MAX_UPLOAD_SIZE_MB}MB allowed.")

    tmp_path = create_temp_path(".jpg")
    try:
        with open(tmp_path, "wb") as tmp:
            shutil.copyfileobj(image.file, tmp)
    except Exception as e:
        logger.exception("Failed to persist uploaded image %s: %s", image.filename, e)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=500, detail="Failed to process uploaded image") from e

    extraction_source = "unknown"
    extraction_quality: dict[str, float | int] = {"characters": 0, "words": 0, "alpha_ratio": 0.0, "mean_confidence": 0.0}
    try:
        # OCR can fail for low quality images; this path is intentionally downgraded to a client-facing 400.
        extracted_content = await asyncio.to_thread(extract_text_from_image, tmp_path)
        content, extraction_source, extraction_quality = extracted_content
    except Exception as e:
        logger.warning("Image extraction failed for %s: %s", image.filename, e)
        raise HTTPException(status_code=400, detail="Could not extract text from image") from e
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    if not content.strip():
        raise HTTPException(status_code=400, detail="Could not extract text from image")

    result = await process_and_build(content, subject, num_slides or None, "image_notes", str(request.base_url))
    result["extraction_source"] = extraction_source
    result["extraction_quality"] = extraction_quality
    return result


@app.get("/slides/download/{filename}")
async def download_slide(filename: str):
    """Download a generated PowerPoint file."""
    # Allow only direct filenames generated by this service.
    safe_filename = os.path.basename(filename)
    if safe_filename != filename or not safe_filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = os.path.join(OUTPUT_DIR, safe_filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found or expired")

    media_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    if safe_filename.endswith(".json"):
        media_type = "application/json"

    return FileResponse(
        path=file_path,
        filename=safe_filename,
        media_type=media_type
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "llm": LLM_MODEL,
        "pptx_builder": PPTX_AVAILABLE,
        "loaders": LOADERS_AVAILABLE,
        "ocr": OCR_AVAILABLE,
        "max_upload_size_mb": MAX_UPLOAD_SIZE_MB,
    }


@app.get("/")
def root():
    return {
        "service": "Akadverse Slide Generator AI",
        "version": "1.0.0",
        "endpoints": {
            "from_text":     "POST /slides/from-text",
            "from_url":      "POST /slides/from-url",
            "from_document": "POST /slides/from-document (PDF, PPTX)",
            "from_image":    "POST /slides/from-image",
            "download":      "GET /slides/download/{filename}"
        },
        "llm": LLM_MODEL,
        "max_upload_size_mb": MAX_UPLOAD_SIZE_MB,
        "status": "running"
    }


if __name__ == "__main__":
    uvicorn.run("slide_generator:app", host="127.0.0.1", port=8009, reload=True)
    
    
# run using uvicorn slide_generator:app --host 127.0.0.1 --port 8009 --reload