from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser


class RobotsPolicy:
    def __init__(self, user_agent="vcrawl/0.1", enabled=True):
        self.user_agent = user_agent
        self.enabled = enabled
        self._cache = {}

    def allowed(self, url):
        if not self.enabled:
            return True
        parser = self._parser_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    def _parser_for(self, url):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
        root = "%s://%s" % (parsed.scheme, parsed.netloc)
        if root not in self._cache:
            robots_url = urljoin(root, "/robots.txt")
            parser = RobotFileParser()
            parser.set_url(robots_url)
            try:
                parser.read()
            except Exception:
                parser = None
            self._cache[root] = parser
        return self._cache[root]
