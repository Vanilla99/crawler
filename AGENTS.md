# Project Notes

## Purpose

This workspace contains `vcrawl`, a Python-first MVP for video-oriented web crawling: seed URLs, scoped link discovery, page fetching, video clue extraction, media resolution hooks, local state, and a small CLI.

## Common Commands

- `python3 -m vcrawl doctor`
- `python3 -m vcrawl init demo-project`
- `python3 -m vcrawl inspect <url>`
- `python3 -m unittest discover -s tests`

## Important Directories

- `vcrawl/`: framework package and CLI.
- `tests/`: unit tests and local crawl fixtures.
- `examples/`: sample project inputs.
- `docs/`: design notes and operational guidance.

## Style Notes

- Keep the default install lightweight and based on the Python standard library.
- Integrations such as Playwright, yt-dlp, FFmpeg, Scrapy, and Crawlee should be optional and detected at runtime.
- Do not implement CAPTCHA bypass, DRM bypass, credential stuffing, or anti-bot evasion. Detect verification pages, pause, and let the user resume after manual action.
