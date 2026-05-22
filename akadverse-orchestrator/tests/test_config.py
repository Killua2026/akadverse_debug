"""Configuration loading tests for the AkadVerse orchestrator."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app import config


class FakeModels:
  def __init__(self, names: list[str]):
    self._names = names

  def list(self):
    return [SimpleNamespace(name=f"models/{name}") for name in self._names]


class FakeClient:
  def __init__(self, names: list[str]):
    self.models = FakeModels(names)


def test_get_valid_model_name_prefers_configured_model(monkeypatch) -> None:
  monkeypatch.setattr(config.genai, "Client", lambda api_key: FakeClient(["gemini-2.0-flash", "gemini-2.0-flash-lite"]))

  result = config.get_valid_model_name("fake-key", "gemini-2.0-flash")

  assert result == "gemini-2.0-flash"


def test_get_valid_model_name_falls_back_when_preferred_is_missing(monkeypatch) -> None:
  monkeypatch.setattr(config.genai, "Client", lambda api_key: FakeClient(["gemini-2.0-flash-lite"]))

  result = config.get_valid_model_name("fake-key", "gemini-2.5-flash")

  assert result == "gemini-2.0-flash-lite"


def test_get_valid_model_name_uses_default_when_discovery_fails(monkeypatch) -> None:
  class BrokenModels:
    @staticmethod
    def list():
      raise RuntimeError("model listing failed")

  class BrokenClient:
    def __init__(self) -> None:
      self.models = BrokenModels()

  monkeypatch.setattr(config.genai, "Client", lambda api_key: BrokenClient())

  result = config.get_valid_model_name("fake-key", "gemini-2.5-flash")

  assert result == "gemini-2.5-flash"


def test_load_settings_discovers_model_and_parses_tools(monkeypatch, tmp_path: Path) -> None:
  monkeypatch.setattr(config.genai, "Client", lambda api_key: FakeClient(["gemini-2.0-flash", "gemini-2.0-flash-lite"]))
  monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")

  config_path = tmp_path / "config.yaml"
  config_path.write_text(
    """
llm:
  provider: gemini
  model_name: gemini-2.5-flash
  model_fallbacks:
    - gemini-2.5-flash
    - gemini-2.0-flash
    - gemini-2.0-flash-lite
session:
  max_messages: 20
tools:
  - name: quiz_generator
    description: Generates quizzes
    parameters:
      type: object
      properties:
        topic:
          type: string
      required: [topic]
    endpoint: http://localhost:8016/quiz
    timeout_seconds: 12
    retry_count: 1
""".strip(),
    encoding="utf-8",
  )

  settings = config.load_settings(config_path)

  assert settings.llm_provider == "gemini"
  assert settings.model_name == "gemini-2.0-flash"
  assert settings.model_fallbacks == ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite"]
  assert len(settings.tools) == 1
  assert settings.tools[0].name == "quiz_generator"
  assert settings.tools[0].endpoint == "http://localhost:8016/quiz"
  assert settings.tools[0].timeout_seconds == 12
  assert settings.tools[0].retry_count == 1
