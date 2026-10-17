# AGENTS.md — short2short

Instructions for AI coding agents working in this repo.

## Read first
All coding rules live in [CONVENTIONS.md](CONVENTIONS.md). Read it before making any change and follow it strictly — it covers environment, layout, style, errors, security, providers, tests, and commits.

## Quick checks
Before reporting a task as done, run:

```sh
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
```

## Hard rules (summary — CONVENTIONS.md is authoritative)
- Never log or store secrets in plaintext; never commit private keys.
- No captcha bypass, humanizer, proxy rotation, or prompt rewriting to evade filters.
- The `flow` adapter drives the UI over loopback CDP only; no private Google REST calls.
- Tests never hit the network, open Chrome, or run real ffmpeg unless marked `integration`.
- Do not commit unless asked.
