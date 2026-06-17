from urllib.parse import urljoin, urlparse, urldefrag


def canonicalize_url(url, base=None):
    joined = urljoin(base, url) if base else url
    cleaned, _fragment = urldefrag(joined)
    return cleaned


def domain_of(url):
    return urlparse(url).netloc.lower()


def in_scope(url, scope):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.netloc.lower()
    if scope.allowed_domains and host not in [domain.lower() for domain in scope.allowed_domains]:
        return False
    path = parsed.path or "/"
    if scope.include and not any(marker in path for marker in scope.include):
        return False
    if any(marker in path for marker in scope.exclude):
        return False
    return True
