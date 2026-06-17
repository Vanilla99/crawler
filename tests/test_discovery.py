import unittest

from vcrawl.config import DiscoveryConfig, ProjectConfig
from vcrawl.discovery import discover_feed_urls, discover_sitemap_urls, expand_discovery_seeds


class DiscoveryTests(unittest.TestCase):
    def test_discovers_sitemap_urls(self):
        xml = """
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://example.com/watch/1</loc></url>
          <url><loc>https://example.com/watch/2</loc></url>
        </urlset>
        """
        urls = discover_sitemap_urls("https://example.com/sitemap.xml", fetch_text=lambda _url: xml)
        self.assertEqual(urls, ["https://example.com/watch/1", "https://example.com/watch/2"])

    def test_discovers_rss_urls(self):
        xml = """
        <rss><channel>
          <item><link>https://example.com/watch/1</link></item>
          <item><guid>https://example.com/watch/2</guid></item>
        </channel></rss>
        """
        urls = discover_feed_urls("https://example.com/feed.xml", fetch_text=lambda _url: xml)
        self.assertEqual(urls, ["https://example.com/watch/1", "https://example.com/watch/2"])

    def test_expands_and_limits_discovery_seeds(self):
        config = ProjectConfig(
            seeds=["https://example.com/root"],
            discovery=DiscoveryConfig(
                sitemaps=["https://example.com/sitemap.xml"],
                max_discovered_urls=2,
            ),
        )
        xml = """
        <urlset>
          <url><loc>https://example.com/watch/1</loc></url>
          <url><loc>https://example.com/watch/2</loc></url>
        </urlset>
        """
        urls = expand_discovery_seeds(config, fetch_text=lambda _url: xml)
        self.assertEqual(urls, ["https://example.com/root", "https://example.com/watch/1"])


if __name__ == "__main__":
    unittest.main()
