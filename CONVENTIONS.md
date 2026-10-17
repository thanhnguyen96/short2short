# Coding Conventions — short2short

Every agent and every person writing code in this repo MUST follow this file.

## 1. Environment
- Python 3.13, managed with `uv` (`uv sync`, `uv run ...`). Do not use `pip install` directly.
- Primary target: Windows 11. Code must also run on Linux/macOS (use `pathlib`, never hardcode `/` or `\`).
- ffmpeg comes from PATH or the `ffmpeg_path` config. Never download binaries automatically.

## 2. Layout
```
src/short2short/        # package (src layout)
  core/                 # pipeline, script, voice, subtitle, compose, models
  providers/            # VideoProvider + adapters (stock, veo_api, flow)
  storage/              # task dir, manifest, cache
  config.py
  cli.py
tests/                  # mirrors src: tests/core/test_pipeline.py ...
```
- One module = one responsibility. Split files longer than 400 lines.
- No circular imports. `core` must not import concrete `providers.*` modules, only `providers.base`.

## 3. Style
- Format + lint: `ruff format` and `ruff check` (config in `pyproject.toml`, line-length 100).
- Type hints are required on every public function. `mypy --strict` on `src/` must pass.
- Naming: `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_SNAKE` for constants. No cryptic abbreviations.
- Data models use `pydantic.BaseModel` (config, manifest, I/O boundaries) or `@dataclass(frozen=True)` (internal values).
- Short docstrings (1–3 lines) on public functions, in English. Comments explain *why*, not *what*.
- No `print` in library code. Use `logging.getLogger(__name__)`. The CLI prints with `rich`.

## 4. Errors
- Custom exceptions inherit from `Short2ShortError` (`short2short/errors.py`). Providers raise `ProviderError` and its subclasses:
  `ProviderAuthError`, `ProviderPolicyError`, `ProviderQuotaError`, `ProviderTimeoutError`, `ProviderUnconfirmedJobError`.
- No `except Exception: pass`. Catch specific errors; if a broad catch is unavoidable, log and re-raise or convert to a domain error.
- When a provider reports a policy violation: raise `ProviderPolicyError` and mark the scene `failed_policy`. **Never** rewrite the prompt to get around the filter.

## 5. Security (mandatory)
- No `shell=True`, no `eval`/`exec`, no `pickle`. Subprocesses always use an argv list.
- Never log secrets: API keys, cookies, bearer tokens. Pass through `redact()` in `short2short/security.py` before logging/raising.
- Secrets are never written to disk in plaintext. Store them with `keyring` (Windows Credential Manager). TOML config holds only references or stays empty.
- Every file-write path must go through `safe_join(root, *parts)` to block path traversal.
- Servers/UIs (if any) bind to `127.0.0.1` by default.
- No humanizer (no fake delays, no mouse jitter), no proxy rotation, no captcha solving via external services, no prompt rewriting to evade filters.
- The `flow` adapter may drive the flow.google.com UI only with actions a real user performs: focusing the input, `Input.insertText`, the Enter key, `.click()` on New project and Approve. Coordinate-based clicks and anything that hides automation are forbidden.
- HTTP: always set `timeout=` and `allow_redirects=False` when downloading from provider-returned URLs, then check the host against an allowlist.

## 6. Providers
- Every adapter implements `VideoProvider` (`providers/base.py`): `submit`, `poll`, `download`, plus the `name` and `max_clip_seconds` attributes.
- Adapters know nothing about the pipeline/task. The pipeline knows nothing about HTTP details.
- Register adapters through `providers/registry.py` (dict name → factory). No scattered if/else on names.
- The `flow` adapter never calls Google's private REST endpoints. It never stores cookies or bearer tokens. CDP is loopback-only via `assert_loopback_host`.

## 7. Tests
- `pytest`. Every module has tests. Coverage target for `core/` ≥ 80%.
- Tests must NOT hit the real network, open Chrome, or run real ffmpeg, except tests marked `@pytest.mark.integration` (skipped by default).
- Mock HTTP with `responses` or a fake provider (`tests/fakes.py`).
- Use `tmp_path` for files. Never write outside the test directory.

## 8. Commits
- Conventional Commits: `feat(core): ...`, `fix(flow): ...`, `test: ...`, `docs: ...`, `chore: ...`.
- Every commit passes `ruff check`, `ruff format --check`, and `pytest`.

## 9. Definition of "done" for a task
1. Code + tests match the task spec.
2. `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest` all pass.
3. Only touch files within the task's scope. Do not modify files belonging to other tasks.
