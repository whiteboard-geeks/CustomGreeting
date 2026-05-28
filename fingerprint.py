"""Video and audio fingerprinting via ffprobe.

Used to detect when an uploaded base video or music track matches one already in
the library, so the user doesn't have to re-upload and we can reuse QA'ed videos
rendered against the same inputs.

Same base video = same visual + same baked-in voiceover body. Fingerprint by
duration (seconds, 2dp) + resolution. Music = duration only.
"""

import hashlib
import json
import re
import subprocess
from difflib import SequenceMatcher
from pathlib import Path

DURATION_TOLERANCE_S = 1.0  # how close two videos must be to count as the same


def normalize_transcript(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Used so Whisper
    transcription quirks (commas, hyphens, casing) don't break similarity."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def transcript_similarity(a: str, b: str) -> float:
    """Return 0..1 similarity between two transcripts after normalization.

    For matching an uploaded base video, the user's upload won't contain a
    TTS greeting — only the baked-in voiceover. For matching a rendered QA'ed
    video (which starts with "Hi [Name]!"), strip the first ~80 chars before
    calling this.
    """
    na, nb = normalize_transcript(a), normalize_transcript(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _ffprobe(path: str) -> dict:
    """Return parsed ffprobe JSON for path. Raises on failure."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", path,
        ],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(result.stdout)


def _file_hash(path: str, sample_bytes: int = 1024 * 1024) -> str:
    """MD5 of the first `sample_bytes` of the file. Fast fingerprint for files
    that were truly uploaded twice; not used as the sole identity check."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read(sample_bytes))
    return h.hexdigest()


def fingerprint_video(path: str) -> dict:
    """Return {duration, width, height, resolution, file_hash} for a video."""
    data = _ffprobe(path)
    duration = float(data["format"]["duration"])
    video_stream = next(s for s in data["streams"] if s.get("codec_type") == "video")
    width = video_stream["width"]
    height = video_stream["height"]
    return {
        "duration": round(duration, 2),
        "width": width,
        "height": height,
        "resolution": f"{width}x{height}",
        "file_hash": _file_hash(path),
    }


def fingerprint_music(path: str) -> dict:
    """Return {duration, file_hash} for an audio file."""
    data = _ffprobe(path)
    duration = float(data["format"]["duration"])
    return {
        "duration": round(duration, 2),
        "file_hash": _file_hash(path),
    }


def videos_match(fp_a: dict, fp_b: dict, tolerance_s: float = DURATION_TOLERANCE_S) -> bool:
    """Two base videos are 'the same' if resolution matches and duration is within
    tolerance. We allow some slack because the audio track length (which feeds the
    container duration) shifts slightly with TTS length when these were originally
    rendered — but the underlying base video is identical."""
    if fp_a["resolution"] != fp_b["resolution"]:
        return False
    return abs(fp_a["duration"] - fp_b["duration"]) <= tolerance_s


def music_matches(fp_a: dict, fp_b: dict, tolerance_s: float = DURATION_TOLERANCE_S) -> bool:
    """Music tracks match if file_hash matches exactly, or duration is very close."""
    if fp_a["file_hash"] == fp_b["file_hash"]:
        return True
    return abs(fp_a["duration"] - fp_b["duration"]) <= tolerance_s
