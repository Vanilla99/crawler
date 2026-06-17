import os
import tempfile
import unittest

from vcrawl.config import ProjectConfig, ScopeConfig
from vcrawl.integrations import scaffold_colly, scaffold_crawlee_python, scaffold_nutch, scaffold_scrapy


class IntegrationScaffoldTests(unittest.TestCase):
    def test_scaffolds_scrapy_project(self):
        config = ProjectConfig(
            project="Demo Video",
            seeds=["https://example.com/videos"],
            scope=ScopeConfig(allowed_domains=["example.com"], max_depth=1),
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = scaffold_scrapy(config, os.path.join(tmp, "scrapy"))
            spider = os.path.join(output, "demo_video", "spiders", "video_spider.py")
            self.assertTrue(os.path.exists(spider))
            with open(spider, "r", encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("https://example.com/videos", content)
            self.assertIn("VideoSpider", content)
            with open(os.path.join(output, "demo_video", "settings.py"), "r", encoding="utf-8") as fh:
                self.assertIn("AUTOTHROTTLE_ENABLED = True", fh.read())
            with open(os.path.join(output, "demo_video", "middlewares.py"), "r", encoding="utf-8") as fh:
                self.assertIn("StaticProxyMiddleware", fh.read())

    def test_scaffolds_crawlee_python_project(self):
        config = ProjectConfig(project="demo", seeds=["https://example.com/videos"])
        with tempfile.TemporaryDirectory() as tmp:
            output = scaffold_crawlee_python(config, os.path.join(tmp, "crawlee"))
            main_py = os.path.join(output, "main.py")
            self.assertTrue(os.path.exists(main_py))
            with open(main_py, "r", encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("PlaywrightCrawler", content)
            self.assertIn("ProxyConfiguration", content)
            self.assertIn("use_session_pool", content)
            self.assertIn("https://example.com/videos", content)

    def test_scaffolds_colly_project(self):
        config = ProjectConfig(
            project="Demo Video",
            seeds=["https://example.com/videos"],
            scope=ScopeConfig(allowed_domains=["example.com"], max_depth=2),
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = scaffold_colly(config, os.path.join(tmp, "colly"))
            main_go = os.path.join(output, "main.go")
            self.assertTrue(os.path.exists(main_go))
            with open(main_go, "r", encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("github.com/gocolly/colly/v2", content)
            self.assertIn("RoundRobinProxySwitcher", content)
            self.assertIn("SetCookieJar", content)
            self.assertIn("https://example.com/videos", content)
            self.assertIn("colly.MaxDepth(2)", content)

    def test_scaffolds_nutch_project(self):
        config = ProjectConfig(
            project="Demo Video",
            seeds=["https://example.com/videos"],
            scope=ScopeConfig(allowed_domains=["example.com"], max_depth=1),
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = scaffold_nutch(config, os.path.join(tmp, "nutch"))
            seed = os.path.join(output, "urls", "seed.txt")
            urlfilter = os.path.join(output, "conf", "regex-urlfilter.txt")
            site = os.path.join(output, "conf", "nutch-site.xml")
            self.assertTrue(os.path.exists(seed))
            self.assertTrue(os.path.exists(urlfilter))
            self.assertTrue(os.path.exists(site))
            with open(seed, "r", encoding="utf-8") as fh:
                self.assertIn("https://example.com/videos", fh.read())
            with open(urlfilter, "r", encoding="utf-8") as fh:
                self.assertIn("+^https?://example\\.com/.*", fh.read())
            with open(site, "r", encoding="utf-8") as fh:
                self.assertIn("<name>fetcher.server.delay</name>", fh.read())


if __name__ == "__main__":
    unittest.main()
