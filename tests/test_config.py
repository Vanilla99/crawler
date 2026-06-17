import json
import os
import tempfile
import unittest

from vcrawl.config import ProjectConfig, config_from_dict, config_to_dict, load_config


class ConfigTests(unittest.TestCase):
    def test_round_trips_new_fields(self):
        config = config_from_dict(
            {
                "scope": {"respect_robots": False},
                "fetch": {
                    "per_domain_delay_seconds": {"example.com": 0.2},
                    "domain_failure_threshold": 2,
                    "per_domain_failure_thresholds": {"example.com": 1},
                    "auto_throttle": True,
                    "auto_throttle_target_concurrency": 2.5,
                    "auto_throttle_min_delay_seconds": 0.1,
                    "auto_throttle_max_delay_seconds": 8.0,
                },
                "network": {
                    "proxy_url": "http://127.0.0.1:8080",
                    "headers": {"X-Test": "1"},
                    "http_cache": True,
                    "http_cache_dir": ".vcrawl/cache",
                    "session_pool_size": 3,
                },
                "queue": {
                    "backend": "postgres",
                    "redis_url": "redis://localhost:6379/1",
                    "postgres_dsn": "postgresql://localhost/vcrawl",
                    "postgres_table": "vcrawl_queue",
                    "stale_after_seconds": 120,
                },
                "worker": {"worker_id": "local-dev", "heartbeat_interval_seconds": 10},
                "extract": {
                    "builtin_plugins": ["gallery"],
                    "plugin_configs": {"gallery": {"mode": "strict"}},
                },
                "media": {"download_concurrency": 4, "thumbnail": True, "thumbnail_at_seconds": 3},
                "archive": {"enabled": True, "root": ".vcrawl/archive-test", "warc": True},
                "observability": {
                    "json_log_path": ".vcrawl/logs/test.jsonl",
                    "opentelemetry": True,
                    "service_name": "vcrawl-test",
                },
                "discovery": {"sitemaps": ["https://example.com/sitemap.xml"], "feeds": ["https://example.com/rss.xml"]},
            }
        )
        self.assertFalse(config.scope.respect_robots)
        self.assertEqual(config.network.proxy_url, "http://127.0.0.1:8080")
        self.assertEqual(config.network.headers["X-Test"], "1")
        self.assertEqual(config.fetch.per_domain_delay_seconds["example.com"], 0.2)
        self.assertEqual(config.fetch.domain_failure_threshold, 2)
        self.assertEqual(config.fetch.per_domain_failure_thresholds["example.com"], 1)
        self.assertTrue(config.fetch.auto_throttle)
        self.assertEqual(config.fetch.auto_throttle_target_concurrency, 2.5)
        self.assertEqual(config.fetch.auto_throttle_min_delay_seconds, 0.1)
        self.assertEqual(config.fetch.auto_throttle_max_delay_seconds, 8.0)
        self.assertEqual(config.network.http_cache_dir, ".vcrawl/cache")
        self.assertEqual(config.network.session_pool_size, 3)
        self.assertEqual(config.queue.backend, "postgres")
        self.assertEqual(config.queue.redis_url, "redis://localhost:6379/1")
        self.assertEqual(config.queue.postgres_dsn, "postgresql://localhost/vcrawl")
        self.assertEqual(config.queue.postgres_table, "vcrawl_queue")
        self.assertEqual(config.worker.worker_id, "local-dev")
        self.assertEqual(config.extract.builtin_plugins, ["gallery"])
        self.assertEqual(config.extract.plugin_configs["gallery"]["mode"], "strict")
        self.assertEqual(config.media.download_concurrency, 4)
        self.assertEqual(config.archive.root, ".vcrawl/archive-test")
        self.assertTrue(config.archive.warc)
        self.assertEqual(config.observability.json_log_path, ".vcrawl/logs/test.jsonl")
        self.assertTrue(config.observability.opentelemetry)
        self.assertEqual(config.observability.service_name, "vcrawl-test")
        self.assertTrue(config.media.thumbnail)
        self.assertEqual(config.discovery.sitemaps, ["https://example.com/sitemap.xml"])
        data = config_to_dict(config)
        self.assertIn("discovery", data)
        self.assertEqual(data["queue"]["stale_after_seconds"], 120)
        self.assertEqual(data["queue"]["postgres_dsn"], "postgresql://localhost/vcrawl")
        self.assertEqual(data["queue"]["postgres_table"], "vcrawl_queue")
        self.assertEqual(data["worker"]["heartbeat_interval_seconds"], 10)
        self.assertEqual(data["extract"]["builtin_plugins"], ["gallery"])
        self.assertEqual(data["extract"]["plugin_configs"]["gallery"]["mode"], "strict")
        self.assertEqual(data["fetch"]["per_domain_delay_seconds"]["example.com"], 0.2)
        self.assertEqual(data["fetch"]["domain_failure_threshold"], 2)
        self.assertEqual(data["fetch"]["per_domain_failure_thresholds"]["example.com"], 1)
        self.assertTrue(data["fetch"]["auto_throttle"])
        self.assertEqual(data["fetch"]["auto_throttle_target_concurrency"], 2.5)
        self.assertEqual(data["fetch"]["auto_throttle_min_delay_seconds"], 0.1)
        self.assertEqual(data["fetch"]["auto_throttle_max_delay_seconds"], 8.0)
        self.assertTrue(data["network"]["http_cache"])
        self.assertEqual(data["network"]["session_pool_size"], 3)
        self.assertEqual(data["archive"]["root"], ".vcrawl/archive-test")
        self.assertTrue(data["archive"]["warc"])
        self.assertEqual(data["observability"]["service_name"], "vcrawl-test")
        self.assertTrue(data["observability"]["opentelemetry"])
        self.assertEqual(data["media"]["thumbnail_at_seconds"], 3)

    def test_load_config_resolves_plugin_paths_relative_to_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "vcrawl.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump({"extract": {"plugin_paths": ["plugins/site.json"]}}, fh)
            config = load_config(config_path)
            self.assertEqual(config.extract.plugin_paths, [os.path.join(tmp, "plugins", "site.json")])


if __name__ == "__main__":
    unittest.main()
