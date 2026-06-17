import json
import os
import shutil
import subprocess


def command_available(name):
    return shutil.which(name) is not None


def ffprobe(media_path, timeout_seconds=30):
    if not command_available("ffprobe"):
        raise RuntimeError("ffprobe is not installed or not on PATH")
    command = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        media_path,
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "ffprobe failed")
    return json.loads(completed.stdout or "{}")


def ffmpeg_thumbnail(media_path, output_path, at_seconds=1, timeout_seconds=60):
    if not command_available("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed or not on PATH")
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-ss",
        str(at_seconds),
        "-i",
        media_path,
        "-frames:v",
        "1",
        output_path,
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "ffmpeg thumbnail failed")
    return output_path
