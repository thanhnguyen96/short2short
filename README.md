# short2short

**Short idea in, short video out.**

short2short turns a one-line topic into a finished vertical short video: script, narration, subtitles, one AI (or stock) clip per scene, all composed with ffmpeg. It combines the scripting/composition approach of [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) with clip generation from Google Veo — either via the official API or by driving your own logged-in Google Flow session.

Best suited for narrated shorts of 15–90 s: event intros, ads, explainers, place showcases.

## How it works

```
topic ──► script ──► voice ──► subtitle ──► clips ──► compose ──► final.mp4
          (LLM)     (edge-tts)  (.srt)    (provider)  (ffmpeg)
```

1. **Script** — an OpenAI-compatible LLM splits the topic into scenes, each with a `narration` line and a `visual_prompt`. You can skip the LLM with `--script-file`.
2. **Voice** — `edge-tts` reads the narration; word timings are kept.
3. **Subtitle** — an `.srt` is built from the TTS timings.
4. **Clips** — one clip per scene from the chosen provider, generated in parallel (bounded by `max_parallel_jobs`) and cached by prompt.
5. **Compose** — clips are fitted to the aspect ratio (`cover`/`contain`), cut to the narration length of their scene, concatenated, then mixed with voice, optional BGM, and burned-in subtitles.

The final length follows the narration, not the clip length: a 10 s clip under 6 s of narration is trimmed to 6 s. Write longer narration for a longer video.

Every run is a **task** with a `manifest.json` that records each stage and scene. Interrupted or failed tasks can be resumed, and single scenes regenerated, without paying for clips that already exist.

### Providers

| Name | Source | Needs |
|---|---|---|
| `fake` | Solid-color clips generated locally by ffmpeg | nothing (offline testing) |
| `stock` | Pexels stock videos | `pexels_api_key` |
| `veo_api` | Google Veo via the official GenAI API | `veo_api_key` |
| `flow` | Google Flow, driven through your own Chrome over CDP | a Google account with Flow credits |

> **Google Flow notice.** The `flow` adapter is unofficial and experimental. It drives your own logged-in browser against a private UI that can change without notice, and it may violate Google's Terms of Service — you are responsible for compliance. It only performs actions a user would (focus the composer, type, press Enter, click New project / Approve). It does not bypass safety filters, rewrite prompts, add humanizer delays or mouse jitter, rotate proxies, or solve captchas. Each clip spends real Flow credits; `flow_max_credits_per_clip` caps the cost the adapter will approve.

## Project layout

```
short2short/
├── src/short2short/
│   ├── cli.py              # Typer CLI: run, resume, regen-scene, tasks, flow, config
│   ├── config.py           # AppConfig (TOML) + keyring secrets
│   ├── errors.py           # Short2ShortError hierarchy, ProviderError subclasses
│   ├── security.py         # redact(), safe_join()
│   ├── ffmpeg.py           # ffmpeg/ffprobe runners (argv only, no shell)
│   ├── net.py              # host-allowlisted downloads
│   ├── core/
│   │   ├── models.py       # Scene, Script, TaskManifest, Stage, ...
│   │   ├── script.py       # LLM + file script generators
│   │   ├── voice.py        # edge-tts narration
│   │   ├── subtitle.py     # SRT writer
│   │   ├── compose.py      # ffmpeg composition
│   │   └── pipeline.py     # stage orchestration, budget, resume
│   ├── providers/
│   │   ├── base.py         # VideoProvider protocol: submit / poll / download
│   │   ├── registry.py     # name → factory
│   │   ├── builtins.py     # registers fake, stock, veo_api, flow
│   │   ├── fake.py  stock.py  veo_api.py
│   │   └── flow/           # CDP-driven Google Flow adapter (auth, ui, ui_js, parse, provider)
│   └── storage/            # task store, clip cache
├── tests/                  # mirrors src/; no network, Chrome, or real ffmpeg by default
├── CONVENTIONS.md          # coding rules (required reading for contributors)
└── AGENTS.md               # entry point for AI coding agents
```

Runtime data lives in `~/.short2short/`: `config.toml`, `tasks/<task-id>/` (manifest, voice, srt, clips, `final.mp4`), `cache/`, and `chrome-profile/` for the `flow` adapter.

## Prerequisites

- **Python 3.13** and **[uv](https://docs.astral.sh/uv/)**.
- **ffmpeg + ffprobe** on `PATH` (v8 recommended), or set `ffmpeg_path` in the config. Windows: `winget install Gyan.FFmpeg`.
- Depending on what you use:
  - **LLM key** (`llm_api_key`) for script generation — any OpenAI-compatible endpoint (`llm_base_url`, `llm_model`). Not needed with `--script-file`.
  - **Pexels key** for `stock`, **Veo API key** for `veo_api`.
  - **Google Chrome** and a Google account with Flow access for `flow`.

### Install

```bash
git clone git@github.com:thanhnguyen96/short2short.git
cd short2short
uv sync
uv run short2short --help
```

### Secrets

Secrets go to the OS keyring (Windows Credential Manager / macOS Keychain / Secret Service), never into `config.toml`. The value is read from stdin, not argv:

```bash
uv run short2short config set-secret llm_api_key
uv run short2short config set-secret pexels_api_key
uv run short2short config set-secret veo_api_key
```

On headless machines without a keyring, use environment variables instead: `SHORT2SHORT_LLM_API_KEY`, `SHORT2SHORT_PEXELS_API_KEY`, `SHORT2SHORT_VEO_API_KEY`.

### Config

Optional `~/.short2short/config.toml` (all keys have defaults; see `short2short config show`):

```toml
llm_base_url = "https://api.openai.com/v1"
llm_model = "gpt-4o-mini"
tts_voice = "vi-VN-HoaiMyNeural"
scene_count = 5
max_parallel_jobs = 2
max_total_seconds = 120.0
flow_max_credits_per_clip = 20
```

### Flow setup (only for `--provider flow`)

```bash
uv run short2short flow login --i-understand   # opens a dedicated Chrome profile; sign in to Flow by hand
uv run short2short flow status                 # is the CDP session alive and logged in?
uv run short2short flow doctor                 # checks page, composer, selectors — spends 0 credits
```

Keep that Chrome window open while generating. The adapter talks to it on loopback only and never stores cookies or tokens.

## Usage

Offline smoke test (no keys, no credits):

```bash
uv run short2short run "Sunrise over Ha Long Bay" --provider fake --scenes 3
```

Full run with an LLM-written script and Flow clips:

```bash
uv run short2short run "intro cho 1 buổi drone biểu diễn tại Đà Nẵng" \
  --provider flow --lang vi --scenes 5 --aspect 9:16
```

Use your own script and stock footage, with background music:

```bash
uv run short2short run "Da Nang drone show" --provider stock \
  --script-file script.json --bgm music.mp3 --aspect 16:9
```

`script.json`:

```json
{
  "scenes": [
    {
      "narration": "Dusk falls over the Han River as the city waits.",
      "visual_prompt": "Cinematic wide shot of the Han River in Da Nang at dusk, city lights reflecting"
    },
    {
      "narration": "Hundreds of drones rise into the night sky.",
      "visual_prompt": "Hundreds of glowing drones lifting off over the river, slow upward camera tilt"
    }
  ]
}
```

Manage tasks:

```bash
uv run short2short tasks                                  # list tasks and status
uv run short2short resume 20261017-062032-d5cc10          # continue a paused/failed task
uv run short2short regen-scene 20261017-062032-d5cc10 2 \
  --prompt "Golden dragon formed by drones above the Dragon Bridge"
uv run short2short providers                              # list providers
```

Budget guards: `--max-clips` and `--max-total-seconds` stop a run before it spends past the limit; the task pauses and can be resumed.

## Development

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
uv run pytest -m integration    # tests that need real ffmpeg
```

Read [CONVENTIONS.md](CONVENTIONS.md) before contributing.

## License

[MIT](LICENSE)
