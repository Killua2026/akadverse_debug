"""Model layer tests for the AkadVerse orchestrator."""

from __future__ import annotations

from app.models import Message, Session, ToolDefinition


def test_message_serialization_round_trip() -> None:
    message = Message(role="user", content="Hello orchestrator")

    serialized = message.model_dump()
    restored = Message.model_validate(serialized)

    assert restored.role == "user"
    assert restored.content == "Hello orchestrator"
    assert restored.timestamp == message.timestamp


def test_session_instantiation_and_serialization() -> None:
    first = Message(role="user", content="Hi")
    second = Message(role="assistant", content="Hello")
    session = Session(session_id="S-001", user_id="U-001", messages=[first, second])

    serialized = session.model_dump()
    restored = Session.model_validate(serialized)

    assert restored.session_id == "S-001"
    assert restored.user_id == "U-001"
    assert [msg.content for msg in restored.messages] == ["Hi", "Hello"]


def test_tool_definition_instantiation() -> None:
    tool = ToolDefinition(
        name="quiz_generator",
        description="Generate quiz questions",
        parameters={
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
            },
            "required": ["topic"],
        },
        endpoint="http://localhost:8016/quiz",
        timeout_seconds=15,
        retry_count=2,
    )

    serialized = tool.model_dump()
    restored = ToolDefinition.model_validate(serialized)

    assert restored.name == "quiz_generator"
    assert restored.endpoint == "http://localhost:8016/quiz"
    assert restored.timeout_seconds == 15
    assert restored.retry_count == 2
