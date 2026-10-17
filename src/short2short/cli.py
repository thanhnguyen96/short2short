from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import tomli_w
import typer
from rich.console import Console
from rich.table import Table

from short2short.config import AppConfig, get_secret, load_config, set_secret
from short2short.core.interfaces import ScriptGenerator
from short2short.core.models import Aspect, Budget, TaskManifest, TaskStatus
from short2short.core.script import FileScriptGenerator, OpenAIScriptGenerator
from short2short.core.voice import EdgeTTS, default_voice_for_lang
from short2short.errors import ConfigError, Short2ShortError
from short2short.providers import registry
from short2short.providers.base import VideoProvider
from short2short.providers.builtins import PROVIDER_INFO, register_builtins
from short2short.storage.cache import ClipCache
from short2short.storage.tasks import TaskStore

if TYPE_CHECKING:
    pipeline: Any
else:
    try:
        from short2short.core import pipeline
    except ImportError:  # pragma: no cover
        import types

        pipeline = types.ModuleType("short2short.core.pipeline")
        pipeline.run_task = None
        pipeline.resume_task = None
        pipeline.regen_scene = None
        sys.modules["short2short.core.pipeline"] = pipeline
        import short2short.core

        short2short.core.pipeline = pipeline

console = Console()
err_console = Console(stderr=True)

FLOW_TOS_TEXT: str = (
    "Google Flow adapter is unofficial and experimental.\n"
    "It drives your own logged-in browser session against a private UI "
    "that can change or break without notice.\n"
    "It may violate Google's Terms of Service. You are responsible for compliance.\n"
    "It only uses actions a user would take. It does not bypass safety filters, "
    "rewrite prompts, add humanizer delays or mouse jitter, rotate proxies, or solve captchas."
)

app = typer.Typer(
    name="short2short",
    help="short2short - Topic to short video CLI",
    no_args_is_help=True,
)
flow_app = typer.Typer(
    help="Google Flow management commands",
    no_args_is_help=True,
)
config_app = typer.Typer(
    help="Configuration management commands",
    no_args_is_help=True,
)

app.add_typer(flow_app, name="flow")
app.add_typer(config_app, name="config")


@app.callback()
def main_callback() -> None:
    """Initialize logging configuration for short2short CLI."""
    logging.basicConfig(level=logging.INFO)


@contextlib.contextmanager
def _cli_error_handler() -> Iterator[None]:
    """Catch domain errors and map to CLI exit codes."""
    try:
        yield
    except (typer.Exit, typer.Abort):
        raise
    except ConfigError as exc:
        err_console.print(f"Error: {exc}")
        raise typer.Exit(code=2) from None
    except Short2ShortError as exc:
        err_console.print(f"Error: {exc}")
        raise typer.Exit(code=1) from None
    except Exception as exc:
        err_console.print(f"Error: {exc}")
        raise typer.Exit(code=1) from None


def _build(config: AppConfig) -> tuple[TaskStore, ClipCache]:
    """Construct TaskStore and ClipCache from configuration."""
    store = TaskStore(config.tasks_dir)
    cache = ClipCache(config.cache_dir)
    return store, cache


def _script_generator(config: AppConfig, script_file: Path | None) -> ScriptGenerator:
    """Build script generator: FileScriptGenerator if script_file, else OpenAIScriptGenerator."""
    if script_file is not None:
        return FileScriptGenerator(script_file)
    api_key = get_secret("llm_api_key")
    return OpenAIScriptGenerator(
        base_url=config.llm_base_url,
        api_key=api_key,
        model=config.llm_model,
    )


def _provider(name: str, config: AppConfig) -> VideoProvider:
    """Resolve and instantiate video provider by name."""
    register_builtins()
    return registry.get(name, config)


def _print_result(manifest: TaskManifest, store: TaskStore) -> None:
    """Print task completion or pause state."""
    if manifest.status == TaskStatus.PAUSED:
        console.print(f"Task paused: {manifest.stop_reason}")
        console.print(f"Resume with: short2short resume {manifest.task_id}")
    elif manifest.status == TaskStatus.COMPLETED:
        final_path = store.task_dir(manifest.task_id) / (manifest.final_relpath or "final.mp4")
        console.print(f"Task ID: {manifest.task_id}")
        console.print(f"Final video: {final_path}")
    elif manifest.status == TaskStatus.FAILED:
        err_console.print(f"Task failed: {manifest.error}")
        raise typer.Exit(code=1)


@app.command(name="run")
def run(
    topic: Annotated[str, typer.Argument(help="Video topic")],
    provider: Annotated[
        str, typer.Option("--provider", help="Video provider (fake, stock, veo_api, flow)")
    ] = "fake",
    aspect: Annotated[
        str, typer.Option("--aspect", help="Aspect ratio (9:16, 16:9, 1:1)")
    ] = "9:16",
    lang: Annotated[str, typer.Option("--lang", help="Narration language code")] = "vi",
    scenes: Annotated[int | None, typer.Option("--scenes", help="Scene count")] = None,
    seed: Annotated[int | None, typer.Option("--seed", help="Random seed integer")] = None,
    voice: Annotated[str | None, typer.Option("--voice", help="TTS voice name")] = None,
    bgm: Annotated[Path | None, typer.Option("--bgm", help="Path to background music file")] = None,
    max_clips: Annotated[
        int | None, typer.Option("--max-clips", help="Maximum video clips allowed")
    ] = None,
    max_total_seconds: Annotated[
        float | None,
        typer.Option("--max-total-seconds", help="Maximum total video duration in seconds"),
    ] = None,
    fit: Annotated[str | None, typer.Option("--fit", help="Fit mode (cover, contain)")] = None,
    script_file: Annotated[
        Path | None, typer.Option("--script-file", help="Path to offline script JSON file")
    ] = None,
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to config.toml")
    ] = None,
) -> None:
    """Generate a short video from topic."""
    with _cli_error_handler():
        try:
            aspect_enum = Aspect(aspect)
        except ValueError:
            err_console.print(
                f"Error: Invalid aspect ratio: {aspect!r}. Must be 9:16, 16:9, or 1:1"
            )
            raise typer.Exit(code=2) from None

        if fit is not None and fit not in ("cover", "contain"):
            err_console.print(f"Error: Invalid fit mode: {fit!r}. Must be 'cover' or 'contain'")
            raise typer.Exit(code=2)

        if bgm is not None and not bgm.is_file():
            err_console.print(f"Error: BGM file not found: {bgm}")
            raise typer.Exit(code=2)

        if script_file is not None and not script_file.is_file():
            err_console.print(f"Error: Script file not found: {script_file}")
            raise typer.Exit(code=2)

        cfg = load_config(config)
        store, cache = _build(cfg)
        prov = _provider(provider, cfg)
        script_gen = _script_generator(cfg, script_file)
        selected_voice = voice if voice is not None else default_voice_for_lang(lang, cfg.tts_voice)
        scene_count = scenes if scenes is not None else cfg.scene_count
        clips_limit = max_clips if max_clips is not None else cfg.max_clips
        seconds_limit = (
            max_total_seconds if max_total_seconds is not None else cfg.max_total_seconds
        )
        budget = Budget(max_clips=clips_limit, max_total_seconds=seconds_limit)
        fit_mode = fit if fit is not None else cfg.fit_mode
        tts = EdgeTTS()

        manifest = pipeline.run_task(
            config=cfg,
            store=store,
            cache=cache,
            provider=prov,
            script_generator=script_gen,
            tts=tts,
            topic=topic,
            aspect=aspect_enum,
            lang=lang,
            scene_count=scene_count,
            seed=seed,
            voice=selected_voice,
            bgm_path=str(bgm.resolve()) if bgm else None,
            budget=budget,
            fit_mode=fit_mode,
        )
        _print_result(manifest, store)


@app.command(name="resume")
def resume(
    task_id: Annotated[str, typer.Argument(help="Task ID to resume")],
    provider: Annotated[
        str | None, typer.Option("--provider", help="Video provider override")
    ] = None,
    script_file: Annotated[
        Path | None, typer.Option("--script-file", help="Path to offline script JSON file")
    ] = None,
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to config.toml")
    ] = None,
) -> None:
    """Resume an existing paused or incomplete task."""
    with _cli_error_handler():
        if script_file is not None and not script_file.is_file():
            err_console.print(f"Error: Script file not found: {script_file}")
            raise typer.Exit(code=2)

        cfg = load_config(config)
        store, cache = _build(cfg)
        manifest = store.load(task_id)
        prov_name = provider if provider is not None else manifest.provider
        prov = _provider(prov_name, cfg)
        script_gen = _script_generator(cfg, script_file)
        tts = EdgeTTS()

        res = pipeline.resume_task(
            task_id,
            config=cfg,
            store=store,
            cache=cache,
            provider=prov,
            script_generator=script_gen,
            tts=tts,
        )
        _print_result(res, store)


@app.command(name="regen-scene")
def regen_scene(
    task_id: Annotated[str, typer.Argument(help="Task ID")],
    index: Annotated[int, typer.Argument(help="0-based scene index to regenerate")],
    prompt: Annotated[
        str | None, typer.Option("--prompt", help="New visual prompt override")
    ] = None,
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to config.toml")
    ] = None,
) -> None:
    """Regenerate a single scene within a task."""
    with _cli_error_handler():
        cfg = load_config(config)
        store, cache = _build(cfg)
        manifest = store.load(task_id)
        prov = _provider(manifest.provider, cfg)
        tts = EdgeTTS()

        res = pipeline.regen_scene(
            task_id,
            index,
            config=cfg,
            store=store,
            cache=cache,
            provider=prov,
            tts=tts,
            prompt=prompt,
        )
        _print_result(res, store)


@app.command(name="providers")
def list_providers() -> None:
    """List available video generation providers."""
    with _cli_error_handler():
        table = Table(title="Available Video Providers")
        table.add_column("Provider", style="bold")
        table.add_column("Paid", justify="center")
        table.add_column("Max Clip (s)", justify="right")
        for name, (paid, max_clip) in PROVIDER_INFO.items():
            table.add_row(name, "Yes" if paid else "No", str(max_clip))
        console.print(table)


@app.command(name="tasks")
def list_tasks(
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to config.toml")
    ] = None,
) -> None:
    """List stored video generation tasks."""
    with _cli_error_handler():
        cfg = load_config(config)
        store, _ = _build(cfg)
        ids = store.list_ids()
        if not ids:
            console.print("No tasks found.")
            return

        table = Table(title="Tasks")
        table.add_column("Task ID", style="bold", no_wrap=True)
        table.add_column("Topic")
        table.add_column("Provider")
        table.add_column("Status")
        table.add_column("Stage")
        table.add_column("Updated (UTC)")

        for tid in ids:
            try:
                m = store.load(tid)
                updated = m.updated_at.strftime("%Y-%m-%d %H:%M:%S")
                table.add_row(
                    m.task_id, m.topic, m.provider, m.status.value, m.stage.value, updated
                )
            except Exception:
                table.add_row(tid, "?", "?", "?", "?", "?")
        console.print(table)


@flow_app.command(name="login")
def flow_login(
    i_understand: Annotated[
        bool,
        typer.Option(
            "--i-understand",
            help="Acknowledge understanding of Google Flow Terms of Service",
        ),
    ] = False,
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to config.toml")
    ] = None,
) -> None:
    """Authenticate with Google Flow via Chrome browser session."""
    with _cli_error_handler():
        err_console.print(FLOW_TOS_TEXT)
        if not i_understand:
            err_console.print("\nError: Must specify --i-understand to proceed with flow login")
            raise typer.Exit(code=2)
        cfg = load_config(config)
        from short2short.providers.flow import auth as flow_auth

        flow_auth.login(cfg)
        console.print("Successfully logged into Google Flow.")


@flow_app.command(name="status")
def flow_status(
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to config.toml")
    ] = None,
) -> None:
    """Check Google Flow authentication and session status."""
    with _cli_error_handler():
        cfg = load_config(config)
        from short2short.providers.flow import auth as flow_auth

        if flow_auth.status(cfg):
            console.print("Google Flow: logged in (CDP reachable, composer visible)")
        else:
            console.print("Google Flow: not logged in")


@flow_app.command(name="doctor")
def flow_doctor(
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to config.toml")
    ] = None,
) -> None:
    """Check CDP, login, composer, and selectors. Does not type a prompt or spend credits."""
    with _cli_error_handler():
        cfg = load_config(config)
        from short2short.providers.flow import auth as flow_auth

        reports = flow_auth.doctor(cfg)
        for r in reports:
            tag = "[ok]" if r.ok else "[fail]"
            console.print(f"{tag} {r.name}: {r.detail}", markup=False, highlight=False)
        if not all(r.ok for r in reports):
            raise typer.Exit(code=1)


@flow_app.command(name="logout")
def flow_logout(
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to config.toml")
    ] = None,
) -> None:
    """Log out from Google Flow and clear stored credentials."""
    with _cli_error_handler():
        cfg = load_config(config)
        from short2short.providers.flow import auth as flow_auth

        flow_auth.logout(cfg)
        console.print("Logged out from Google Flow.")


@config_app.command(name="show")
def config_show(
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to config.toml")
    ] = None,
) -> None:
    """Display active application configuration."""
    with _cli_error_handler():
        cfg = load_config(config)
        data = cfg.model_dump(mode="json")
        console.print(tomli_w.dumps(data).strip())


@config_app.command(name="set-secret")
def config_set_secret(
    name: Annotated[str, typer.Argument(help="Secret name to store in keyring")],
) -> None:
    """Store a secret credential in keyring from standard input."""
    with _cli_error_handler():
        val = sys.stdin.read().strip()
        if not val:
            err_console.print("Error: Secret value cannot be empty")
            raise typer.Exit(code=2)
        set_secret(name, val)
        console.print(f"stored {name}")
