import unittest

from vcrawl.config import ScopeConfig
from vcrawl.scope import canonicalize_url, in_scope


class ScopeTests(unittest.TestCase):
    def test_canonicalize_removes_fragment(self):
        self.assertEqual(canonicalize_url("/a#b", "https://example.com/root"), "https://example.com/a")

    def test_in_scope_filters_domain_and_excludes(self):
        scope = ScopeConfig(allowed_domains=["example.com"], exclude=["/login"])
        self.assertTrue(in_scope("https://example.com/watch/1", scope))
        self.assertFalse(in_scope("https://example.com/login", scope))
        self.assertFalse(in_scope("https://other.example/watch/1", scope))


if __name__ == "__main__":
    unittest.main()
