import os
import tempfile
import unittest
from pathlib import Path

from vcrawl.downloads import DownloadManager, DownloadOptions
from vcrawl.exporters import export_csv
from vcrawl.models import VideoCandidate
from vcrawl.storage import SQLiteStore


class DownloadTests(unittest.TestCase):
    def test_downloads_direct_file_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source.mp4")
            with open(source, "wb") as fh:
                fh.write(b"demo-video")
            output_dir = os.path.join(tmp, "downloads")
            candidate = VideoCandidate(
                page_url="https://example.com/watch",
                media_url=Path(source).as_uri(),
                kind="source",
                title="Demo Video",
                source="test",
            )
            manager = DownloadManager(DownloadOptions(output_dir=output_dir))
            result = manager.download_candidate(candidate)
            self.assertEqual(result.status, "downloaded")
            self.assertTrue(os.path.exists(result.output_path))
            with open(result.output_path, "rb") as fh:
                self.assertEqual(fh.read(), b"demo-video")

    def test_download_rows_uses_concurrency_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = []
            for idx in range(2):
                source = os.path.join(tmp, "source-%s.mp4" % idx)
                with open(source, "wb") as fh:
                    fh.write(("demo-video-%s" % idx).encode("utf-8"))
                sources.append(source)
            rows = [
                {
                    "page_url": "https://example.com/watch/%s" % idx,
                    "media_url": Path(sources[idx]).as_uri(),
                    "kind": "source",
                    "title": "Demo %s" % idx,
                    "source": "test",
                    "metadata_json": "{}",
                }
                for idx in range(2)
            ]
            manager = DownloadManager(DownloadOptions(output_dir=os.path.join(tmp, "downloads"), concurrency=2))
            results = manager.download_rows(rows)
            self.assertEqual([result.status for result in results].count("downloaded"), 2)

    def test_export_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(os.path.join(tmp, "state.sqlite"))
            try:
                store.record_videos(
                    [
                        VideoCandidate(
                            page_url="https://example.com/watch",
                            media_url="https://cdn.example.com/v.mp4",
                            kind="source",
                            title="Demo",
                            source="test",
                        )
                    ]
                )
                output = os.path.join(tmp, "videos.csv")
                count = export_csv(store, output)
                self.assertEqual(count, 1)
                with open(output, "r", encoding="utf-8") as fh:
                    content = fh.read()
                self.assertIn("page_url,media_url", content)
                self.assertIn("https://cdn.example.com/v.mp4", content)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
