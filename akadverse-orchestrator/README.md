# AkadVerse Orchestrator

Tier 1 orchestration service for AkadVerse on port 8000.

This README reflects the current implementation in this folder.

## What Is Running Today

The orchestrator is a FastAPI service that:

- Accepts chat messages via `POST /chat`
- Routes conversation turns through Gemini (with model fallback)
- Lets Gemini call configured HTTP tools from `config.yaml`
- Returns a unified response payload with optional action metadata
- Stores session history in memory with a bounded window
- Stores STEM context in memory

Current state summary:

- Session storage: in-memory (`InMemorySessionManager`)
- STEM context storage: in-memory (`InMemoryStemClient`)
- Long-term persistence: not wired yet in this service
- Tool wiring: config-driven via `config.yaml`

## High-Level Flow

1. Request arrives at `POST /chat`.
2. Session history is loaded from in-memory store.
3. STEM context is loaded and converted into system instruction context.
4. Router sends the conversation to Gemini.
5. If Gemini issues function calls, orchestrator invokes matching tools over HTTP.
6. Final reply is composed and returned.
7. User and assistant messages are appended to in-memory session history.

## API Contract

### POST /chat

Request body:

```json
{
  "session_id": "S-123",
  "message": "Generate a quiz on photosynthesis",
  "user_id": "23CE034397",
  "agent_hint": "quiz generator"
}
```

Notes:

- Required fields: `session_id`, `message`
- Optional fields: `user_id`, `agent_hint`
- `agent_hint` is accepted but routing is currently driven by model/tool logic

Success response:

```json
{
  "reply": "Here is your quiz.",
  "tool_used": "quiz_generator",
  "action": null
}
```

Download-style response example:

```json
{
  "reply": "Your slide deck is ready. Click the download button below.",
  "tool_used": "slide_generator",
  "action": {
    "type": "download",
    "url": "http://127.0.0.1:8009/slides/download/presentation.pptx",
    "label": "Download Slide Generator"
  }
}
```

Failure behavior:

- If Gemini is not configured, response is graceful with a setup hint.
- If Gemini model calls fail, router tries fallback models.
- If all model attempts fail, a user-safe fallback message is returned.
- If tool invocation fails, failure is handled gracefully and returned to the model loop.

### GET /health

Response shape:

```json
{
  "status": "healthy",
  "selected_model": "gemini-2.5-flash",
  "llm_ready": true,
  "session_ready": true,
  "stem_ready": true,
  "tools_ready": true
}
```

`selected_model` is the model chosen at startup after dynamic discovery.

## Configuration

Settings are loaded from environment variables plus `config.yaml`.

### Environment Variables

- `GOOGLE_API_KEY`: enables Gemini routing
- `OPENAI_API_KEY`: reserved in settings but not used by current router path
- `LLM_PROVIDER`: default `gemini`
- `LLM_MODEL_NAME`: preferred Gemini model
- `LLM_MODEL_FALLBACKS`: comma-separated fallback list
- `AKADVERSE_ORCHESTRATOR_CONFIG`: path to YAML config file (default `config.yaml`)
- `SESSION_LIMIT`: max messages retained per session (default `20`)
- `CORS_ORIGINS`: comma-separated allowed origins (default `*`)
- `STEM_BACKEND`: default `memory`

### YAML Config (`config.yaml`)

Defines:

- LLM defaults
- Session defaults
- STEM backend label
- Tool definitions (name, schema, endpoint, timeout, retry policy, request format)

## Tool Invocation Model

Tools are registered from config and exposed to Gemini as function declarations.

Invocation details:

- Default request format: JSON
- JSON payload:

```json
{
  "session_id": "S-123",
  "tool_name": "quiz_generator",
  "params": {
    "topic": "photosynthesis"
  }
}
```

- Form payload is supported per-tool (`request_format: form`), used by services like `slide_generator`.
- Request headers include:
  - `X-Akadverse-Session-Id`
  - `X-Akadverse-Tool-Name`
- Timeout and retries are per tool (`timeout_seconds`, `retry_count`).

## Service Ports in Current Tool Config

Configured endpoints currently map to these tool services:

- 8003: YouTube Recommender
- 8004: Marketplace API
- 8005: Google Workspace Service
- 8006: Resource Tracker
- 8007: Schedule Manager
- 8008: Notes Creator
- 8009: Slide Generator
- 8010: Concept Explainer
- 8011: External Resources Puller
- 8012: Assignment Generator
- 8013: Sample Questions Generator
- 8014: Note to Audio
- 8015: Note to Animations
- 8016: Quiz Generator
- 8017: Practice Questions Generator
- 8018: Attendance AI
- 8019: Grade Upload AI

## Installation

From this folder:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Create `.env` in this folder (minimum):

```env
GOOGLE_API_KEY=your_key_here
```

## Running the Service

Option 1 (direct uvicorn):

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Option 2 (wrapper script):

```bash
python orchestrator.py
```

Open API docs:

- http://127.0.0.1:8000/docs

Health check:

- http://127.0.0.1:8000/health

## Testing

Run from this folder:

```bash
pytest -q
```

Core covered behavior in tests:

- Health endpoint availability
- Chat response contract
- Tool-call and non-tool paths
- Download action payload handling
- Config loading and model discovery behavior
- HTTP tool invoker payload format and error handling
- Session cap behavior
- STEM context storage behavior

## Project Structure

```text
akadverse-orchestrator/
|-- app/
|   |-- main.py
|   |-- router.py
|   |-- tools.py
|   |-- config.py
|   |-- session.py
|   |-- stem.py
|   |-- composer.py
|   |-- models.py
|-- tests/
|-- config.yaml
|-- orchestrator.py
|-- requirements.txt
|-- README.md
```

## Important Implementation Notes

- The previous Redis/Mongo-backed memory model is not currently active in this code path.
- Session and STEM state are process-local and reset on restart.
- Tool availability depends on downstream microservices being reachable.
- Router loop is capped to prevent infinite tool-call cycles.
- Download actions are surfaced when tool responses contain `download_url` or `file_url`.

## Troubleshooting

- `Gemini is not configured...`
  - Set `GOOGLE_API_KEY` in `.env` and restart.

- Model unavailable or quota exhausted
  - Router automatically tries fallback models.
  - If all fail, user-safe fallback text is returned.

- Tool unavailable
  - Check downstream service status and endpoint in `config.yaml`.
  - Verify payload schema expected by that service.

- Empty or unexpected replies
  - Check orchestrator logs for router/tool events and model errors.
