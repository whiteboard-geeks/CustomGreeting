"""Seed the library with the canonical Whiteboard Geeks base videos & music.

Run once after deploying. Idempotent — skips files already present (by file_hash).
Expects the canonical assets in `--source-dir` (default: the WBG Dropbox folder
on the local machine). The transcript for each base video is taken from the
matching `*.txt` file in `--transcripts-dir` if present; otherwise we call
Whisper to transcribe (needs OPENAI_API_KEY).
"""

import argparse
import sys
from pathlib import Path

import db
import fingerprint as fp


DEFAULT_SOURCE = "/Users/work/Dropbox/Greeting Automation Base Files"
DEFAULT_TRANSCRIPTS = "/tmp/cg_transcripts"


def seed_base_videos(source_dir: Path, transcripts_dir: Path) -> None:
    videos_dir = source_dir / "Base Videos"
    if not videos_dir.is_dir():
        print(f"!! no Base Videos folder at {videos_dir}", file=sys.stderr)
        return

    existing_hashes = {row["file_hash"] for row in db.list_base_videos()}

    for mp4 in sorted(videos_dir.glob("*.mp4")):
        fingerprint = fp.fingerprint_video(str(mp4))
        if fingerprint["file_hash"] in existing_hashes:
            print(f"   skip {mp4.name} (already in library)")
            continue

        # Load transcript if we have it on disk; otherwise transcribe via Whisper.
        transcript_path = transcripts_dir / (mp4.stem + ".txt")
        if transcript_path.exists() and transcript_path.stat().st_size > 100:
            transcript = transcript_path.read_text().strip()
            print(f"   transcript loaded from {transcript_path.name} ({len(transcript)} chars)")
        else:
            print(f"   transcribing {mp4.name} via Whisper...")
            import transcribe
            transcript = transcribe.transcribe_video(str(mp4))

        row = db.register_base_video(
            name=mp4.stem,
            source_path=str(mp4),
            fingerprint=fingerprint,
            transcript=transcript,
        )
        print(f"   added base_video id={row['id']} name={row['name']} "
              f"dur={row['duration']}s res={row['resolution']}")


def seed_music(source_dir: Path) -> None:
    music_dir = source_dir / "Base Music"
    if not music_dir.is_dir():
        print(f"!! no Base Music folder at {music_dir}", file=sys.stderr)
        return

    existing_hashes = {row.get("file_hash") for row in db.list_music_tracks()}

    for wav in sorted(music_dir.glob("*.wav")):
        fingerprint = fp.fingerprint_music(str(wav))
        if fingerprint["file_hash"] in existing_hashes:
            print(f"   skip {wav.name} (already in library)")
            continue
        row = db.register_music(
            name=wav.stem, source_path=str(wav), fingerprint=fingerprint,
        )
        print(f"   added music id={row['id']} name={row['name']} dur={row['duration']}s")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", default=DEFAULT_SOURCE,
                        help="Folder containing 'Base Videos/' and 'Base Music/'")
    parser.add_argument("--transcripts-dir", default=DEFAULT_TRANSCRIPTS,
                        help="Folder of pre-computed *.txt transcripts (optional)")
    args = parser.parse_args()

    source = Path(args.source_dir).expanduser().resolve()
    transcripts = Path(args.transcripts_dir).expanduser().resolve()

    db.init_db()
    print(f"library at {db.DATA_DIR}")
    print(f"source: {source}")
    print(f"transcripts: {transcripts}")
    print()
    print(">> seeding base videos")
    seed_base_videos(source, transcripts)
    print()
    print(">> seeding music")
    seed_music(source)


if __name__ == "__main__":
    main()
