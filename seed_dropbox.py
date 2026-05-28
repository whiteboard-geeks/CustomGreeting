"""Seed the QA registry from existing QA'ed videos in Dropbox.

Approach:
  1. Walk the Sales Dev folders, group videos by folder.
  2. For each folder, transcribe one sample via Whisper.
  3. Match the transcript (with the greeting stripped) against the canonical
     base_video transcripts already in the DB — that picks the folder's
     base_video_id deterministically, even when duration alone is ambiguous
     (e.g. the 110.82 vs 110.99 Barbara variants).
  4. For each file in the folder: extract the recipient name (strip numeric
     prefix and trailing dots; dot-duplicates are byte-identical, so we keep
     only the first occurrence), copy into the library, and insert a
     qa_videos row.

Default mode targets just the 114.33s Barbara cluster (the biggest reuse
pool). --sample N caps the total number of names seeded — useful for staging
spot-checks. --voice/--all let you scope to other voices.

Run on the machine that has the Dropbox folders synced locally, then rsync
the resulting /data/library/ up to the server.
"""

import argparse
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import db
import fingerprint as fp
from transcribe import transcribe_video as whisper_transcribe


DROPBOX_ROOTS = [
    Path("/Users/work/Dropbox/SalesDev 2025"),
    Path("/Users/work/Dropbox/Sales Dev 2026"),
]
GREETING_STRIP_CHARS = 80  # ignore the leading 'Hi <Name>!' when comparing to base


def video_track_duration(path: str) -> float | None:
    """Duration of the VIDEO stream only (not the container — audio extends it)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True, timeout=15,
        ).stdout.strip()
        return float(out) if out else None
    except Exception:
        return None


def clean_variable(filename: str) -> str:
    """Drop number prefix and trailing dots — '136_Kevin....mp4' -> 'Kevin'."""
    name = re.sub(r"\.(mp4|avi)$", "", filename)
    name = re.sub(r"^\d+_", "", name)
    return name.rstrip(".")


def discover_folders() -> list[Path]:
    """Return every leaf folder under DROPBOX_ROOTS that contains *.mp4 or *.avi."""
    folders: list[Path] = []
    for root in DROPBOX_ROOTS:
        if not root.is_dir():
            continue
        for d, _, files in os.walk(root):
            if any(f.endswith((".mp4", ".avi")) for f in files):
                folders.append(Path(d))
    return folders


def pick_sample(folder: Path) -> Path | None:
    """Pick the first non-zero mp4 in folder; fall back to avi."""
    for ext in (".mp4", ".avi"):
        for f in sorted(folder.iterdir()):
            if not f.is_file() or not f.name.endswith(ext):
                continue
            try:
                if f.stat().st_size > 100_000:
                    return f
            except OSError:
                pass
    return None


def best_base_video_for_transcript(
    transcript: str, candidates: list[dict]
) -> tuple[dict | None, float]:
    """Return (best_match_row, similarity) — strips the greeting before scoring."""
    body = transcript[GREETING_STRIP_CHARS:] if len(transcript) > GREETING_STRIP_CHARS else transcript
    best, best_sim = None, 0.0
    for bv in candidates:
        if not bv.get("transcript"):
            continue
        sim = fp.transcript_similarity(body, bv["transcript"])
        if sim > best_sim:
            best, best_sim = bv, sim
    return best, best_sim


def classify_folder(folder: Path, base_videos: list[dict],
                    music_id_for_voice: dict[str, int]) -> dict | None:
    """Transcribe one sample from `folder`, pick the matching base_video,
    and infer the matching music for the same voice. Returns a dict ready
    to attach to every file in the folder, or None if we couldn't classify."""
    sample = pick_sample(folder)
    if sample is None:
        return None

    sample_fp = fp.fingerprint_video(str(sample))
    # Quick disqualifier: a folder whose sample video duration is wildly off
    # from any base video probably uses a different (not-yet-seeded) base.
    video_dur = video_track_duration(str(sample)) or sample_fp["duration"]
    candidates = [
        bv for bv in base_videos
        if bv["resolution"] == sample_fp["resolution"]
        and abs(bv["duration"] - video_dur) < 2.0
    ]
    if not candidates:
        return None

    try:
        transcript = whisper_transcribe(str(sample))
    except Exception as exc:
        print(f"   !! transcribe failed for {sample.name}: {exc}", file=sys.stderr)
        return None

    best, sim = best_base_video_for_transcript(transcript, candidates)
    if best is None or sim < 0.6:
        return None

    voice_first_name = best["name"].split("_")[0]  # e.g. 'Barbara' / 'April'
    return {
        "base_video_id": best["id"],
        "base_video_name": best["name"],
        "voice_first_name": voice_first_name,
        "music_id": music_id_for_voice.get(voice_first_name.lower()),
        "match_similarity": round(sim, 3),
    }


VOICE_IDS = {
    "barbara": ("Ro4VVDudw85O3XfD3nva", "Barbara Pigg"),
    "april":   ("Ww6IPT0jYNzyTUBnXTDG", "April Lowrie Pro"),
}


def music_by_voice_first_name() -> dict[str, int]:
    """Look up music_track ids whose name starts with each voice first name."""
    by_voice: dict[str, int] = {}
    for m in db.list_music_tracks():
        first = m["name"].split("-")[0].lower()
        by_voice.setdefault(first, m["id"])
    return by_voice


def seed(*, sample_limit: int | None, voice_filter: str | None, dry_run: bool) -> None:
    db.init_db()
    base_videos = db.list_base_videos()
    if not base_videos:
        sys.exit("No base videos seeded yet. Run seed_canonical.py first.")
    music_map = music_by_voice_first_name()

    folders = discover_folders()
    print(f"Found {len(folders)} candidate folders under Dropbox.")

    print(f">> classifying folders by transcript (parallel Whisper calls)...")
    folder_classification: dict[Path, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(classify_folder, f, base_videos, music_map): f
                   for f in folders}
        for fut in concurrent.futures.as_completed(futures):
            folder = futures[fut]
            classification = fut.result()
            if classification:
                folder_classification[folder] = classification
                print(f"   ✓ {folder.name}: {classification['base_video_name']}"
                      f" (sim={classification['match_similarity']})")
            else:
                print(f"   ? {folder.name}: unclassified (skipped)")

    if voice_filter:
        folder_classification = {
            f: c for f, c in folder_classification.items()
            if c["voice_first_name"].lower() == voice_filter.lower()
        }
        print(f">> filtered to voice={voice_filter}: {len(folder_classification)} folders")

    # Pass over all files, dedup by (voice, base_video_id, music_id, variable).
    print(">> walking files...")
    seen_keys: set = set()
    insert_count = 0
    skip_existing = 0
    skip_dup = 0

    for folder, classification in folder_classification.items():
        bv_id = classification["base_video_id"]
        music_id = classification["music_id"]
        voice_first = classification["voice_first_name"].lower()
        if voice_first not in VOICE_IDS:
            continue
        voice_id, voice_name = VOICE_IDS[voice_first]

        for f in sorted(folder.iterdir()):
            if not f.is_file() or not f.name.endswith((".mp4", ".avi")):
                continue
            try:
                if f.stat().st_size < 100_000:
                    continue
            except OSError:
                continue

            variable = clean_variable(f.name)
            if not variable:
                continue
            key = (voice_id, bv_id, music_id, variable)
            if key in seen_keys:
                skip_dup += 1
                continue
            seen_keys.add(key)

            # Already in the DB? Skip.
            existing = db.lookup_qaed(voice_id, bv_id, music_id, [variable])
            if existing["already_qaed"]:
                skip_existing += 1
                continue

            if dry_run:
                insert_count += 1
                continue

            try:
                db.register_qaed_video(
                    voice_id=voice_id, voice_name=voice_name,
                    base_video_id=bv_id, music_id=music_id,
                    variable=variable,
                    greeting_text=f"Hi {variable}!",
                    source_path=str(f),
                    confirmed=True,
                )
                insert_count += 1
            except Exception as exc:
                print(f"   !! insert failed for {f}: {exc}", file=sys.stderr)
                continue

            if sample_limit and insert_count >= sample_limit:
                print(f">> reached sample limit of {sample_limit}, stopping.")
                _summary(insert_count, skip_existing, skip_dup, dry_run)
                return

    _summary(insert_count, skip_existing, skip_dup, dry_run)


def _summary(inserted: int, skipped_existing: int, skipped_dup: int, dry_run: bool) -> None:
    verb = "would insert" if dry_run else "inserted"
    print(f"\n>> done: {verb} {inserted}, "
          f"skipped {skipped_existing} already-in-DB, "
          f"skipped {skipped_dup} duplicates within scan")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=None,
                        help="Cap total inserted rows (for staging tests).")
    parser.add_argument("--voice", choices=["barbara", "april"], default=None,
                        help="Only seed folders matching this voice.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify and report; don't copy files or insert rows.")
    args = parser.parse_args()
    seed(sample_limit=args.sample, voice_filter=args.voice, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
