"""Transcribe video/audio files using OpenAI Whisper API.

We extract a low-bitrate mono audio track first to stay under Whisper's 25MB
upload limit and reduce API latency. Used to fingerprint base videos by their
baked-in voiceover script.
"""

import os
import subprocess
import tempfile
from pathlib import Path


def _extract_audio_for_whisper(video_path: str, dest: str) -> None:
    """Extract a low-bitrate mono mp3 at 16kHz — small enough for Whisper API."""
    subprocess.run(
        [
            "ffmpeg", "-v", "quiet", "-y", "-i", video_path,
            "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
            dest,
        ],
        check=True,
    )


def transcribe_video(video_path: str) -> str:
    """Return the transcribed text of a video's audio track. Raises on failure."""
    from openai import OpenAI  # lazy import — only needed when actually transcribing

    client = OpenAI()
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        audio_path = tmp.name
    try:
        _extract_audio_for_whisper(video_path, audio_path)
        with open(audio_path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
            )
        return resp.strip() if isinstance(resp, str) else str(resp).strip()
    finally:
        try:
            os.unlink(audio_path)
        except FileNotFoundError:
            pass
