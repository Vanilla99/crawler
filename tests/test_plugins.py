import os
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

from vcrawl.cli import main
from vcrawl.extractors import VideoExtractor
from vcrawl.models import FetchResult
from vcrawl.plugin_templates import write_plugin_template
from vcrawl.plugins import PluginRegistry, load_plugin, run_plugin_tests


class PluginTests(unittest.TestCase):
    def test_plugin_extracts_video_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_path = os.path.join(tmp, "site_plugin.py")
            with open(plugin_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "def extract_videos(page_url, html, title=None):\n"
                    "    return [{'media_url': '/plugin-video.mp4', 'title': title, 'source': 'plugin.demo'}]\n"
                )
            result = FetchResult(
                url="https://example.com/watch",
                status_code=200,
                headers={},
                text="<html><title>Plugin Page</title></html>",
            )
            page = VideoExtractor(plugin_paths=[plugin_path]).extract(result)
            self.assertEqual(len(page.videos), 1)
            self.assertEqual(page.videos[0].media_url, "https://example.com/plugin-video.mp4")
            self.assertEqual(page.videos[0].source, "plugin.demo")

    def test_plugin_template_is_valid_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_plugin_template(os.path.join(tmp, "plugin.py"))
            with open(path, "r", encoding="utf-8") as fh:
                source = fh.read()
            compile(source, path, "exec")

    def test_builtin_gallery_plugin_extracts_anchor_media(self):
        result = FetchResult(
            url="https://example.com/gallery/",
            status_code=200,
            headers={},
            text='<html><a href="/media/clip.mp4"><img src="/thumb.jpg"></a></html>',
        )
        page = VideoExtractor(builtin_plugins=["gallery"]).extract(result)
        self.assertEqual(page.videos[0].media_url, "https://example.com/media/clip.mp4")
        self.assertEqual(page.videos[0].source, "builtin.gallery")

    def test_manifest_plugin_config_and_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_path = os.path.join(tmp, "manifest_plugin.py")
            with open(plugin_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "def extract_videos(page_url, html, title=None, config=None):\n"
                    "    label = (config or {}).get('source_label', 'default')\n"
                    "    return [{'media_url': '/configured.mp4', 'source': label}]\n"
                )
            manifest_path = os.path.join(tmp, "vcrawl-plugin.json")
            with open(manifest_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "name": "demo-manifest",
                        "module": "manifest_plugin.py",
                        "capabilities": ["html", "direct-media"],
                        "config_schema": {
                            "type": "object",
                            "properties": {"source_label": {"type": "string"}},
                            "required": ["source_label"],
                        },
                        "fixtures": [
                            {
                                "name": "configured",
                                "page_url": "https://example.com/watch",
                                "html": "<html></html>",
                                "expected_media_urls": ["https://example.com/configured.mp4"],
                            }
                        ],
                    },
                    fh,
                )
            registry = PluginRegistry(paths=[manifest_path], plugin_configs={"demo-manifest": {"source_label": "custom"}})
            videos = registry.extract_videos("https://example.com/watch", "<html></html>")
            self.assertEqual(videos[0].source, "custom")
            self.assertEqual(videos[0].media_url, "https://example.com/configured.mp4")
            self.assertTrue(run_plugin_tests(path=manifest_path, config={"source_label": "custom"})["ok"])

    def test_manifest_plugin_config_schema_rejects_bad_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_path = os.path.join(tmp, "bad_config_plugin.py")
            with open(plugin_path, "w", encoding="utf-8") as fh:
                fh.write("def extract_videos(page_url, html, title=None, config=None):\n    return []\n")
            manifest_path = os.path.join(tmp, "plugin.json")
            with open(manifest_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "name": "schema-demo",
                        "module": "bad_config_plugin.py",
                        "config_schema": {"type": "object", "properties": {"enabled": {"type": "boolean"}}},
                    },
                    fh,
                )
            with self.assertRaises(RuntimeError):
                load_plugin(manifest_path, config_map={"schema-demo": {"enabled": "yes"}})

    def test_builtin_fixture_runner_and_cli(self):
        self.assertTrue(run_plugin_tests(builtin="gallery")["ok"])
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["plugin", "test", "--builtin", "gallery"]), 0)
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["plugin", "list-builtins"]), 0)


if __name__ == "__main__":
    unittest.main()
