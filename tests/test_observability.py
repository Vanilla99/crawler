import json
import os
import tempfile
import unittest

from vcrawl.observability import JsonLogWriter, classify_error, make_observation_record


class ObservabilityTests(unittest.TestCase):
    def test_error_taxonomy(self):
        self.assertEqual(classify_error(status_code=429), "rate_limited")
        self.assertEqual(classify_error(status_code=403), "forbidden_or_auth")
        self.assertEqual(classify_error(message="SSL certificate failed"), "tls_error")
        self.assertEqual(classify_error(event_type="challenge"), "verification_required")
        self.assertEqual(classify_error(event_type="domain_blocked"), "domain_failure_threshold")

    def test_observation_record_has_phase_status_and_severity(self):
        record = make_observation_record("fetch_failed", message="https://example.com timed out", worker_id="w1")
        self.assertEqual(record["phase"], "fetch")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["severity"], "error")
        self.assertEqual(record["error_class"], "timeout")
        self.assertEqual(record["url"], "https://example.com")

    def test_json_log_writer_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            JsonLogWriter(path).write({"event_type": "fetch", "url": "https://example.com"})
            with open(path, "r", encoding="utf-8") as fh:
                row = json.loads(fh.readline())
            self.assertEqual(row["event_type"], "fetch")
            self.assertEqual(row["url"], "https://example.com")


if __name__ == "__main__":
    unittest.main()
