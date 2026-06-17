# Test Matrix

`vcrawl test-matrix` gives the project a layered verification path. The default install stays light, while heavier checks can be run explicitly when the matching optional tools are installed.

## Layers

- `unit`: runs `python3 -m unittest discover -s tests`.
- `smoke`: inspects a public video page and expects at least one video candidate. When it fails, the report includes extraction diagnostics, media hint counts, candidate source counts, dynamic player signals, recommendations, and notes such as `try_browser_fetcher_for_dynamic_players`.
- `optional`: reports optional framework/tool availability for Redis, Postgres drivers, Playwright, yt-dlp, FFmpeg, Scrapy, Crawlee, and related tools. Missing optional tools do not fail the layer.
- `integration`: generates Scrapy, Crawlee Python, Colly, and Nutch scaffolds, checks expected files, compiles generated Python, and validates generated Nutch XML.
- `long-run`: enqueues 1000 URLs by default, claims them as in-progress, ages them as stale, and verifies recovery back to pending.

## Commands

```bash
python3 -m vcrawl test-matrix --layer unit
python3 -m vcrawl test-matrix --layer optional --json
python3 -m vcrawl test-matrix --layer integration --output .vcrawl/test-matrix
python3 -m vcrawl test-matrix --layer long-run --long-run-size 1000
python3 -m vcrawl test-matrix --layer smoke --smoke-url https://www.w3schools.com/html/html5_video.asp
```

For one-off debugging outside the matrix, `python3 -m vcrawl inspect <url>` prints a compact diagnostic summary, dynamic script markers, network hint kinds, and safe recommendations. Use `--json` when another tool should consume the same structured diagnostics.

Use `--layer all` when you want the full local matrix. It includes the network smoke check, so it is intentionally slower and more environment-sensitive than unit or long-run checks.

## Product Intent

The matrix mirrors the way mature crawlers are validated:

- Keep unit tests fast for normal development.
- Make public-page smoke checks explicit.
- Make smoke failures explainable instead of returning only `videos=0`.
- Treat optional integrations as capability probes rather than hard dependencies.
- Verify generated integration projects without requiring every external runtime.
- Exercise queue recovery with a realistic long-running crawl failure mode.
