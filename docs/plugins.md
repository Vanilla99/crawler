# VCrawl Plugins

Plugins extend video extraction without modifying core crawler code.

Built-in plugins:

```bash
python3 -m vcrawl plugin list-builtins
python3 -m vcrawl plugin test --builtin gallery
python3 -m vcrawl plugin test --builtin playlist
python3 -m vcrawl plugin test --builtin m3u8
python3 -m vcrawl plugin test --builtin jsonld
```

Default built-ins:

- `gallery`: media URLs linked from gallery-style anchors.
- `playlist`: `.m3u8` and `.mpd` playlist URLs in HTML attributes.
- `m3u8`: pages that are themselves M3U8 manifests.
- `jsonld`: `VideoObject` records in JSON-LD script tags.

Local plugin options:

- a Python file with `extract_videos(page_url, html, title=None, config=None)`
- a manifest JSON file
- a directory containing `vcrawl-plugin.json` or `plugin.json`

Manifest example:

```json
{
  "name": "site-plugin",
  "version": "0.1",
  "module": "site_plugin.py",
  "capabilities": ["html", "direct-media"],
  "config_schema": {
    "type": "object",
    "properties": {
      "source_label": {"type": "string"}
    }
  },
  "fixtures": [
    {
      "name": "basic page",
      "page_url": "https://example.com/watch",
      "html": "<script>const media='https://cdn.example.com/video.mp4'</script>",
      "expected_media_urls": ["https://cdn.example.com/video.mp4"]
    }
  ]
}
```

Project config:

```json
{
  "extract": {
    "builtin_plugins": ["gallery", "playlist", "m3u8", "jsonld"],
    "plugin_paths": ["plugins/vcrawl-plugin.json"],
    "plugin_configs": {
      "site-plugin": {
        "source_label": "site.custom"
      }
    }
  }
}
```

Test before crawling:

```bash
python3 -m vcrawl plugin test --path plugins/vcrawl-plugin.json
python3 -m vcrawl plugin test --path plugins/vcrawl-plugin.json --fixture fixtures/watch.json
```

Fixture files can contain either one fixture object or a `{"fixtures": [...]}` object. Each fixture should include `page_url`, `html`, and `expected_media_urls`.

The local UI also exposes plugins in the Plugins tab. It lists built-in and configured local plugins, shows capabilities and fixture counts, surfaces load errors, and lets users run fixture tests through `/api/plugins/test` without leaving the browser console.
