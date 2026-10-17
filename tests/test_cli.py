from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from short2short.cli import app
from short2short.core.models import Aspect, Stage, TaskManifest, TaskStatus
from short2short.errors import ConfigError, PipelineError, ProviderAuthError

runner = CliRunner()


def _make_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    tasks_dir = (tmp_path / "tasks").as_posix()
    cache_dir = (tmp_path / "cache").as_posix()
    cfg.write_text(f'tasks_dir = "{tasks_dir}"\ncache_dir = "{cache_dir}"\n', encoding="utf-8")
    return cfg


def _make_manifest(
    task_id: str = "20261017-120000-abcdef",
    status: TaskStatus = TaskStatus.COMPLETED,
    stop_reason: str | None = None,
    final_relpath: str | None = "final.mp4",
) -> TaskManifest:
    now = datetime.now(UTC)
    return TaskManifest(
        task_id=task_id,
        topic="Test topic",
        provider="fake",
        aspect=Aspect.PORTRAIT,
        lang="vi",
        seed=123,
        voice="vi-VN-HoaiMyNeural",
        status=status,
        stage=Stage.DONE if status == TaskStatus.COMPLETED else Stage.CLIPS,
        created_at=now,
        updated_at=now,
        stop_reason=stop_reason,
        final_relpath=final_relpath,
    )


def test_cli_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.stdout
    assert "resume" in result.stdout
    assert "regen-scene" in result.stdout
    assert "providers" in result.stdout
    assert "tasks" in result.stdout
    assert "flow" in result.stdout
    assert "config" in result.stdout


def test_providers_command() -> None:
    result = runner.invoke(app, ["providers"])
    assert result.exit_code == 0
    assert "fake" in result.stdout
    assert "stock" in result.stdout
    assert "veo_api" in result.stdout
    assert "flow" in result.stdout


def test_config_show_command(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    result = runner.invoke(app, ["config", "show", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "tasks_dir" in result.stdout
    assert "cache_dir" in result.stdout


def test_config_set_secret_success(monkeypatch: pytest.MonkeyPatch) -> None:
    stored: dict[str, str] = {}

    def fake_set_secret(name: str, value: str) -> None:
        stored[name] = value

    monkeypatch.setattr("short2short.cli.set_secret", fake_set_secret)
    result = runner.invoke(
        app, ["config", "set-secret", "llm_api_key"], input="sk-test-secret-123\n"
    )
    assert result.exit_code == 0
    assert "stored llm_api_key" in result.stdout
    assert stored.get("llm_api_key") == "sk-test-secret-123"


def test_config_set_secret_empty() -> None:
    result = runner.invoke(app, ["config", "set-secret", "llm_api_key"], input="   \n")
    assert result.exit_code == 2


def test_config_set_secret_unknown_name() -> None:
    result = runner.invoke(app, ["config", "set-secret", "invalid_name"], input="some_value\n")
    assert result.exit_code == 2


def test_flow_login_missing_i_understand() -> None:
    result = runner.invoke(app, ["flow", "login"])
    assert result.exit_code == 2
    assert "Google Flow adapter is unofficial and experimental" in result.output
    assert "--i-understand" in result.output


def test_flow_login_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    called = False

    def fake_login(config: Any) -> str:
        nonlocal called
        called = True
        return "2026-10-18T00:00:00Z"

    import short2short.providers.flow.auth as flow_auth

    monkeypatch.setattr(flow_auth, "login", fake_login)
    result = runner.invoke(app, ["flow", "login", "--i-understand", "--config", str(cfg)])
    assert result.exit_code == 0
    assert called is True
    assert "Successfully logged into Google Flow" in result.output
    assert "Google Flow adapter is unofficial and experimental" in result.output


def test_flow_login_auth_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)

    def fake_login(config: Any) -> str:
        raise ProviderAuthError("Chrome not found", provider="flow")

    import short2short.providers.flow.auth as flow_auth

    monkeypatch.setattr(flow_auth, "login", fake_login)
    result = runner.invoke(app, ["flow", "login", "--i-understand", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "Chrome not found" in result.output


def test_flow_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    import short2short.providers.flow.auth as flow_auth

    monkeypatch.setattr(flow_auth, "status", lambda c: True)
    res_true = runner.invoke(app, ["flow", "status", "--config", str(cfg)])
    assert res_true.exit_code == 0
    assert "logged in" in res_true.output

    monkeypatch.setattr(flow_auth, "status", lambda c: False)
    res_false = runner.invoke(app, ["flow", "status", "--config", str(cfg)])
    assert res_false.exit_code == 0
    assert "not logged in" in res_false.output


def test_flow_logout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    called = False

    def fake_logout(config: Any) -> None:
        nonlocal called
        called = True

    import short2short.providers.flow.auth as flow_auth

    monkeypatch.setattr(flow_auth, "logout", fake_logout)
    res = runner.invoke(app, ["flow", "logout", "--config", str(cfg)])
    assert res.exit_code == 0
    assert called is True
    assert "Logged out" in res.output


def test_tasks_empty(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    res = runner.invoke(app, ["tasks", "--config", str(cfg)])
    assert res.exit_code == 0
    assert "No tasks found" in res.output


def test_tasks_list(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tasks_dir = tmp_path / "tasks"
    tdir = tasks_dir / "20261017-120000-abcdef"
    tdir.mkdir(parents=True, exist_ok=True)
    manifest = _make_manifest()
    (tdir / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")

    res = runner.invoke(app, ["tasks", "--config", str(cfg)])
    assert res.exit_code == 0
    assert "20261017-120000-abcdef" in res.output
    assert "fake" in res.output


def test_run_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    m = _make_manifest()
    captured_kwargs: dict[str, Any] = {}

    def fake_run_task(**kwargs: Any) -> TaskManifest:
        captured_kwargs.update(kwargs)
        return m

    monkeypatch.setattr("short2short.core.pipeline.run_task", fake_run_task, raising=False)
    monkeypatch.setattr("short2short.cli.get_secret", lambda name: "sk-mock-key")

    res = runner.invoke(
        app,
        [
            "run",
            "History of coffee",
            "--provider",
            "fake",
            "--aspect",
            "16:9",
            "--lang",
            "en",
            "--scenes",
            "3",
            "--seed",
            "42",
            "--max-clips",
            "5",
            "--max-total-seconds",
            "60.0",
            "--fit",
            "contain",
            "--config",
            str(cfg),
        ],
    )
    assert res.exit_code == 0
    assert m.task_id in res.output
    assert "Final video:" in res.output
    assert captured_kwargs["topic"] == "History of coffee"
    assert captured_kwargs["aspect"] == Aspect.LANDSCAPE
    assert captured_kwargs["lang"] == "en"
    assert captured_kwargs["scene_count"] == 3
    assert captured_kwargs["seed"] == 42
    assert captured_kwargs["budget"].max_clips == 5
    assert captured_kwargs["budget"].max_total_seconds == 60.0
    assert captured_kwargs["fit_mode"] == "contain"


def test_run_paused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    m = _make_manifest(status=TaskStatus.PAUSED, stop_reason="quota")

    monkeypatch.setattr("short2short.core.pipeline.run_task", lambda **kwargs: m, raising=False)
    monkeypatch.setattr("short2short.cli.get_secret", lambda name: "sk-mock-key")

    res = runner.invoke(app, ["run", "Coffee", "--config", str(cfg)])
    assert res.exit_code == 0
    assert "Task paused: quota" in res.output
    assert f"short2short resume {m.task_id}" in res.output


def test_run_invalid_aspect(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    res = runner.invoke(app, ["run", "Coffee", "--aspect", "4:3", "--config", str(cfg)])
    assert res.exit_code == 2
    assert "Invalid aspect ratio" in res.output


def test_run_invalid_fit(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    res = runner.invoke(app, ["run", "Coffee", "--fit", "stretch", "--config", str(cfg)])
    assert res.exit_code == 2
    assert "Invalid fit mode" in res.output


def test_run_missing_bgm(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    res = runner.invoke(
        app, ["run", "Coffee", "--bgm", str(tmp_path / "nonexistent.mp3"), "--config", str(cfg)]
    )
    assert res.exit_code == 2
    assert "BGM file not found" in res.output


def test_run_missing_script_file(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    res = runner.invoke(
        app,
        [
            "run",
            "Coffee",
            "--script-file",
            str(tmp_path / "nonexistent.json"),
            "--config",
            str(cfg),
        ],
    )
    assert res.exit_code == 2
    assert "Script file not found" in res.output


def test_run_with_script_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    script_p = tmp_path / "script.json"
    script_p.write_text('{"scenes":[{"narration":"n1","visual_prompt":"v1"}]}', encoding="utf-8")
    m = _make_manifest()
    captured_kwargs: dict[str, Any] = {}

    def fake_run_task(**kwargs: Any) -> TaskManifest:
        captured_kwargs.update(kwargs)
        return m

    monkeypatch.setattr("short2short.core.pipeline.run_task", fake_run_task, raising=False)

    # Note: get_secret should NOT be called when script_file is given
    def fail_get_secret(name: str) -> str:
        raise AssertionError("get_secret was unexpectedly called")

    monkeypatch.setattr("short2short.cli.get_secret", fail_get_secret)

    res = runner.invoke(
        app, ["run", "Coffee", "--script-file", str(script_p), "--config", str(cfg)]
    )
    assert res.exit_code == 0
    assert captured_kwargs["script_generator"].__class__.__name__ == "FileScriptGenerator"


def test_run_missing_secret_fails_exit_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)

    def raise_config_error(name: str) -> str:
        raise ConfigError(f"Missing secret {name!r}")

    monkeypatch.setattr("short2short.cli.get_secret", raise_config_error)
    res = runner.invoke(app, ["run", "Coffee", "--config", str(cfg)])
    assert res.exit_code == 2
    assert "Missing secret 'llm_api_key'" in res.output


def test_run_pipeline_error_fails_exit_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)

    def fake_run_task(**kwargs: Any) -> TaskManifest:
        raise PipelineError("Pipeline failed in stage clips")

    monkeypatch.setattr("short2short.core.pipeline.run_task", fake_run_task, raising=False)
    monkeypatch.setattr("short2short.cli.get_secret", lambda name: "sk-mock-key")

    res = runner.invoke(app, ["run", "Coffee", "--config", str(cfg)])
    assert res.exit_code == 1
    assert "Pipeline failed in stage clips" in res.output


def test_resume_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tasks_dir = tmp_path / "tasks"
    tid = "20261017-120000-abcdef"
    tdir = tasks_dir / tid
    tdir.mkdir(parents=True, exist_ok=True)
    m = _make_manifest(task_id=tid, status=TaskStatus.PAUSED)
    (tdir / "manifest.json").write_text(m.model_dump_json(), encoding="utf-8")

    completed_m = _make_manifest(task_id=tid, status=TaskStatus.COMPLETED)
    captured_kwargs: dict[str, Any] = {}

    def fake_resume(task_id: str, **kwargs: Any) -> TaskManifest:
        captured_kwargs["task_id"] = task_id
        captured_kwargs.update(kwargs)
        return completed_m

    monkeypatch.setattr("short2short.core.pipeline.resume_task", fake_resume, raising=False)
    monkeypatch.setattr("short2short.cli.get_secret", lambda name: "sk-mock-key")

    res = runner.invoke(app, ["resume", tid, "--config", str(cfg)])
    assert res.exit_code == 0
    assert captured_kwargs["task_id"] == tid
    assert captured_kwargs["provider"].name == "fake"
    assert "Final video:" in res.output


def test_resume_provider_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tasks_dir = tmp_path / "tasks"
    tid = "20261017-120000-abcdef"
    tdir = tasks_dir / tid
    tdir.mkdir(parents=True, exist_ok=True)
    m = _make_manifest(task_id=tid, status=TaskStatus.PAUSED)
    (tdir / "manifest.json").write_text(m.model_dump_json(), encoding="utf-8")

    captured_provider_name: str | None = None

    def fake_resume(task_id: str, **kwargs: Any) -> TaskManifest:
        nonlocal captured_provider_name
        captured_provider_name = kwargs["provider"].name
        return _make_manifest(task_id=tid, status=TaskStatus.COMPLETED)

    monkeypatch.setattr("short2short.core.pipeline.resume_task", fake_resume, raising=False)
    monkeypatch.setattr("short2short.cli.get_secret", lambda name: "sk-mock-key")

    res = runner.invoke(app, ["resume", tid, "--provider", "fake", "--config", str(cfg)])
    assert res.exit_code == 0
    assert captured_provider_name == "fake"


def test_resume_nonexistent_task(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    res = runner.invoke(app, ["resume", "nonexistent-id", "--config", str(cfg)])
    assert res.exit_code == 1
    assert "Task manifest not found" in res.output


def test_regen_scene_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    tasks_dir = tmp_path / "tasks"
    tid = "20261017-120000-abcdef"
    tdir = tasks_dir / tid
    tdir.mkdir(parents=True, exist_ok=True)
    m = _make_manifest(task_id=tid, status=TaskStatus.COMPLETED)
    (tdir / "manifest.json").write_text(m.model_dump_json(), encoding="utf-8")

    captured_kwargs: dict[str, Any] = {}

    def fake_regen(task_id: str, index: int, **kwargs: Any) -> TaskManifest:
        captured_kwargs["task_id"] = task_id
        captured_kwargs["index"] = index
        captured_kwargs.update(kwargs)
        return m

    monkeypatch.setattr("short2short.core.pipeline.regen_scene", fake_regen, raising=False)

    res = runner.invoke(
        app,
        ["regen-scene", tid, "1", "--prompt", "New cinematic shot", "--config", str(cfg)],
    )
    assert res.exit_code == 0
    assert captured_kwargs["task_id"] == tid
    assert captured_kwargs["index"] == 1
    assert captured_kwargs["prompt"] == "New cinematic shot"
    assert "Final video:" in res.output


def test_regen_scene_nonexistent_task(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    res = runner.invoke(app, ["regen-scene", "nonexistent-id", "0", "--config", str(cfg)])
    assert res.exit_code == 1
    assert "Task manifest not found" in res.output


def test_flow_doctor_all_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    import short2short.providers.flow.auth as flow_auth

    calls: list[str] = []

    def fake_doctor(c: Any) -> list[flow_auth.DoctorReport]:
        calls.append("doctor")
        return [
            flow_auth.DoctorReport("cdp_port", True, "port 9222"),
            flow_auth.DoctorReport("logged_in", True, "composer visible"),
        ]

    monkeypatch.setattr(flow_auth, "doctor", fake_doctor)
    res = runner.invoke(app, ["flow", "doctor", "--config", str(cfg)])
    assert res.exit_code == 0
    assert "[ok] cdp_port" in res.output
    assert calls == ["doctor"]


def test_flow_doctor_failure_exit_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    import short2short.providers.flow.auth as flow_auth

    monkeypatch.setattr(
        flow_auth,
        "doctor",
        lambda c: [
            flow_auth.DoctorReport("cdp_port", True, "port 9222"),
            flow_auth.DoctorReport("logged_in", False, "composer missing host=x"),
        ],
    )
    res = runner.invoke(app, ["flow", "doctor", "--config", str(cfg)])
    assert res.exit_code == 1
    assert "[fail] logged_in" in res.output
