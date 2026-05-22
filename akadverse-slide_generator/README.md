# AkadVerse: Slide Generator AI
### Tier 5 Learning AI Tool | Microservice Port: `8009`

> A student and faculty facing presentation generation tool. It converts text, URLs, documents, and images into downloadable `.pptx` slide decks. It uses Gemini for structured slide authoring, hybrid topic classification for theme selection, and OCR with Gemini Vision fallback for weak scans. This gives users fast, testable, and presentation ready outputs from multiple academic input formats.

## Table of Contents

1. [What This Microservice Does](#what-this-microservice-does)
2. [The Hybrid Theme Classifier](#the-hybrid-theme-classifier)
3. [Architecture Overview](#architecture-overview)
4. [Prerequisites](#prerequisites)
5. [Getting Your API Key](#getting-your-api-key)
6. [Critical Setup: Tesseract OCR on Windows](#critical-setup-tesseract-ocr-on-windows)
7. [Installation](#installation)
8. [Configuring Environment Variables](#configuring-environment-variables)
9. [Running the Server](#running-the-server)
10. [API Endpoints](#api-endpoints)
   - [1. POST /slides/from-text](#1-post-slidesfrom-text)
   - [2. POST /slides/from-url](#2-post-slidesfrom-url)
   - [3. POST /slides/from-document](#3-post-slidesfrom-document)
   - [4. POST /slides/from-image](#4-post-slidesfrom-image)
   - [5. GET /slides/download/{filename}](#5-get-slidesdownloadfilename)
   - [6. GET /health](#6-get-health)
   - [7. GET /](#7-get-)
11. [Testing with Swagger UI](#testing-with-swagger-ui)
12. [Example Test Inputs](#example-test-inputs)
13. [Understanding the Responses](#understanding-the-responses)
14. [Theme Domains and Selection Rules](#theme-domains-and-selection-rules)
15. [Generated Files](#generated-files)
16. [Common Errors and Fixes](#common-errors-and-fixes)
17. [Project Structure](#project-structure)
18. [Part of the AkadVerse Platform](#part-of-the-akadverse-platform)

## What This Microservice Does

This service is a **Tier 5 component** of the AkadVerse AI-first e-learning platform, living inside the *My Learning* module as a content-to-presentation converter.

When a user submits content, the service extracts source text from one of four input paths: raw text, URL, document, or image. Gemini then generates a structured slide plan with title, intro, section headings, explanatory bodies, and support bullets. The renderer builds a `.pptx` deck with topic aware color themes, layout styling, and slide notes, then returns a downloadable file URL.

Core inputs:
- Text content (`/slides/from-text`)
- URL content (`/slides/from-url`)
- Uploaded PDF or PPTX (`/slides/from-document`)
- Uploaded image with OCR and vision fallback (`/slides/from-image`)

Core outputs:
- Generated slide file name
- Download endpoint URL
- Theme metadata (`theme_name`, `theme_domain`, `theme_source`, `theme_confidence`)
- Extraction diagnostics for image inputs

## The Hybrid Theme Classifier

The service uses a two-stage topic classification strategy so slide themes are accurate and stable:

1. Gemini classifier stage:
- Gemini classifies the topic into a constrained domain list.
- The model must return JSON with `domain` and `confidence`.
- If confidence is above the configured threshold, that domain is used.

2. Keyword fallback stage:
- If Gemini fails or confidence is below threshold, deterministic keyword scoring runs.
- Scoring uses whole-word and phrase matches.
- If no meaningful score is found, the service falls back to `general_academic`.

3. Cache layer:
- Classification is cached for a short TTL to reduce repeated model calls.

This design keeps theme selection reliable even under API or quota degradation.

## Architecture Overview

```
User Input
  │
  ├── Text input -------------------------------┐
  ├── URL input ----> Web loader --------------┤
  ├── Document -----> PDF/PPTX loader ---------┤
  └── Image --------> OCR quality scoring -----┤
                     ├── strong OCR: Tesseract │
                     └── weak OCR: Gemini Vision fallback
                                             │
                                             ▼
                               Unified source text payload
                                             │
                                             ▼
                        Gemini slide structure generation
                         (title, intro, sections, notes)
                                             │
                                             ▼
                    Hybrid theme classification pipeline
                    ├── Gemini domain classifier + confidence
                    ├── keyword fallback if needed
                    └── cache for repeated requests
                                             │
                                             ▼
                             PPTX renderer (python-pptx)
                                             │
                                             ▼
                     generated_slides/{filename}.pptx
                                             │
                                             ▼
                         /slides/download/{filename}
```

**Key design decisions:**
- **Asynchronous I/O path:** Endpoint handlers use async execution and offload blocking operations with worker threads. This prevents request stalls during extraction and render operations.
- **Graceful degradation by default:** If Gemini, OCR, or loaders are unavailable, the service returns fallback responses instead of crashing.
- **Filename-based file serving:** The download endpoint serves a specific generated artifact by filename. This keeps file retrieval explicit and stateless.
- **Hybrid theme selection:** Gemini semantic classification improves topic relevance, while deterministic fallback prevents failures under model uncertainty.
- **Weak-scan OCR routing:** Image extraction uses quality scoring to decide when Gemini Vision is required, which controls cost and improves reliability.

## Prerequisites

- **Python 3.10 or higher**
- **pip** or **uv**
- A **Google Gemini API key**
- **Tesseract OCR binary** for local OCR on image uploads

> The service can start without Gemini and still run in mock mode, but real slide generation and topic classification need a valid Gemini key.

## Getting Your API Key

1. Go to [https://aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. Sign in with a Google account.
3. Click **Create API Key**.
4. Copy the key and place it in your `.env` file.

## Critical Setup: Tesseract OCR on Windows

**This setup is required if you want OCR quality scoring and the OCR-first image path to work.**

1. Install Tesseract OCR for Windows.
2. Add the install folder to your PATH. Typical path:
   - `C:\Program Files\Tesseract-OCR`
3. Restart VS Code and terminal sessions.
4. Verify installation:

```
tesseract --version
```

If this is skipped, image extraction may rely only on Gemini Vision fallback where available.

## Installation

### Step 1 - Set up your project folder

```
slide_generator.py
requirements.txt
.gitignore
.env
```

### Step 2 - Create and activate a virtual environment

```
python -m venv .venv
```

Windows:

```
.venv\Scripts\activate
```

macOS/Linux:

```
source .venv/bin/activate
```

### Step 3 - Install dependencies

```
pip install -r requirements.txt
```

|Package|Purpose|
|---|---|
|fastapi|HTTP API framework|
|uvicorn[standard]|ASGI server for local runtime|
|python-multipart|Required for form and file uploads in FastAPI|
|python-dotenv|Loads `.env` variables at startup|
|langchain-google-genai|Gemini chat wrapper used for structure generation and classification|
|google-genai|Gemini SDK used for model discovery and vision fallback|
|python-pptx|Builds `.pptx` files programmatically|
|langchain-community|Document loaders for URL, PDF, and PPTX extraction|
|pypdf|Backend parser for PDF loader path|
|unstructured|PPTX extraction support for unstructured loader|
|beautifulsoup4|HTML parsing support for URL content extraction|
|lxml|Fast parser backend for web/document extraction paths|
|pillow|Image processing for OCR preprocessing|
|pytesseract|Python wrapper for Tesseract OCR|

## Configuring Environment Variables

1. Create `.env` in your service root.
2. Add these values:

```
GOOGLE_API_KEY=YOUR_KEY_HERE
MAX_UPLOAD_SIZE_MB=10
THEME_CLASSIFIER_CONFIDENCE_THRESHOLD=0.60
THEME_CLASSIFIER_CACHE_TTL_SEC=300
THEME_CLASSIFIER_CONTENT_PREVIEW_CHARS=1800
```

3. Verify the key is loaded:

```
python -c "import os; from dotenv import load_dotenv; load_dotenv(); print(bool(os.getenv('GOOGLE_API_KEY')))"
```

If this prints `False`, your key is not being loaded.

## Running the Server

From inside your project folder with the virtual environment activated:

```
uvicorn slide_generator:app --host 127.0.0.1 --port 8009 --reload
```

**Expected startup output:**

```
INFO:     Uvicorn running on http://127.0.0.1:8009 (Press CTRL+C to quit)
INFO:     Started reloader process [PID] using WatchFiles
INFO:     Started server process [PID]
INFO:     Application startup complete.
```

If Gemini is unavailable you may also see warning logs, and the service will continue in mock mode.

## API Endpoints

### 1. `POST /slides/from-text`

**What it does:**
Generates slides from plain text content and returns a downloadable file reference.

|Field|Required|Default|Description|
|---|---|---|---|
|content|Yes|--|Source text to convert into slides|
|subject|No|`""`|Topic title used for prompt guidance and theme selection|
|num_slides|No|`0`|Requested slide count. `0` triggers auto-estimation|
|user_id|No|`""`|Optional caller reference metadata|

**Success response (200 OK):**

```json
{
  "status": "success",
  "title": "Nigeria Legal System and Conflict Resolution",
  "total_slides": 7,
  "filename": "Nigeria_Legal_System_20260414140622.pptx",
  "download_url": "/slides/download/Nigeria_Legal_System_20260414140622.pptx",
  "structure_preview": [
    {
      "slide_number": 1,
      "title": "Overview",
      "intro": "This deck introduces legal structure and dispute pathways in Nigeria.",
      "sections": [
        {
          "heading": "Core Scope",
          "body": "The legal system combines statutory law, case law, and constitutional governance.",
          "bullets": ["Sources of law", "Institutions", "Dispute pathways"]
        }
      ],
      "speaker_note": "Open with legal context before institutions."
    }
  ],
  "llm_model": "gemini-2.5-flash",
  "theme_name": "legal_governance",
  "theme_domain": "legal_governance",
  "theme_source": "gemini_classifier",
  "theme_confidence": 0.87,
  "output_format": "pptx",
  "timestamp": "2026-04-14T14:06:22.194201"
}
```

**Expected terminal output:**

```
INFO:     127.0.0.1:xxxxx - "POST /slides/from-text HTTP/1.1" 200 OK
```

### 2. `POST /slides/from-url`

**What it does:**
Extracts page content from a URL, generates structured slides, and returns a downloadable artifact.

|Field|Required|Default|Description|
|---|---|---|---|
|url|Yes|--|Public URL to extract content from|
|subject|No|`""`|Optional presentation subject override|
|num_slides|No|`0`|Requested slide count or auto-estimation|
|user_id|No|`""`|Optional caller reference metadata|

**Success response (200 OK):**

```json
{
  "status": "success",
  "title": "Machine Learning Fundamentals",
  "total_slides": 8,
  "filename": "Machine_Learning_Fundamentals_20260414140710.pptx",
  "download_url": "/slides/download/Machine_Learning_Fundamentals_20260414140710.pptx",
  "llm_model": "gemini-2.5-flash",
  "theme_name": "technology",
  "theme_domain": "technology",
  "theme_source": "keyword_fallback",
  "theme_confidence": 0.55,
  "output_format": "pptx",
  "timestamp": "2026-04-14T14:07:10.038112"
}
```

**Expected terminal output:**

```
INFO:     127.0.0.1:xxxxx - "POST /slides/from-url HTTP/1.1" 200 OK
```

### 3. `POST /slides/from-document`

**What it does:**
Accepts a PDF or PPTX upload, extracts text, generates slides, and returns file metadata.

|Field|Required|Default|Description|
|---|---|---|---|
|file|Yes|--|Uploaded `.pdf`, `.pptx`, or `.txt` source file|
|subject|No|`""`|Optional topic override|
|num_slides|No|`0`|Requested slide count or auto-estimation|
|user_id|No|`""`|Optional caller reference metadata|

**Special behaviour note:**
Uploads larger than the configured max size are rejected with `413`.

**Success response (200 OK):**

```json
{
  "status": "success",
  "title": "Database Systems Revision",
  "total_slides": 9,
  "filename": "Database_Systems_Revision_20260414140801.pptx",
  "download_url": "/slides/download/Database_Systems_Revision_20260414140801.pptx",
  "theme_name": "technology",
  "theme_domain": "technology",
  "theme_source": "gemini_classifier",
  "theme_confidence": 0.78,
  "output_format": "pptx",
  "timestamp": "2026-04-14T14:08:01.814091"
}
```

**Expected terminal output:**

```
INFO:     127.0.0.1:xxxxx - "POST /slides/from-document HTTP/1.1" 200 OK
```

### 4. `POST /slides/from-image`

**What it does:**
Extracts text from uploaded images using OCR quality scoring with Gemini Vision fallback for weak scans, then generates slides.

|Field|Required|Default|Description|
|---|---|---|---|
|image|Yes|--|Uploaded image file containing text|
|subject|No|`""`|Optional topic override|
|num_slides|No|`0`|Requested slide count or auto-estimation|
|user_id|No|`""`|Optional caller reference metadata|

**Special behaviour note:**
The response exposes extraction diagnostics so you can verify whether `tesseract`, `gemini_vision`, or `tesseract_fallback` was used.

**Success response (200 OK):**

```json
{
  "status": "success",
  "title": "Linear Algebra Quick Review",
  "total_slides": 6,
  "filename": "Linear_Algebra_Quick_Review_20260414140920.pptx",
  "download_url": "/slides/download/Linear_Algebra_Quick_Review_20260414140920.pptx",
  "theme_name": "math",
  "theme_domain": "math",
  "theme_source": "gemini_classifier",
  "theme_confidence": 0.73,
  "extraction_source": "gemini_vision",
  "extraction_quality": {
    "characters": 1144,
    "words": 201,
    "alpha_ratio": 0.811,
    "mean_confidence": 0.0
  },
  "output_format": "pptx",
  "timestamp": "2026-04-14T14:09:20.019491"
}
```

**Expected terminal output:**

```
INFO:     127.0.0.1:xxxxx - "POST /slides/from-image HTTP/1.1" 200 OK
```

### 5. `GET /slides/download/{filename}`

> In integrated AkadVerse deployments, browser download actions are served through the orchestrator proxy (`/downloads/slide/{filename}`). This endpoint remains the downstream service endpoint used by the orchestrator.

**What it does:**
Streams a previously generated file by exact filename from the `generated_slides` directory.

**Success response (200 OK):**
Returns the file stream with the correct media type.

**Expected terminal output:**

```
INFO:     127.0.0.1:xxxxx - "GET /slides/download/<filename> HTTP/1.1" 200 OK
```

**Error responses:**
- `404`: File not found or expired

### 6. `GET /health`

**What it does:**
Returns health and capability flags.

**Success response (200 OK):**

```json
{
  "status": "ok",
  "llm": "gemini-2.5-flash",
  "pptx_builder": true,
  "loaders": true,
  "ocr": true,
  "max_upload_size_mb": 10
}
```

### 7. `GET /`

**What it does:**
Returns service metadata and endpoint map.

**Success response (200 OK):**

```json
{
  "service": "Akadverse Slide Generator AI",
  "version": "1.0.0",
  "endpoints": {
    "from_text": "POST /slides/from-text",
    "from_url": "POST /slides/from-url",
    "from_document": "POST /slides/from-document (PDF, PPTX)",
    "from_image": "POST /slides/from-image",
    "download": "GET /slides/download/{filename}"
  },
  "llm": "gemini-2.5-flash",
  "max_upload_size_mb": 10,
  "status": "running"
}
```

## Testing with Swagger UI

With the server running, open:

```
http://127.0.0.1:8009/docs
```

For file upload endpoints, use the Swagger file picker for `file` or `image` fields.

## Example Test Inputs

### Test 1 - Health check

`GET /health`

**Expected:** `status` is `ok`, and capability flags reflect your local environment.

### Test 2 - Text generation baseline

`POST /slides/from-text` with:

```json
{
  "content": "Nigeria uses constitutional law, statutory law, and case law. Courts include magistrate courts, high courts, the court of appeal, and the supreme court. Conflict resolution methods include negotiation, mediation, arbitration, and litigation.",
  "subject": "Nigeria Legal System and Conflict Resolution",
  "num_slides": 6,
  "user_id": "STU-1001"
}
```

**Expected:** `theme_domain` resolves to `legal_governance`, and a downloadable filename is returned.

### Test 3 - URL extraction and generation

`POST /slides/from-url` with form fields:
- `url`: `https://en.wikipedia.org/wiki/Constitution`
- `subject`: `Constitutional Law Basics`
- `num_slides`: `7`

**Expected:** successful slide generation with valid `download_url`.

### Test 4 - Document upload path

`POST /slides/from-document` with a local PDF under 10MB.

**Expected:** successful file output. If file exceeds max size, response is `413`.

### Test 5 - Image OCR strong scan

`POST /slides/from-image` with a clear typed note image.

**Expected:** `extraction_source` is often `tesseract` and extraction quality metrics are present.

### Test 6 - Image OCR weak scan

`POST /slides/from-image` with a blurry or noisy image.

**Expected:** `extraction_source` switches to `gemini_vision` or `tesseract_fallback` if vision call fails.

### Test 7 - Download generated file

Call `GET /slides/download/{filename}` using the exact `filename` from Test 2 to 6.

**Expected:** browser downloads `.pptx` (or `.json` fallback if PPTX rendering is unavailable).

## Understanding the Responses

### Why does download require a filename

The service writes artifacts to `generated_slides` and serves files by exact filename. This keeps the API stateless, avoids session coupling, and lets any client retrieve generated output deterministically.

### What `theme_source` means

- `gemini_classifier`: semantic classifier selected domain above confidence threshold
- `keyword_fallback`: deterministic fallback selected a domain when model confidence was low
- `default`: no reliable signal, so neutral academic theme was selected

### Why `theme_confidence` can be low

If Gemini confidence is below threshold and fallback path is used, confidence is bounded to a conservative value. This indicates heuristic selection, not semantic certainty.

### Why image extraction source changes

The image pipeline scores OCR quality. If text quality is weak, it routes to Gemini Vision. If vision fails, it can return `tesseract_fallback` when OCR text is still usable.

### Why output can be `.json` instead of `.pptx`

If `python-pptx` is unavailable, the service returns a JSON structure file as fallback to preserve pipeline output instead of failing the request.

## Theme Domains and Selection Rules

|Domain|When it is selected|
|---|---|
|`legal_governance`|Law, judiciary, dispute resolution, constitutional topics|
|`science`|Biology, chemistry, physics, lab and experiment topics|
|`math`|Algebra, calculus, statistics, proofs, equations|
|`history`|Civilizations, timelines, revolutions, colonial contexts|
|`business`|Finance, strategy, leadership, market analysis|
|`technology`|Software, AI, ML, programming, cybersecurity|
|`health`|Medical, anatomy, wellness, nutrition topics|
|`arts`|Literature, music, culture, design, drama topics|
|`general_academic`|Fallback when no reliable domain signal is present|

## Generated Files

|File / Folder|What it is|
|---|---|
|`generated_slides/`|Output folder for generated deck files|
|`generated_slides/*.pptx`|Rendered PowerPoint presentations|
|`generated_slides/*_structure.json`|Fallback structure output when PPTX renderer is unavailable|
|`%TEMP%/{uuid}.pdf|.pptx|.jpg|.txt`|Temporary extraction files removed after request completion|

Suggested `.gitignore` additions:

```
generated_slides/
__pycache__/
.venv/
.env
*.db
```

## Common Errors and Fixes

**`ModuleNotFoundError: No module named 'pptx'`**

`python-pptx` is not installed.

```
pip install python-pptx
```

**`ModuleNotFoundError: No module named 'langchain_community'`**

Document loaders package is missing.

```
pip install langchain-community pypdf unstructured beautifulsoup4 lxml
```

**`TesseractNotFoundError`**

Tesseract OCR binary is not installed or not on PATH.

```
tesseract --version
```

Install Tesseract and add it to PATH if command fails.

**`Address already in use`**

Port `8009` is occupied by another process.

```
netstat -ano | findstr :8009
```

Stop the occupying process or run the service on a different port.

**`HTTP 413 File too large`**

Uploaded file exceeds `MAX_UPLOAD_SIZE_MB`.

Set a higher value in `.env` only if your deployment can handle larger payloads.

**`Gemini not available - running in MOCK mode`**

`GOOGLE_API_KEY` is missing or invalid.

```
python -c "import os; from dotenv import load_dotenv; load_dotenv(); print(os.getenv('GOOGLE_API_KEY'))"
```

## Project Structure

```
|-- slide_generator.py              # Main FastAPI service
|-- requirements.txt                # Python dependencies for this service
|-- .env                            # Environment variables (not committed)
|-- generated_slides/               # Runtime output folder for generated files
|-- slide_creator docs/             # Service documentation package
|   |-- README.md                   # End to end setup and test guide
|   |-- requirements.txt            # Reproducible dependency list
|   `-- .gitignore                  # Recommended ignore rules for this service
`-- .venv/                          # Local virtual environment (not committed)
```

## Part of the AkadVerse Platform

This microservice is **Tier 5** in the AkadVerse AI architecture, operating within the *My Learning* module alongside:

- Notes Creator (Port 8003)
- Note-to-Animations (Port 8005)
- Note-to-Audio (Port 8006)
- Assignment Generator (Port 8007)

The slide generator currently does not publish a Kafka event in this local service implementation.

---

*AkadVerse AI Architecture v1.0*
