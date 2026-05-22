import asyncio
from unittest.mock import AsyncMock
import httpx
import os
import sys

# Ensure the current directory is in the path so 'app' can be imported
sys.path.append(os.getcwd())

from app.config import load_settings
from app.tools import ToolRegistry, HttpToolInvoker

async def run_check():
    print("--- Phase 1 & 2: Loading settings and building ToolRegistry ---")
    settings = load_settings("config.yaml")
    registry = ToolRegistry.from_settings(settings)
    
    print("--- Phase 3: Printing tool information ---")
    tools = registry.list_tools()
    print(f"Loaded tool count: {len(tools)}")
    print("A few tool names:")
    for tool in tools[:3]:
        print(f"  - {tool.name}")
    
    print("\n--- Phase 4: Asserting quiz_generator configuration ---")
    quiz_tool = registry.get("quiz_generator")
    print(f"quiz_generator endpoint: {quiz_tool.endpoint}")
    print(f"quiz_generator retry_count: {quiz_tool.retry_count}")
    print(f"quiz_generator timeout_seconds: {quiz_tool.timeout_seconds}")
    
    assert str(quiz_tool.endpoint) == 'http://localhost:8016/generate-quiz'
    assert quiz_tool.retry_count is not None
    assert quiz_tool.timeout_seconds is not None
    print("Assertions for quiz_generator passed.")

    print("\n--- Phase 5: Proving HttpToolInvoker._post_once captures URL ---")
    # Use a fake async client whose post method captures the URL
    fake_client = AsyncMock(spec=httpx.AsyncClient)
    invoker = HttpToolInvoker(registry)
    
    session_id = 'test-session'
    params = {'topic': 'AI'}
    
    # We call _post_once directly as requested
    await invoker._post_once(fake_client, quiz_tool, params, session_id)
    
    # Check if post was called with the correct endpoint
    called_url = fake_client.post.call_args[0][0]
    print(f"Captured URL in _post_once: {called_url}")
    
    assert str(called_url) == str(quiz_tool.endpoint)
    print("HttpToolInvoker URL capture proof passed.")

if __name__ == '__main__':
    try:
        asyncio.run(run_check())
        print("\nOVERALL STATUS: PASS")
    except Exception as e:
        print(f"\nOVERALL STATUS: FAIL - {e}")
        import traceback
        traceback.print_exc()
