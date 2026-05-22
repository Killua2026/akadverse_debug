Let’s design the orchestrator’s internals step-by-step. we’re building a **Python/FastAPI service**

---

## 1. High-Level Component Diagram

```
Frontend (Unified Chat UI)(akadverse_demo_frontend.html)
       │ POST /chat
       ▼
┌─────────────────────────────────────┐
│         Orchestrator Service        │
│                                     │
│ 1. Session Manager                  │
│ 2. Context Assembler (→ Stem)       │
│ 3. Router (LLM with Tools)          │
│ 4. Tool Registry                    │
│ 5. Tool Invoker (abstraction)       │
│ 6. Response Composer                │
│                                     │
└──────┬──────────────────┬───────────┘
       │                  │
  ┌────▼────┐      ┌──────▼──────┐
  │  Stem   │      │ Tool Services│
  │ (Redis/ │      │  (HTTP/gRPC) │
  │  DB)    │      └──────────────┘
  └─────────┘
```

Data flow:  
**User message → Orchestrator → Assembles context from Stem → Router decides (reply or call tool) → Tool invoked → Tool result → Router formats user response → Reply to user.**

---

## 2. Core Components Explained

### 2.1 Session Manager
- Maintains **conversation history** (list of messages) per user/session.
- Keyed by `session_id`. Can store in memory (with redis for persistence) or directly in the Stem.
- Each message includes role (`user`, `assistant`, `tool`), content, timestamp, and optionally a `tool_call_id` to pair tool requests with responses.
- Exposes: `get_history(session_id) → List[Message]`, `append(session_id, message)`.

### 2.2 Context Assembler (The “Stem” Abstraction)
- Before routing, enriches the prompt with relevant data from the central stem (Red Panda stem).
- The stem holds:
  - User profile / preferences.
  - Current document/session context (e.g., a book the user is studying).
  - Recent outputs from other tools (for cross-tool continuity).
- **Abstraction**: define `StemClient` interface with methods like:
  - `get_user_context(user_id) → dict`
  - `get_active_document(session_id) → str`
  - `store_tool_output(session_id, tool_name, data)`
- **Early implementation**: a simple Redis store with JSON blobs. Later, replace with Red Panda state stores or a dedicated service.
- The Assembler takes the user message + session history, fetches required stem data, and constructs a **system prompt fragment** summarizing the user’s “current state.”

### 2.3 Tool Registry
- A dynamic catalogue of available AI tools (quiz generator, image creator, note maker, etc.).
*   Port `8003`: `akadverse-youtube-recommender/recommender.py`
*   Port `8004`: `marketplace_api.py`
*   Port `8005`: `akadverse- google-workspace-service\main.py`
*   Port `8006`: `akadverse-resource-tracker\main.py`
*   Port `8007`: `akadverse-schedule-manager\main.py`
*   Port `8008`: `notes_creator.py`
*   Port `8009`: `slide_generator.py`
*   Port `8010`: `concept_explainer.py`
*   Port `8011`: `resource_puller.py`
*   Port `8012`: `assignment_generator.py`
*   Port `8013`: `sample_questions_generator.py`
*   Port `8014`: `note_to_audio.py`
*   Port `8015`: `note_to_animations.py`
*   Port `8016`: `quiz_generator.py`
*   Port `8017`: `practice_questions_generator.py`
*   Port `8018`: `attendance_ai.py`
*   Port `8019`: `grade_upload_ai.py`
- Each tool entry defines:
  - `name` (unique ID)
  - `description` (for LLM, natural language what it does)
  - `parameters` (JSON Schema for function calling)
  - `endpoint` (HTTP URL or bus topic)
  - `auth`/headers if needed
  - `timeout`, `retry` policy
- Loaded at startup from configuration or a database, can be hot-reloaded.
- The registry is used to generate the **tool definitions** for the LLM router.

### 2.4 Router (Intelligent Dispatcher)
This is the brain. I recommend using a **LLM with tool-use/function-calling** like Gemini. I want you to use Gemini. The logic:

1. **Build prompt** with:
   - System message that includes:
     - The assistant’s personality and rules (be helpful, never initiate a tool unless needed, etc.).
     - The current context from the Stem (what the user is working on).
     - Explicit instructions on tool usage: “If user asks for an quiz, call `generate_quiz`. If a greeting, reply normally. If a question, answer directly or call `search_web` if needed. If user message matches none of the tools, respond normally.”
   - Conversation history (last N messages, including past tool calls and their results).
   - The new user message.

2. **Send to LLM** with `tools` parameter (list of tool definitions from the registry).
3. **Interpret LLM response**:
   - If `finish_reason == "stop"` and no tool_calls: a normal text response → hand to Response Composer.
   - If `model_response` contains `tool_calls`:
     - Extract `tool_name` and `arguments`.
     - Validate arguments against the tool’s schema.
     - Call the Tool Invoker.
     - **Feed the tool result back** to the LLM as a `tool` role message with the corresponding `tool_call_id`, and continue the conversation loop until the LLM produces a final text response.
   - If the LLM wants multiple tool calls, handle them in parallel or sequentially, then continue.

**Important**: This loop allows the LLM to “think” and possibly correct an incorrectly activated tool because it sees the result before replying to the user. For example, if a user says “hi” to the image creator UI (where the front-end may add a prompt hint), the LLM can see the context and decide *not* to call the tool, just reply with a greeting.

If using a model that doesn’t natively support tool/function calling, you can build an intent classifier (fine-tuned small model, or a separate LLM call) that outputs a structured intent, but the native tool-use approach is cleaner.

### 2.5 Tool Invoker (Abstraction Layer)
- Interface: `async invoke(tool_name: str, params: dict, session_id: str) → ToolResult`
- **Phase 1 (direct)**: The invoker looks up the tool’s HTTP endpoint from the registry, makes an async HTTP POST, awaits the result (with timeout/retry), and returns it.
- **Phase 2 (bus-backed)**: The invoker publishes a message to Red Panda topic `tool.requests` with a correlation ID, then sets up a temporary listener or uses an async callback. The tool processes and publishes to `tool.responses`. The invoker resolves when the matching response arrives (or times out).
- Both phases conform to the same `Invoker` interface, so the router remains agnostic.

### 2.6 Response Composer
- Takes the final LLM text response (after all tool calls have been processed) and wraps it in a user-friendly format (could include rich elements like images or buttons).
- May also append the assistant’s response to the session history.
- Optionally logs the entire conversation turn to the Stem for compliance/analytics.

---

## 3. The Router Loop in Detail (Pseudo-code)

```python
async def process_message(session_id: str, user_message: str):
    history = session_manager.get_history(session_id)
    context = await stem_client.get_context(session_id)
    
    # Prepare system prompt with context and tool guidance
    system_prompt = build_system_prompt(context, tool_registry.list_tools())
    
    messages = [system_prompt] + history + [{"role": "user", "content": user_message}]
    
    while True:
        llm_response = await llm.chat(
            messages=messages,
            tools=tool_registry.get_tool_definitions(),
            tool_choice="auto"  # let model decide
        )
        
        if llm_response.has_tool_calls():
            # Store the assistant message with tool calls in history
            messages.append(llm_response.assistant_message)
            
            tool_results = []
            for tc in llm_response.tool_calls:
                tool_name = tc.function.name
                args = json.loads(tc.function.arguments)
                # Invoke tool (abstracted)
                result = await tool_invoker.invoke(tool_name, args, session_id)
                tool_results.append({
                    "tool_call_id": tc.id,
                    "role": "tool",
                    "content": json.dumps(result)
                })
            
            # Append all tool results to messages
            messages.extend(tool_results)
            # Loop back to LLM to generate final response
            continue
        
        # No tool calls: final text answer
        final_text = llm_response.content
        session_manager.append(session_id, {"role": "user", "content": user_message})
        session_manager.append(session_id, {"role": "assistant", "content": final_text})
        return final_text
```

**Error handling**: If a tool fails/timeout, the tool result should be `{"error": "Tool unavailable, try again later."}` and let the LLM compose an apology or fallback.

---

## 4. Interaction with the Red Panda Stem (Future)

When you introduce the Red panda bus later, the orchestrator remains largely the same. The stem client becomes:

- **Reads** from a compacted topic or a materialized view for user/session state.
- **Writes** tool outputs (and conversation logs) to topics so other services can react.
- The **Tool Invoker** evolves from HTTP to a bus-based request-reply pattern. You might use a library that supports async request-reply over Kafka/Redpanda (like `aiokafka` with consumer for response, or a temporary inbox).

A typical flow with bus:
1. Orchestrator publishes to `tool.requests` with a header `correlation_id` and a `reply_topic` (perhaps `tool.responses`).
2. Orchestrator listens to a dedicated consumer group on `tool.responses` filtering by correlation_id.
3. Tool services consume from `tool.requests`, process, publish result to `tool.responses` with the same correlation_id.
4. Orchestrator picks up the response and resumes the LLM loop.

Because the loop may involve multiple sequential tool calls, you’d maintain the LLM conversation state in memory/Redis while waiting for bus responses. This is fine as long as you handle timeouts and retries gracefully.

---

## 5. Streaming and User Experience

- For a smoother UX, stream the final assistant response token-by-token (using Server-Sent Events or WebSocket) once the tool results are in and the LLM is generating the final answer.
- The orchestrator’s API can return `text/event-stream` after the tool-calling phase, or it can split into two endpoints: one to submit the message (which returns a job ID) and another to stream the result. I recommend the simpler single POST that streams, as it’s compatible with modern chat UIs.

---

## 6. Security & Guardrails

- In the system prompt, add strict instructions not to call tools unless explicitly required by the user’s intent. For example: `“Only use a tool when the user directly requests its function. If a user says ‘hello’ even if the UI suggests a tool, respond normally.”`
- Validate user input size and sanitize against prompt injection.
- Tools must be treated as untrusted; the orchestrator should never execute arbitrary code from tool outputs. Only pass structured, validated data to the user.

---

## 7. Phased Rollout Plan

1. **Phase 0 – Mock tools + Direct LLM Router**: Use a single Gemini API key, define 2-3 mock tool functions that return static data, build the whole loop. Verify that a greeting does not trigger a tool, but “generate a quiz about photosynthesis” does. No Red Panda yet.
2. **Phase 1 – Real Tools via HTTP**: Implement actual tool services (quiz, slides, note) as simple FastAPI endpoints. Integrate them into the registry. Use synchronous HTTP calls. Have the frontend point to the orchestrator.
3. **Phase 2 – Stem Integration**: Replace the dummy context with a Redis-based Stem that holds a “current book” or “user level” that tools can access. The orchestrator enriches the prompt.
4. **Phase 3 – Bus Migration**: Add Red Panda topics, implement a `BusToolInvoker` that adheres to the same interface, swap it in. Now you can scale tools independently and process long-running tasks asynchronously.

---