import re
from html import unescape


CHALLENGE_PATTERNS = [
    re.compile(r"captcha", re.I),
    re.compile(r"recaptcha", re.I),
    re.compile(r"hcaptcha", re.I),
    re.compile(r"cf-chl", re.I),
    re.compile(r"turnstile", re.I),
    re.compile(r"verify\s+you\s+are\s+human", re.I),
    re.compile(r"checking\s+your\s+browser", re.I),
    re.compile(r"human\s+verification", re.I),
    re.compile(r"are\s+you\s+a\s+robot", re.I),
]


def detect_challenge(url, status_code, headers, body):
    """Detect verification pages without attempting to solve or bypass them."""
    haystack = " ".join(
        [
            url or "",
            str(status_code),
            " ".join("%s:%s" % (k, v) for k, v in (headers or {}).items()),
            unescape((body or "")[:20000]),
        ]
    )
    if status_code in (401, 403, 429, 503) and any(pattern.search(haystack) for pattern in CHALLENGE_PATTERNS):
        return True
    return any(pattern.search(haystack) for pattern in CHALLENGE_PATTERNS)


def challenge_message(url):
    return (
        "Human verification or bot challenge detected for %s. "
        "vcrawl will not bypass it. Use a lower crawl rate, official APIs, "
        "or complete verification manually in a user-controlled browser profile and resume."
    ) % url
