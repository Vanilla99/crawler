import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import Mock, patch

from vcrawl.cli import main
from vcrawl.test_matrix import (
    format_test_matrix_summary,
    run_smoke_layer,
    run_integration_layer,
    run_long_run_layer,
    run_optional_layer,
    run_test_matrix,
    run_unit_layer,
)


class TestMatrixTests(unittest.TestCase):
    def test_optional_layer_reports_missing_tools_without_failing(self):
        result = run_optional_layer()
        self.assertTrue(result["ok"])
        self.assertIn("diagnostics", result)
        self.assertIn("python", [row["name"] for row in result["diagnostics"]])

    def test_integration_layer_generates_and_verifies_scaffolds(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_integration_layer(output_dir=tmp)
            self.assertTrue(result["ok"])
            self.assertFalse(result["ephemeral"])
            self.assertTrue(os.path.exists(os.path.join(tmp, "scrapy", "scrapy.cfg")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "crawlee-python", "main.py")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "colly", "main.go")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "nutch", "conf", "nutch-site.xml")))

    def test_smoke_layer_reports_extraction_diagnostics_on_zero_videos(self):
        result = run_smoke_layer(smoke_url="data:text/html,<html><title>Plain</title><body>No%20video</body></html>")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["videos"], 0)
        self.assertIn("diagnostics", result)
        self.assertIn("try_browser_fetcher_for_dynamic_players", result["diagnostic_notes"])
        self.assertIn("try_browser_fetcher_for_dynamic_players", result["summary"])

    def test_long_run_layer_recovers_one_thousand_stale_requests(self):
        result = run_long_run_layer(1000)
        self.assertTrue(result["ok"])
        self.assertEqual(result["queued"], 1000)
        self.assertEqual(result["claimed"], 1000)
        self.assertEqual(result["recovered"], 1000)
        self.assertEqual(result["stats_after_recovery"]["pending"], 1000)

    def test_unit_layer_uses_unittest_discover(self):
        completed = Mock(returncode=0, stdout="ok\n", stderr="")
        with patch("vcrawl.test_matrix.subprocess.run", return_value=completed) as run:
            result = run_unit_layer()
        self.assertTrue(result["ok"])
        self.assertEqual(result["command"][-3:], ["discover", "-s", "tests"])
        run.assert_called_once()

    def test_run_test_matrix_rejects_unknown_layer(self):
        with self.assertRaises(ValueError):
            run_test_matrix(layer="unknown")

    def test_format_summary_is_human_readable(self):
        report = {"ok": True, "layer": "optional", "duration_seconds": 0.1, "sections": [run_optional_layer()]}
        text = format_test_matrix_summary(report)
        self.assertIn("test-matrix", text)
        self.assertIn("optional", text)

    def test_cli_runs_long_run_layer(self):
        with redirect_stdout(StringIO()) as stdout:
            code = main(["test-matrix", "--layer", "long-run", "--long-run-size", "1000", "--json"])
        self.assertEqual(code, 0)
        self.assertIn('"recovered": 1000', stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
