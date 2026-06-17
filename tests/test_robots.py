import unittest
from unittest.mock import patch

from vcrawl.robots import RobotsPolicy


class RobotsTests(unittest.TestCase):
    def test_disabled_policy_allows_everything(self):
        policy = RobotsPolicy(enabled=False)
        self.assertTrue(policy.allowed("https://example.com/private"))

    def test_policy_uses_robotparser(self):
        class FakeParser:
            def set_url(self, url):
                self.url = url

            def read(self):
                return None

            def can_fetch(self, user_agent, url):
                return not url.endswith("/blocked")

        with patch("vcrawl.robots.RobotFileParser", return_value=FakeParser()):
            policy = RobotsPolicy(user_agent="vcrawl-test", enabled=True)
            self.assertTrue(policy.allowed("https://example.com/open"))
            self.assertFalse(policy.allowed("https://example.com/blocked"))


if __name__ == "__main__":
    unittest.main()
