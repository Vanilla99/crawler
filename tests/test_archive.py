import json
import os
import tempfile
import unittest

from vcrawl.archive import ArchiveManager, ArchiveOptions, verify_archive
from vcrawl.models import DownloadResult, ExtractedPage, FetchResult, VideoCandidate
from vcrawl.storage import SQLiteStore


class ArchiveTests(unittest.TestCase):
    def test_page_snapshot_sidecar_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ArchiveManager(ArchiveOptions(root=tmp, project="demo"))
            result = FetchResult(
                url="https://example.com/watch",
                status_code=200,
                headers={},
                text="<html><title>Demo</title></html>",
                final_url="https://example.com/watch",
                fetcher="fake",
            )
            extracted = ExtractedPage(url=result.url, title="Demo", links=["https://example.com/a"])
            record = manager.write_page_snapshot(result, extracted)
            self.assertTrue(record["html_path"].endswith(".html"))
            self.assertTrue(os.path.exists(os.path.join(tmp, record["html_path"])))
            self.assertTrue(os.path.exists(os.path.join(tmp, "pages.jsonl")))

            store = SQLiteStore(os.path.join(tmp, "state.sqlite"))
            try:
                manifest = manager.write_manifest(store)
            finally:
                store.close()
            self.assertEqual(manifest["counts"]["html_snapshots"], 1)
            verification = verify_archive(tmp)
            self.assertTrue(verification["ok"])
            self.assertEqual(verification["pages"], 1)

    def test_page_snapshot_writes_warc_response_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ArchiveManager(ArchiveOptions(root=tmp, project="demo", warc=True))
            result = FetchResult(
                url="https://example.com/watch",
                status_code=200,
                headers={"Content-Type": "text/html"},
                text="<html><title>Demo</title></html>",
                final_url="https://example.com/watch",
                fetcher="fake",
            )
            extracted = ExtractedPage(url=result.url, title="Demo")
            record = manager.write_page_snapshot(result, extracted)
            self.assertEqual(record["warc_path"], "archive.warc")
            self.assertTrue(record["warc_record_id"].startswith("<urn:uuid:"))
            warc_path = os.path.join(tmp, "archive.warc")
            self.assertTrue(os.path.exists(warc_path))
            with open(warc_path, "rb") as fh:
                content = fh.read()
            self.assertIn(b"WARC/1.1", content)
            self.assertIn(b"WARC-Type: response", content)
            self.assertIn(b"WARC-Target-URI: https://example.com/watch", content)
            self.assertIn(b"HTTP/1.1 200 OK", content)

            store = SQLiteStore(os.path.join(tmp, "state.sqlite"))
            try:
                manifest = manager.write_manifest(store)
            finally:
                store.close()
            self.assertEqual(manifest["format"]["warc_status"], "written")
            self.assertEqual(manifest["counts"]["warc_records"], 1)
            verification = verify_archive(tmp)
            self.assertTrue(verification["ok"])
            self.assertEqual(verification["warc_records"], 1)
            self.assertTrue(verification["warc_sha256"])

    def test_warc_can_be_written_without_html_or_jsonl_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ArchiveManager(
                ArchiveOptions(root=tmp, project="demo", html_snapshots=False, jsonl_sidecar=False, warc=True)
            )
            result = FetchResult(
                url="https://example.com/warc-only",
                status_code=200,
                headers={},
                text="<html></html>",
                fetcher="fake",
            )
            record = manager.write_page_snapshot(result)
            self.assertIsNone(record["html_path"])
            self.assertEqual(record["warc_path"], "archive.warc")
            self.assertFalse(os.path.exists(os.path.join(tmp, "pages.jsonl")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "archive.warc")))

    def test_archive_sidecars_include_video_download_and_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_path = os.path.join(tmp, "demo.mp4")
            thumbnail_path = os.path.join(tmp, "demo.jpg")
            with open(media_path, "wb") as fh:
                fh.write(b"video")
            with open(thumbnail_path, "wb") as fh:
                fh.write(b"thumb")
            store = SQLiteStore(os.path.join(tmp, "state.sqlite"))
            try:
                store.record_videos(
                    [
                        VideoCandidate(
                            page_url="https://example.com/watch",
                            media_url="https://cdn.example.com/demo.mp4",
                            kind="source",
                            title="Demo",
                            source="test",
                        )
                    ]
                )
                store.record_download(
                    DownloadResult(
                        page_url="https://example.com/watch",
                        media_url="https://cdn.example.com/demo.mp4",
                        status="downloaded",
                        output_path=media_path,
                        metadata={"thumbnail": thumbnail_path},
                    )
                )
                archive_root = os.path.join(tmp, "archive")
                result = ArchiveManager(ArchiveOptions(root=archive_root, project="demo")).write_sidecars(store)
            finally:
                store.close()
            self.assertEqual(result["videos"], 1)
            self.assertEqual(result["assets"], 2)
            with open(os.path.join(archive_root, "assets.jsonl"), "r", encoding="utf-8") as fh:
                assets = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual({asset["kind"] for asset in assets}, {"media", "thumbnail"})
            self.assertTrue(verify_archive(archive_root)["ok"])

    def test_verify_reports_missing_html_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump({"archive_version": 1}, fh)
            with open(os.path.join(tmp, "pages.jsonl"), "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"html_path": "pages/missing.html", "html_sha256": "bad"}) + "\n")
            result = verify_archive(tmp)
            self.assertFalse(result["ok"])
            self.assertIn("missing html snapshot", result["errors"][0])

    def test_verify_reports_missing_warc_when_pages_reference_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "archive_version": 1,
                        "format": {"warc": True},
                        "files": {"warc": "archive.warc"},
                        "counts": {"html_snapshots": 1},
                    },
                    fh,
                )
            with open(os.path.join(tmp, "pages.jsonl"), "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"warc_path": "archive.warc"}) + "\n")
            result = verify_archive(tmp)
            self.assertFalse(result["ok"])
            self.assertIn("missing WARC file", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
