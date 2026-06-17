# VCrawl Archive

Archive output is designed to be inspectable without extra services or heavy dependencies.

Default layout:

```text
.vcrawl/archive/
  manifest.json
  pages.jsonl
  videos.jsonl
  assets.jsonl
  archive.warc
  pages/
    <domain>/
      <sha256-prefix>.html
```

Commands:

```bash
python3 -m vcrawl crawl --config vcrawl.json --resume --max-pages 20
python3 -m vcrawl archive --config vcrawl.json --verify
python3 -m vcrawl archive-verify --config vcrawl.json
```

The local UI exposes the same workflow in the Archive tab. It can refresh `videos.jsonl`, `assets.jsonl`, and `manifest.json`, run archive verification, and show missing snapshot/WARC/asset warnings without leaving the browser console.

Artifacts:

- `pages.jsonl` records page URL, final URL, status, title, challenge flag, video count, snapshot path, and SHA-256 checksum.
- `videos.jsonl` mirrors stored video candidates plus download state.
- `assets.jsonl` inventories downloaded media, thumbnails, subtitles, and manifest-like files when those paths are present in download metadata.
- `archive.warc` stores WARC/1.1 response records for fetched HTML pages when `archive.warc` is enabled.
- `manifest.json` records archive format flags, counts, and generation metadata.

Enable WARC output in `vcrawl.json`:

```json
{
  "archive": {
    "enabled": true,
    "warc": true
  }
}
```

WARC output is implemented with the Python standard library so the default install remains light. It captures the HTML responses seen by `vcrawl`; it is not yet a full Browsertrix-style WACZ package with browser subresource capture, indexes, and replay metadata.
