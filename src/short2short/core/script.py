from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import openai

from short2short.core.models import Scene, Script
from short2short.errors import PipelineError
from short2short.security import redact

logger = logging.getLogger(__name__)


def _strip_code_fence(text: str) -> str:
    """Strip one outermost markdown code fence if present."""
    s = text.strip()
    if s.startswith("```"):
        pattern = r"^```(?:[a-zA-Z0-9_-]+)?\s*\r?\n?(.*?)\r?\n?```\s*$"
        match = re.match(pattern, s, re.DOTALL)
        if match:
            return match.group(1).strip()
    return s


def _parse_and_validate_scenes(
    raw_scenes: Any, *, expected_count: int | None = None
) -> list[Scene]:
    """Validate raw scenes list and reindex sequentially."""
    if not isinstance(raw_scenes, list):
        raise ValueError("Field 'scenes' must be a list")
    if not raw_scenes:
        raise ValueError("Field 'scenes' must contain at least one scene")
    if expected_count is not None and len(raw_scenes) != expected_count:
        raise ValueError(f"Scene count mismatch: expected {expected_count}, got {len(raw_scenes)}")

    scenes: list[Scene] = []
    for i, item in enumerate(raw_scenes):
        if not isinstance(item, dict):
            raise ValueError(f"Scene at index {i} must be a dictionary")
        narration = item.get("narration")
        visual_prompt = item.get("visual_prompt")
        if not isinstance(narration, str) or not narration.strip():
            raise ValueError(f"Scene at index {i} requires non-empty string 'narration'")
        if not isinstance(visual_prompt, str) or not visual_prompt.strip():
            raise ValueError(f"Scene at index {i} requires non-empty string 'visual_prompt'")

        scenes.append(
            Scene(
                index=i,
                narration=narration.strip(),
                visual_prompt=visual_prompt.strip(),
            )
        )
    return scenes


class OpenAIScriptGenerator:
    """Generate video scripts using OpenAI-compatible chat completion APIs."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        client: Any | None = None,
    ) -> None:
        """Initialize generator with API credentials and model configuration."""
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        if client is None:
            self.client: Any = openai.OpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                max_retries=0,
            )
        else:
            self.client = client

    def __repr__(self) -> str:
        """Safe representation hiding api_key."""
        return f"OpenAIScriptGenerator(model={self.model!r}, base_url={self.base_url!r})"

    def generate(
        self, topic: str, *, lang: str, scene_count: int, seconds_per_scene: int
    ) -> Script:
        """Generate a validated Script with the requested scene count and language."""
        system_prompt = (
            "You write a short video plan. Return only a JSON object, no markdown.\n"
            'Schema: {"scenes":[{"narration":"string","visual_prompt":"string"}]}\n'
            "Rules:\n"
            f"- Produce exactly {scene_count} scenes.\n"
            f"- narration is spoken aloud in {lang}. One or two sentences. No stage directions.\n"
            "- visual_prompt is English only. Concrete camera-visible action. No on-screen text.\n"
            f"- Each scene should support about {seconds_per_scene} seconds of speech.\n"
            "- Do not include labels, hashtags, or shot numbers inside the strings."
        )
        user_prompt = f"Topic: {topic}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.7,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                raise PipelineError(redact(f"LLM request failed: {exc}")) from exc

            content: str | None = None
            if hasattr(response, "choices") and response.choices:
                choice = response.choices[0]
                if hasattr(choice, "message") and hasattr(choice.message, "content"):
                    content = choice.message.content

            try:
                if not content:
                    raise ValueError("LLM returned empty response content")

                cleaned = _strip_code_fence(content)
                data = json.loads(cleaned)
                if not isinstance(data, dict):
                    raise ValueError(f"Expected JSON object, got {type(data).__name__}")
                if "scenes" not in data:
                    raise ValueError("Missing 'scenes' key in JSON response")

                scenes = _parse_and_validate_scenes(data["scenes"], expected_count=scene_count)
                return Script(topic=topic, lang=lang, scenes=scenes)
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                logger.warning(
                    "Script generation parse/validation failed (attempt %d/2): %s",
                    attempt,
                    redact(str(exc)),
                )
                if attempt == 2:
                    raise PipelineError(
                        redact(f"Script generation failed validation: {exc}")
                    ) from exc

        raise PipelineError(redact(f"Script generation failed: {last_error}"))


class FileScriptGenerator:
    """Load pre-authored video script from a local JSON file."""

    def __init__(self, path: Path | str) -> None:
        """Initialize generator with file path."""
        self.path = Path(path)

    def generate(
        self, topic: str, *, lang: str, scene_count: int, seconds_per_scene: int
    ) -> Script:
        """Load JSON script file, ignore scene_count, reindex scenes, and return Script."""
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise PipelineError(f"Script file not found: {self.path}") from exc
        except OSError as exc:
            raise PipelineError(f"Cannot read script file {self.path}: {exc}") from exc

        try:
            cleaned = _strip_code_fence(content)
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise PipelineError(f"Invalid JSON in script file {self.path}: {exc}") from exc

        if not isinstance(data, dict):
            raise PipelineError(
                f"Script file {self.path} must contain a JSON object, got {type(data).__name__}"
            )
        if "scenes" not in data:
            raise PipelineError(f"Script file {self.path} missing 'scenes' key")

        try:
            scenes = _parse_and_validate_scenes(data["scenes"], expected_count=None)
        except ValueError as exc:
            raise PipelineError(f"Invalid scenes in script file {self.path}: {exc}") from exc

        return Script(topic=topic, lang=lang, scenes=scenes)
