import shutil
import sys


def collect_diagnostics():
    return [
        ("python", sys.version.split()[0], True),
        ("sqlite3", "stdlib", True),
        ("redis",) + _optional_module("redis"),
        ("postgres-driver",) + _optional_any_module(["psycopg", "psycopg2"]),
        ("scrapy",) + _optional_module("scrapy"),
        ("crawlee",) + _optional_module("crawlee"),
        ("yt-dlp", shutil.which("yt-dlp") or "not found", shutil.which("yt-dlp") is not None),
        ("ffmpeg", shutil.which("ffmpeg") or "not found", shutil.which("ffmpeg") is not None),
        ("ffprobe", shutil.which("ffprobe") or "not found", shutil.which("ffprobe") is not None),
        ("playwright",) + _optional_module("playwright"),
        ("yaml",) + _optional_module("yaml"),
    ]


def _optional_module(name):
    try:
        __import__(name)
    except ImportError:
        return ("not installed", False)
    return ("installed", True)


def _optional_any_module(names):
    for name in names:
        try:
            __import__(name)
        except ImportError:
            continue
        return ("%s installed" % name, True)
    return ("not installed", False)


def format_diagnostics():
    lines = []
    for name, value, ok in collect_diagnostics():
        status = "ok" if ok else "optional-missing"
        lines.append("%-16s %-16s %s" % (name, status, value))
    return "\n".join(lines)
