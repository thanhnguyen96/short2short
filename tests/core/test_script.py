from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from short2short.core.models import SceneState
from short2short.core.script import FileScriptGenerator, OpenAIScriptGenerator
from short2short.errors import PipelineError


class FakeChoice:
    """Mock choice object containing message content."""

    def __init__(self, content: str | None) -> None:
        self.message = MagicMock()
        self.message.content = content


class FakeResponse:
    """Mock response object mimicking openai ChatCompletion."""

    def __init__(self, content: str | None) -> None:
        self.choices = [FakeChoice(content)]


class FakeCompletions:
    """Mock completions interface capturing calls and yielding configured responses."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.responses:
            raise RuntimeError("No more fake responses configured")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)


class FakeOpenAIClient:
    """Mock OpenAI client hierarchy."""

    def __init__(self, responses: list[Any]) -> None:
        self.chat = MagicMock()
        self.chat.completions = FakeCompletions(responses)


def test_openai_script_generator_happy_path() -> None:
    valid_payload = json.dumps(
        {
            "scenes": [
                {"narration": "Line 1", "visual_prompt": "Visual 1"},
                {"narration": "Line 2", "visual_prompt": "Visual 2"},
            ]
        }
    )
    fake_client = FakeOpenAIClient([valid_payload])
    generator = OpenAIScriptGenerator(
        base_url="https://api.example.com/v1",
        api_key="sk-test-key",
        model="test-model",
        client=fake_client,
    )

    script = generator.generate(
        "AI Revolution",
        lang="vi",
        scene_count=2,
        seconds_per_scene=8,
    )

    assert script.topic == "AI Revolution"
    assert script.lang == "vi"
    assert len(script.scenes) == 2
    assert script.scenes[0].index == 0
    assert script.scenes[0].narration == "Line 1"
    assert script.scenes[0].visual_prompt == "Visual 1"
    assert script.scenes[0].state == SceneState.PENDING
    assert script.scenes[1].index == 1
    assert script.scenes[1].narration == "Line 2"
    assert script.scenes[1].visual_prompt == "Visual 2"

    calls = fake_client.chat.completions.calls
    assert len(calls) == 1
    assert calls[0]["model"] == "test-model"
    assert calls[0]["temperature"] == 0.7
    assert calls[0]["response_format"] == {"type": "json_object"}
    messages = calls[0]["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "Produce exactly 2 scenes." in messages[0]["content"]
    assert "narration is spoken aloud in vi." in messages[0]["content"]
    assert "about 8 seconds of speech" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "Topic: AI Revolution"}


def test_openai_script_generator_code_fence() -> None:
    fenced_content = (
        '```json\n{"scenes": [{"narration": "Line 1", "visual_prompt": "Visual 1"}]}\n```'
    )
    fake_client = FakeOpenAIClient([fenced_content])
    generator = OpenAIScriptGenerator(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="test-model",
        client=fake_client,
    )

    script = generator.generate("Topic", lang="en", scene_count=1, seconds_per_scene=5)
    assert len(script.scenes) == 1
    assert script.scenes[0].narration == "Line 1"


def test_openai_script_generator_retry_on_parse_failure() -> None:
    bad_json = "not json at all"
    good_json = json.dumps({"scenes": [{"narration": "Line 1", "visual_prompt": "Visual 1"}]})
    fake_client = FakeOpenAIClient([bad_json, good_json])
    generator = OpenAIScriptGenerator(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="test-model",
        client=fake_client,
    )

    script = generator.generate("Topic", lang="en", scene_count=1, seconds_per_scene=5)
    assert len(script.scenes) == 1
    assert len(fake_client.chat.completions.calls) == 2


def test_openai_script_generator_retry_on_count_mismatch() -> None:
    mismatched = json.dumps(
        {
            "scenes": [
                {"narration": "Line 1", "visual_prompt": "Visual 1"},
                {"narration": "Line 2", "visual_prompt": "Visual 2"},
            ]
        }
    )
    matched = json.dumps({"scenes": [{"narration": "Line 1", "visual_prompt": "Visual 1"}]})
    fake_client = FakeOpenAIClient([mismatched, matched])
    generator = OpenAIScriptGenerator(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="test-model",
        client=fake_client,
    )

    script = generator.generate("Topic", lang="en", scene_count=1, seconds_per_scene=5)
    assert len(script.scenes) == 1
    assert len(fake_client.chat.completions.calls) == 2


def test_openai_script_generator_fails_after_two_validation_errors() -> None:
    bad1 = "bad json 1"
    bad2 = "bad json 2"
    fake_client = FakeOpenAIClient([bad1, bad2])
    generator = OpenAIScriptGenerator(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="test-model",
        client=fake_client,
    )

    with pytest.raises(PipelineError, match="validation"):
        generator.generate("Topic", lang="en", scene_count=1, seconds_per_scene=5)
    assert len(fake_client.chat.completions.calls) == 2


def test_openai_script_generator_count_mismatch_raises_pipeline_error() -> None:
    mismatched = json.dumps(
        {
            "scenes": [
                {"narration": "Line 1", "visual_prompt": "Visual 1"},
                {"narration": "Line 2", "visual_prompt": "Visual 2"},
            ]
        }
    )
    fake_client = FakeOpenAIClient([mismatched, mismatched])
    generator = OpenAIScriptGenerator(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="test-model",
        client=fake_client,
    )

    with pytest.raises(PipelineError, match="Scene count mismatch"):
        generator.generate("Topic", lang="en", scene_count=1, seconds_per_scene=5)


def test_openai_script_generator_api_error_raises_pipeline_error() -> None:
    fake_client = FakeOpenAIClient([RuntimeError("Network timeout")])
    generator = OpenAIScriptGenerator(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="test-model",
        client=fake_client,
    )

    with pytest.raises(PipelineError, match="LLM request failed"):
        generator.generate("Topic", lang="en", scene_count=1, seconds_per_scene=5)


def test_openai_script_generator_redacts_secrets_in_error() -> None:
    fake_client = FakeOpenAIClient([RuntimeError("Failed with secret sk-1234567890abcdef12345")])
    generator = OpenAIScriptGenerator(
        base_url="https://api.example.com/v1",
        api_key="sk-1234567890abcdef12345",
        model="test-model",
        client=fake_client,
    )

    with pytest.raises(PipelineError) as exc_info:
        generator.generate("Topic", lang="en", scene_count=1, seconds_per_scene=5)
    assert "sk-1234567890abcdef12345" not in str(exc_info.value)
    assert "***" in str(exc_info.value)


def test_openai_script_generator_repr_hides_api_key() -> None:
    generator = OpenAIScriptGenerator(
        base_url="https://api.example.com/v1",
        api_key="sk-super-secret-key",
        model="test-model",
        client=FakeOpenAIClient([]),
    )
    representation = repr(generator)
    assert "sk-super-secret-key" not in representation
    assert "test-model" in representation


def test_openai_script_generator_default_client_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_openai_cls = MagicMock()
    monkeypatch.setattr("openai.OpenAI", mock_openai_cls)

    generator = OpenAIScriptGenerator(
        base_url="https://api.custom.com/v1",
        api_key="sk-custom-key",
        model="custom-model",
        timeout=45.0,
    )
    mock_openai_cls.assert_called_once_with(
        base_url="https://api.custom.com/v1",
        api_key="sk-custom-key",
        timeout=45.0,
        max_retries=0,
    )
    assert generator.client == mock_openai_cls.return_value


def test_file_script_generator_happy_path(tmp_path: Path) -> None:
    script_file = tmp_path / "script.json"
    script_file.write_text(
        json.dumps(
            {
                "scenes": [
                    {"index": 99, "narration": "Scene 1", "visual_prompt": "Shot 1"},
                    {"narration": "Scene 2", "visual_prompt": "Shot 2"},
                ]
            }
        ),
        encoding="utf-8",
    )

    generator = FileScriptGenerator(script_file)
    # scene_count=5 should be ignored per specification A7
    script = generator.generate(
        "Custom Topic",
        lang="vi",
        scene_count=5,
        seconds_per_scene=10,
    )

    assert script.topic == "Custom Topic"
    assert script.lang == "vi"
    assert len(script.scenes) == 2
    # Sequential reindexing
    assert script.scenes[0].index == 0
    assert script.scenes[0].narration == "Scene 1"
    assert script.scenes[0].visual_prompt == "Shot 1"
    assert script.scenes[1].index == 1
    assert script.scenes[1].narration == "Scene 2"
    assert script.scenes[1].visual_prompt == "Shot 2"


def test_file_script_generator_with_fenced_json(tmp_path: Path) -> None:
    script_file = tmp_path / "script.json"
    content = (
        "```json\n"
        '{"scenes": [{"narration": "Fenced narration", "visual_prompt": "Fenced visual"}]}\n'
        "```"
    )
    script_file.write_text(content, encoding="utf-8")

    generator = FileScriptGenerator(str(script_file))
    script = generator.generate("Topic", lang="en", scene_count=1, seconds_per_scene=5)
    assert len(script.scenes) == 1
    assert script.scenes[0].narration == "Fenced narration"


def test_file_script_generator_file_not_found(tmp_path: Path) -> None:
    missing_file = tmp_path / "nonexistent.json"
    generator = FileScriptGenerator(missing_file)
    with pytest.raises(PipelineError, match="Script file not found"):
        generator.generate("Topic", lang="en", scene_count=1, seconds_per_scene=5)


def test_file_script_generator_invalid_json(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("invalid json content", encoding="utf-8")
    generator = FileScriptGenerator(bad_file)
    with pytest.raises(PipelineError, match="Invalid JSON in script file"):
        generator.generate("Topic", lang="en", scene_count=1, seconds_per_scene=5)


def test_file_script_generator_missing_scenes_key(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.json"
    bad_file.write_text('{"other": 1}', encoding="utf-8")
    generator = FileScriptGenerator(bad_file)
    with pytest.raises(PipelineError, match="missing 'scenes' key"):
        generator.generate("Topic", lang="en", scene_count=1, seconds_per_scene=5)


def test_file_script_generator_empty_scenes(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.json"
    bad_file.write_text('{"scenes": []}', encoding="utf-8")
    generator = FileScriptGenerator(bad_file)
    with pytest.raises(PipelineError, match="Invalid scenes in script file"):
        generator.generate("Topic", lang="en", scene_count=1, seconds_per_scene=5)


def test_file_script_generator_invalid_scene_fields(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(
        json.dumps({"scenes": [{"narration": "", "visual_prompt": "Visual"}]}),
        encoding="utf-8",
    )
    generator = FileScriptGenerator(bad_file)
    with pytest.raises(PipelineError, match="Invalid scenes in script file"):
        generator.generate("Topic", lang="en", scene_count=1, seconds_per_scene=5)
