# AkadVerse Orchestrator Implementation Checklist

Status: skeleton first, then module-by-module implementation.

## Package Skeleton
1. Create `app/__init__.py` as the package entry point.
2. Create `app/models.py` for all shared request, response, session, message, and tool data models.
3. Create `app/config.py` for runtime settings and config loading.
4. Create `app/session.py` for session history storage and trimming.
5. Create `app/stem.py` for the context store abstraction and in-memory fallback.
6. Create `app/tools.py` for the tool registry, tool metadata, and invoker interface.
7. Create `app/router.py` for the Gemini tool-calling loop.
8. Create `app/composer.py` for final response shaping.
9. Create `app/main.py` for the FastAPI app, `/chat`, `/health`, and CORS wiring.
10. Create `config.yaml` for tool and orchestrator settings.
11. Create `tests/__init__.py` and add test files for the model layer, router, and API surface.

## Module Build Order
1. `app/models.py` - implement the canonical data contracts first.
2. `app/session.py` - implement in-memory session history with a 20-message cap.
3. `app/stem.py` - implement the context store abstraction and in-memory stem client.
4. `app/config.py` - implement config loading from environment and `config.yaml`.
5. `app/tools.py` - implement the tool registry and HTTP invoker contract.
6. `app/router.py` - implement the Gemini tool loop and tool-call handling.
7. `app/composer.py` - implement the final response composer.
8. `app/main.py` - wire the FastAPI routes to the orchestration flow.
9. `orchestrator.py` - convert to a thin compatibility wrapper once `app/main.py` is stable.

## Validation Order
1. Syntax check `app/models.py` after the first implementation.
2. Validate session and stem helpers after their module passes syntax.
3. Validate the router with a mocked Gemini response and a mocked tool call.
4. Validate `POST /chat` and `GET /health` after the FastAPI wiring lands.

## Target File Map
- `akadverse-orchestrator/app/__init__.py`
- `akadverse-orchestrator/app/models.py`
- `akadverse-orchestrator/app/config.py`
- `akadverse-orchestrator/app/session.py`
- `akadverse-orchestrator/app/stem.py`
- `akadverse-orchestrator/app/tools.py`
- `akadverse-orchestrator/app/router.py`
- `akadverse-orchestrator/app/composer.py`
- `akadverse-orchestrator/app/main.py`
- `akadverse-orchestrator/config.yaml`
- `akadverse-orchestrator/tests/__init__.py`
- `akadverse-orchestrator/tests/test_models.py`
- `akadverse-orchestrator/tests/test_router.py`
- `akadverse-orchestrator/tests/test_chat_api.py`