import hashlib
import os
import zipfile
import shutil
import time
import uuid
import streamlit as st
from moviepy.editor import (
    AudioFileClip,
    AudioClip,
    concatenate_audioclips,
    CompositeAudioClip,
    VideoFileClip,
)
from elevenlabs import VoiceSettings, PronunciationDictionaryVersionLocator
from elevenlabs.client import ElevenLabs

import db
import fingerprint as fp_mod

db.init_db()


def _render_wave_qa_ui(wave: dict) -> None:
    """Show a focused QA queue for a single wave. Streams one video at a
    time so 100+ name waves stay responsive. Bulk-imports the approved
    videos into the QA library when the user is done."""
    st.markdown(f"## 📂 Wave: {wave['name']}")
    st.caption(
        f"Voice: **{wave['voice_name']}** · "
        f"text: `{wave['text_before']} <name>{wave['text_after']}`"
    )

    videos = db.list_wave_videos(wave["id"])
    if not videos:
        st.info("This wave has no videos yet. Switch back to 'Start a new wave' to generate.")
        return

    stats = db.wave_stats(wave["id"])
    approved = [v for v in videos if v["status"] == "approved"]
    rejected = [v for v in videos if v["status"] == "rejected"]
    pending = [v for v in videos if v["status"] == "pending"]

    progress = (stats["approved"] + stats["rejected"]) / max(stats["total"], 1)
    st.progress(progress, text=f"{stats['approved']} approved · "
                                 f"{stats['rejected']} rejected · "
                                 f"{stats['pending']} pending")

    if pending:
        cursor_key = f"_wave_cursor_{wave['id']}"
        cursor = st.session_state.get(cursor_key, 0) % len(pending)
        current = pending[cursor]

        nav_cols = st.columns([1, 4, 1])
        with nav_cols[0]:
            if st.button("← Prev", use_container_width=True,
                         disabled=(cursor == 0),
                         key=f"prev_{wave['id']}"):
                st.session_state[cursor_key] = max(0, cursor - 1)
                st.rerun()
        with nav_cols[1]:
            st.markdown(
                f"**Reviewing `{current['variable']}` "
                f"({cursor + 1} of {len(pending)} pending)**"
            )
        with nav_cols[2]:
            if st.button("Next →", use_container_width=True,
                         disabled=(cursor >= len(pending) - 1),
                         key=f"next_{wave['id']}"):
                st.session_state[cursor_key] = min(len(pending) - 1, cursor + 1)
                st.rerun()

        if os.path.isfile(current["file_path"]):
            st.video(current["file_path"])
        else:
            st.warning(f"File missing: {current['file_path']}")

        ar_cols = st.columns(2)
        with ar_cols[0]:
            if st.button("✅ Approve", type="primary", use_container_width=True,
                         key=f"approve_{wave['id']}_{current['variable']}"):
                db.update_wave_video_status(
                    wave_id=wave["id"], variable=current["variable"], status="approved",
                )
                # Don't advance cursor by index; the list shrinks, so the cursor
                # naturally points at the next pending item. Clamp instead.
                pending_after = [v for v in pending if v["variable"] != current["variable"]]
                st.session_state[cursor_key] = min(cursor, max(0, len(pending_after) - 1))
                st.rerun()
        with ar_cols[1]:
            if st.button("❌ Reject", use_container_width=True,
                         key=f"reject_{wave['id']}_{current['variable']}"):
                db.update_wave_video_status(
                    wave_id=wave["id"], variable=current["variable"], status="rejected",
                )
                pending_after = [v for v in pending if v["variable"] != current["variable"]]
                st.session_state[cursor_key] = min(cursor, max(0, len(pending_after) - 1))
                st.rerun()
    else:
        st.success("🎉 No pending videos left in this wave.")

    with st.expander(f"Approved ({len(approved)}) and rejected ({len(rejected)})",
                     expanded=False):
        if approved:
            st.markdown("**Approved:** " + ", ".join(f"`{v['variable']}`" for v in approved))
        if rejected:
            st.markdown("**Rejected:** " + ", ".join(f"`{v['variable']}`" for v in rejected))

    st.divider()
    st.markdown("### Commit approved videos to the QA library")
    if not approved:
        st.caption("Nothing approved yet.")
    else:
        st.caption(
            f"This will add **{len(approved)}** videos to the QA library so "
            "future generations for this voice / base video / music reuse them."
        )
        commit_clicked = st.button(
            "Add approved videos to the library",
            type="primary",
            disabled=(len(approved) == 0),
        )
        if commit_clicked:
            added = 0
            for v in approved:
                if v["from_library"]:
                    continue  # already in the library
                try:
                    db.register_qaed_video(
                        voice_id=wave["voice_id"],
                        voice_name=wave["voice_name"],
                        base_video_id=wave["base_video_id"],
                        music_id=wave["music_id"],
                        variable=v["variable"],
                        greeting_text=f"{wave['text_before']} {v['variable']} {wave['text_after']}",
                        source_path=v["file_path"],
                        confirmed=True,
                    )
                    added += 1
                except Exception as exc:
                    st.warning(f"Failed to add {v['variable']}: {exc}")
            st.success(f"Added {added} approved videos to the QA library.")
            if stats["pending"] == 0:
                db.update_wave_status(wave["id"], "completed")
                st.info(f"Marking wave **{wave['name']}** as completed.")
            st.rerun()


def _handle_qa_upload(uploaded_zip) -> None:
    """Read a zip of QA'ed videos. If manifest.json is present, use its
    voice/base_video/music context. Otherwise, fall back to whichever voice +
    base video + music are currently selected. Register each .mp4 inside the
    zip (excluding `already_qaed/` entries) as a QA'ed library video."""
    import io
    import json
    import tempfile
    import zipfile as zf

    cache_key = (uploaded_zip.name, uploaded_zip.size)
    if st.session_state.get("_qa_upload_handled") == cache_key:
        return

    try:
        zbytes = uploaded_zip.getvalue()
        archive = zf.ZipFile(io.BytesIO(zbytes))
    except Exception as exc:
        st.error(f"Couldn't read zip: {exc}")
        return

    # Try the manifest first.
    manifest: dict | None = None
    try:
        with archive.open("manifest.json") as f:
            manifest = json.load(f)
    except KeyError:
        manifest = None
    except Exception as exc:
        st.warning(f"manifest.json present but unreadable: {exc}")

    if manifest:
        voice_id = manifest.get("voice_id")
        voice_name = manifest.get("voice_name")
        base_video_id = manifest.get("base_video_id")
        music_id = manifest.get("music_id")
        text_before = manifest.get("text_before", "Hi")
        text_after = manifest.get("text_after", "!")
        st.info(
            f"Found manifest: **{voice_name}** + "
            f"**{manifest.get('base_video_name', '?')}** + "
            f"**{manifest.get('music_name', '?')}**"
        )
    else:
        st.warning(
            "No manifest.json in the zip — using the voice / base video / "
            "music currently selected above."
        )
        bv_src = st.session_state.get("_bv_source") or {}
        music_src = st.session_state.get("_music_source") or {}
        voice_id = st.session_state.get("_active_voice_id")
        voice_name = st.session_state.get("_active_voice_name")
        base_video_id = bv_src.get("id")
        music_id = music_src.get("id")
        text_before = "Hi"
        text_after = "!"

    if not voice_id or not base_video_id:
        st.error(
            "Can't import without a voice and a known base video. Select a "
            "voice and a library base video above, then re-upload."
        )
        return

    # Walk the entries. Filenames inside `already_qaed/` are skipped — they
    # came from the library and are already registered.
    candidates = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        path = info.filename
        if not path.lower().endswith(".mp4"):
            continue
        if path.startswith("already_qaed/"):
            continue
        variable = os.path.splitext(os.path.basename(path))[0]
        if not variable:
            continue
        candidates.append((path, variable))

    if not candidates:
        st.warning("No new .mp4 files found in the zip.")
        return

    added: list[str] = []
    replaced: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        for path, variable in candidates:
            tmp_video = os.path.join(tmpdir, os.path.basename(path))
            with archive.open(path) as src, open(tmp_video, "wb") as dst:
                shutil.copyfileobj(src, dst)
            existing = db.lookup_qaed(voice_id, base_video_id, music_id, [variable])
            try:
                db.register_qaed_video(
                    voice_id=voice_id,
                    voice_name=voice_name or "Unknown",
                    base_video_id=base_video_id,
                    music_id=music_id,
                    variable=variable,
                    greeting_text=f"{text_before} {variable} {text_after}",
                    source_path=tmp_video,
                    confirmed=True,
                )
                if existing["already_qaed"]:
                    replaced.append(variable)
                else:
                    added.append(variable)
            except Exception as exc:
                st.warning(f"Failed to register `{variable}`: {exc}")

    if added or replaced:
        msg = []
        if added:
            msg.append(f"Added **{len(added)}** new videos to the library.")
        if replaced:
            msg.append(f"Replaced **{len(replaced)}** existing entries.")
        st.success(" ".join(msg))
        if added:
            st.caption("Added: " + ", ".join(added[:20])
                       + (f" (+{len(added)-20} more)" if len(added) > 20 else ""))
        if replaced:
            st.caption("Replaced: " + ", ".join(replaced[:20])
                       + (f" (+{len(replaced)-20} more)" if len(replaced) > 20 else ""))
    else:
        st.info("Nothing to import.")

    st.session_state["_qa_upload_handled"] = cache_key


def _generation_signature(*, voice_id: str, variables: list[str],
                          text_before: str, text_after: str,
                          clip_start, voiceover_volume, variable_audio_volume,
                          music_volume, force_fresh: bool) -> str:
    """Stable hash of every input that affects the generated zip. Used to keep
    the previous download button alive as long as none of the inputs have
    changed — clicking Generate Videos saves the new signature, and a later
    UI change (different name, voice, volume, …) invalidates it."""
    import hashlib
    import json
    bv = st.session_state.get("_bv_source") or {}
    music = st.session_state.get("_music_source") or {}
    if bv.get("type") == "upload":
        u = bv.get("uploaded")
        bv_sig = ("upload", bv.get("name"), getattr(u, "size", 0))
    else:
        bv_sig = ("lib", bv.get("path"))
    if music.get("type") == "upload":
        u = music.get("uploaded")
        m_sig = ("upload", music.get("name"), getattr(u, "size", 0))
    else:
        m_sig = ("lib", music.get("path"))
    payload = {
        "voice_id": voice_id,
        "variables": sorted(variables),
        "text_before": text_before,
        "text_after": text_after,
        "bv": bv_sig,
        "music": m_sig,
        "clip_start": float(clip_start),
        "voiceover_volume": float(voiceover_volume),
        "variable_audio_volume": float(variable_audio_volume),
        "music_volume": float(music_volume),
        "force_fresh": bool(force_fresh),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _compute_qa_split(variables: list[str], voice_id: str, use_library: bool) -> dict:
    """Split variables into already-QA'ed (with library file_path) and needs_generation,
    given the currently selected base video + music. Returns {'enabled': bool,
    'already_qaed': [...], 'needs_generation': [...]}.

    'enabled' is False when the toggle is off, when no base video is selected,
    or when the selected base video isn't recognised by the library.
    """
    bv = st.session_state.get("_bv_source") or {}
    music = st.session_state.get("_music_source") or {}
    bv_id = bv.get("id")
    music_id = music.get("id")
    if not use_library or not variables or not bv_id:
        return {"enabled": False, "already_qaed": [], "needs_generation": list(variables)}
    result = db.lookup_qaed(voice_id, bv_id, music_id, variables)
    result["enabled"] = True
    return result


def _voice_usage_caption(last_used: str | None, voice_name: str) -> str:
    """Friendly caption for a library item: when it was last used with this voice."""
    if not last_used:
        return f"Not yet used with {voice_name}"
    # SQLite CURRENT_TIMESTAMP stores 'YYYY-MM-DD HH:MM:SS'
    date_part = last_used.split(" ", 1)[0]
    return f"Last used with {voice_name} on {date_part}"


def _select_or_upload_base_video(voice_id: str, voice_name: str):
    """Render the Library / Upload chooser for the base video. Stores the choice
    in session state and returns a (source_type, identifier) tuple that the
    generation flow can resolve into bytes via `read_base_video_bytes()`."""
    library = db.list_base_videos_for_voice(voice_id, voice_name)
    has_library = bool(library)
    options = []
    if has_library:
        options.append("Select from library")
    options.append("Upload new")

    default_index = 0  # Library if available, else Upload
    source_choice = st.radio(
        "Base Video",
        options=options,
        horizontal=True,
        index=default_index,
        key=f"bv_source_{voice_id}",
    )

    if source_choice == "Select from library":
        labels = [
            f"{bv['name']}  •  {bv['duration']}s  •  {bv['resolution']}"
            for bv in library
        ]
        idx = st.selectbox(
            "Choose a base video",
            options=list(range(len(library))),
            format_func=lambda i: labels[i],
            key=f"bv_pick_{voice_id}",
        )
        chosen = library[idx]
        st.caption(_voice_usage_caption(chosen.get("last_used_with_voice"), voice_name))
        st.session_state["_bv_source"] = {"type": "library", "path": chosen["file_path"],
                                           "name": chosen["name"], "id": chosen["id"]}
        with st.expander("Preview base video", expanded=False):
            st.video(chosen["file_path"])
        return
    # Upload new — try to match against the library so the QA registry stays useful.
    uploaded = st.file_uploader("Upload Base Video", type=["mp4"], key="bv_upload")
    matched_id = _detect_uploaded_base_video(uploaded)
    if uploaded is not None:
        st.session_state["_bv_source"] = {"type": "upload", "uploaded": uploaded,
                                           "name": uploaded.name, "id": matched_id}
        with st.expander("Preview base video", expanded=False):
            st.video(uploaded)
    else:
        st.session_state.pop("_bv_source", None)


def _select_or_upload_music(voice_id: str, voice_name: str):
    """Library / Upload chooser for the music track."""
    library = db.list_music_for_voice(voice_id, voice_name)
    has_library = bool(library)
    options = []
    if has_library:
        options.append("Select from library")
    options.append("Upload new")

    source_choice = st.radio(
        "Music",
        options=options,
        horizontal=True,
        index=0,
        key=f"music_source_{voice_id}",
    )

    if source_choice == "Select from library":
        labels = [f"{m['name']}  •  {m['duration']}s" for m in library]
        idx = st.selectbox(
            "Choose a music track",
            options=list(range(len(library))),
            format_func=lambda i: labels[i],
            key=f"music_pick_{voice_id}",
        )
        chosen = library[idx]
        st.caption(_voice_usage_caption(chosen.get("last_used_with_voice"), voice_name))
        st.session_state["_music_source"] = {"type": "library", "path": chosen["file_path"],
                                              "name": chosen["name"], "id": chosen["id"]}
        with st.expander("Preview music", expanded=False):
            st.audio(chosen["file_path"])
        return
    uploaded = st.file_uploader("Upload Music", type=["wav"], key="music_upload")
    matched_id = _detect_uploaded_music(uploaded)
    if uploaded is not None:
        st.session_state["_music_source"] = {"type": "upload", "uploaded": uploaded,
                                              "name": uploaded.name, "id": matched_id}
        with st.expander("Preview music", expanded=False):
            st.audio(uploaded)
    else:
        st.session_state.pop("_music_source", None)


def _read_source_bytes(src: dict | None) -> bytes | None:
    if not src:
        return None
    if src["type"] == "upload":
        return src["uploaded"].getvalue()
    if src["type"] == "library":
        with open(src["path"], "rb") as f:
            return f.read()
    return None


def read_base_video_bytes() -> bytes | None:
    return _read_source_bytes(st.session_state.get("_bv_source"))


def read_music_bytes() -> bytes | None:
    return _read_source_bytes(st.session_state.get("_music_source"))


def has_base_video() -> bool:
    return st.session_state.get("_bv_source") is not None


def has_music() -> bool:
    return st.session_state.get("_music_source") is not None


def _detect_uploaded_base_video(uploaded_file) -> int | None:
    """Show a banner if an uploaded base video matches one already in the library.
    Returns the matched base_videos.id if any, else None — so the caller can
    use that id for QA registry lookups."""
    if uploaded_file is None:
        return None
    cache_key = (uploaded_file.name, uploaded_file.size)
    cached = st.session_state.get("_bv_last_detected")
    if cached and cached[0] == cache_key:
        return cached[1]

    tmp_path = os.path.join("temp_data", f"_detect_bv_{uuid.uuid4()}.mp4")
    os.makedirs("temp_data", exist_ok=True)
    matched_id: int | None = None
    try:
        with open(tmp_path, "wb") as f:
            f.write(uploaded_file.getvalue())
        fingerprint = fp_mod.fingerprint_video(tmp_path)
        match = db.find_matching_base_video(fingerprint)
        if match:
            matched_id = match["id"]
            st.info(
                f"📚 This base video matches **{match['name']}** "
                f"({match['duration']}s, {match['resolution']}) — already in the library."
            )
        else:
            st.info(
                f"🆕 Looks like a new base video "
                f"({fingerprint['duration']}s, {fingerprint['resolution']}) — "
                "not yet in the library."
            )
    except Exception as e:
        st.warning(f"Could not fingerprint base video: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    st.session_state["_bv_last_detected"] = (cache_key, matched_id)
    return matched_id


def _detect_uploaded_music(uploaded_file) -> int | None:
    """Show a banner if an uploaded music track matches one already in the library.
    Returns the matched music_tracks.id if any, else None."""
    if uploaded_file is None:
        return None
    cache_key = (uploaded_file.name, uploaded_file.size)
    cached = st.session_state.get("_music_last_detected")
    if cached and cached[0] == cache_key:
        return cached[1]

    tmp_path = os.path.join("temp_data", f"_detect_music_{uuid.uuid4()}.wav")
    os.makedirs("temp_data", exist_ok=True)
    matched_id: int | None = None
    try:
        with open(tmp_path, "wb") as f:
            f.write(uploaded_file.getvalue())
        fingerprint = fp_mod.fingerprint_music(tmp_path)
        match = db.find_matching_music(fingerprint)
        if match:
            matched_id = match["id"]
            st.info(
                f"🎵 This music matches **{match['name']}** "
                f"({match['duration']}s) — already in the library."
            )
        else:
            st.info(
                f"🆕 Looks like a new music track "
                f"({fingerprint['duration']}s) — not yet in the library."
            )
    except Exception as e:
        st.warning(f"Could not fingerprint music: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    st.session_state["_music_last_detected"] = (cache_key, matched_id)
    return matched_id


# Password protection
def check_password():
    """Returns True if the user entered the correct password."""

    def password_entered():
        if st.session_state["password"] == os.environ.get("APP_PASSWORD"):
            st.session_state["password_correct"] = True
            del st.session_state["password"]  # Don't store password
        else:
            st.session_state["password_correct"] = False

    if st.session_state.get("password_correct", False):
        return True

    st.text_input(
        "Password", type="password", on_change=password_entered, key="password"
    )
    if (
        "password_correct" in st.session_state
        and not st.session_state["password_correct"]
    ):
        st.error("Incorrect password")
    return False


if not check_password():
    st.stop()


# Function to create a one-second silent audio clip
def create_silence(duration=1):
    return AudioClip(lambda t: 0, duration=duration)


# Default TTS model. Barbara/April were cloned in the multilingual_v2 era, so
# we stay in that voice-embedding family — turbo_v2_5 is compatible AND it
# hard-locks language_code="en" (multilingual_v2 treats it as advisory only,
# which is what caused 'Lance' to drift into another language).
DEFAULT_MODEL_ID = "eleven_turbo_v2_5"


@st.cache_data(ttl=600, show_spinner=False)
def list_tts_models(api_key_fingerprint: str) -> list[dict]:
    """Return [{'model_id', 'name'}] for every TTS-capable model. Cached for
    10 minutes per API key. We pass an opaque fingerprint of the key so the
    cache is per-account but the key itself never lands in cache metadata."""
    primary = os.environ.get("ELEVENLABS_API_KEY")
    alt = os.environ.get("ELEVENLABS_API_KEY_ALT")
    seen: dict[str, str] = {}  # model_id -> friendly name
    for key in (primary, alt):
        if not key:
            continue
        try:
            c = ElevenLabs(api_key=key)
            for m in c.models.get_all():
                if getattr(m, "can_do_text_to_speech", False):
                    seen.setdefault(m.model_id, m.name)
        except Exception:
            continue
    return [{"model_id": mid, "name": name} for mid, name in seen.items()]


# Function to generate greeting and save as MP3
def text_to_speech_file(
    client,
    text: str,
    name: str,
    output_folder: str,
    voice_id: str,
    pronunciation_dict=None,
    model_id: str = DEFAULT_MODEL_ID,
) -> str:
    kwargs = {
        "voice_id": voice_id,
        "output_format": "mp3_44100_192",
        "text": text,
        # language_code is honored as a hard lock by turbo_v2_5 / flash_v2_5,
        # advisory for multilingual_v2 / v3. We send it anyway as a hint.
        "language_code": "en",
        "model_id": model_id,
        "voice_settings": VoiceSettings(
            stability=0.6,
            similarity_boost=0.9,
            style=0.1,
            use_speaker_boost=True,
        ),
    }

    if pronunciation_dict:
        kwargs["pronunciation_dictionary_locators"] = [
            PronunciationDictionaryVersionLocator(
                pronunciation_dictionary_id=pronunciation_dict.id,
                version_id=pronunciation_dict.version_id,
            )
        ]

    response = client.text_to_speech.convert(**kwargs)

    save_file_path = os.path.join(output_folder, f"{name}.mp3")
    with open(save_file_path, "wb") as f:
        for chunk in response:
            if chunk:
                f.write(chunk)

    return save_file_path


# Function to create an audio clip with greeting and music
def create_audio_clip(
    audio_path,
    video,
    clip_start,
    variable_audio_volume_factor,
    voiceover_volume_factor,
    music_path,
    music_volume_factor,
):
    audio = AudioFileClip(audio_path)
    audio = audio.volumex(variable_audio_volume_factor)
    video_voiceover_audio = video.audio.subclip(clip_start).volumex(
        voiceover_volume_factor
    )
    voiceover_audio_with_greeting = concatenate_audioclips(
        [audio, video_voiceover_audio]
    )
    silence = create_silence(2)
    voiceover_audio_with_intro_silence = concatenate_audioclips(
        [silence, voiceover_audio_with_greeting]
    )
    music = AudioFileClip(music_path).volumex(music_volume_factor)
    final_audio = CompositeAudioClip(
        [voiceover_audio_with_intro_silence, music.set_start(0)]
    )
    return final_audio


def get_session_paths():
    if "session_id" not in st.session_state:
        st.session_state["session_id"] = uuid.uuid4()

    session_id = st.session_state["session_id"]
    base_dir = os.path.join("temp_data", str(session_id))
    input_folder = os.path.join(base_dir, "input")
    output_folder = os.path.join(base_dir, "output")
    return input_folder, output_folder


def cleanup_old_sessions(max_age_seconds=3600):
    temp_data_dir = "temp_data"
    if not os.path.exists(temp_data_dir):
        return

    current_time = time.time()
    for item in os.listdir(temp_data_dir):
        item_path = os.path.join(temp_data_dir, item)
        if os.path.isdir(item_path):
            try:
                # Check modification time
                if current_time - os.path.getmtime(item_path) > max_age_seconds:
                    shutil.rmtree(item_path)
            except Exception as e:
                print(f"Error cleaning up {item_path}: {e}")


# Streamlit UI
st.set_page_config(page_title="Video Greeting Generator", page_icon="🎬")
st.title("Video Greeting Generator")

# Initialize session and clean up old files on first load
if "session_id" not in st.session_state:
    st.session_state["session_id"] = uuid.uuid4()
    cleanup_old_sessions()

# Reuse from the QA library by default. The user can flip this off per-batch
# to force fresh generation for every name.
use_qa_library = True  # set below after the variables section


# ---------------------------------------------------------------------------
# Wave selection: resume an existing one or start a new one. The chosen wave
# (or absence thereof) drives whether Generate Videos persists state.
# ---------------------------------------------------------------------------
open_waves = db.list_waves(status="in_progress")
wave_options = ["➕ Start a new wave"] + [
    f"📂 {w['name']}  ({w['voice_name']})" for w in open_waves
]
wave_label_to_id = {f"📂 {w['name']}  ({w['voice_name']})": w["id"] for w in open_waves}
chosen_wave_label = st.selectbox(
    "Wave",
    options=wave_options,
    index=0,
    help="A wave saves your generated videos + QA progress so you can come "
         "back and finish later. Pick an open wave to resume it, or start a "
         "new one (you'll name it before generating).",
)
active_wave: dict | None = None
if chosen_wave_label != wave_options[0]:
    active_wave_id = wave_label_to_id[chosen_wave_label]
    active_wave = db.get_wave(active_wave_id)
    st.session_state["_active_wave_id"] = active_wave_id
else:
    st.session_state.pop("_active_wave_id", None)


# Wave-resume short-circuit: show the QA queue and skip the rest of the
# generation form entirely. The wave already holds voice + base video + music
# + variables, so there's nothing to re-enter.
if active_wave:
    _render_wave_qa_ui(active_wave)
    st.stop()


# New-wave name input. Required for Generate Videos so the resulting batch
# is recoverable; the test buttons don't need it.
new_wave_name = st.text_input(
    "New wave name",
    value="",
    placeholder="e.g. 'Barbara BC Wave 67' — required for Generate Videos",
    help="Give this batch a name so you can come back to it later for QA.",
)


# All voice options. We declare them up here so both the dropdown and the
# wave-resume code can index into the same canonical list.
ALL_VOICE_OPTIONS = [
    ("Barbara Pigg", "Ro4VVDudw85O3XfD3nva", "primary"),
    ("ZTS07a VO", "LEbsUt7al3JBfWgXlFFc", "primary"),
    ("April Lowrie Pro", "Ww6IPT0jYNzyTUBnXTDG", "alt"),
    ("Dale - YourCause", "xUeVfoX4TgqQTP9P2Rns", "alt"),
    ("Mark Davis", "gVSJ0zoFJ1Wgov5gD1Pi", "alt"),
]

# Add a select box for voice selection
voice_option = st.selectbox(
    "Select Voice",
    options=ALL_VOICE_OPTIONS,
    format_func=lambda x: x[0],
)

# Read API keys from environment variables
primary_api_key = os.environ.get("ELEVENLABS_API_KEY")
alt_api_key = os.environ.get("ELEVENLABS_API_KEY_ALT")

client_registry = {}
if primary_api_key:
    client_registry["primary"] = ElevenLabs(api_key=primary_api_key)
if alt_api_key:
    client_registry["alt"] = ElevenLabs(api_key=alt_api_key)

if "primary" not in client_registry:
    st.error("ELEVENLABS_API_KEY environment variable not set.")
else:
    # Determine which client to use for the selected voice
    selected_label = voice_option[2]
    if selected_label not in client_registry:
        st.error(
            f"Selected voice '{voice_option[0]}' requires an API key that has not been provided."
        )
        st.stop()

    # Alias the chosen client so the rest of the code remains unchanged
    client = client_registry[selected_label]

    # Add pronunciation dictionary file uploader
    pronunciation_file = st.file_uploader(
        "Upload Pronunciation Dictionary (Optional)", type=["pls"]
    )
    pronunciation_dict = None

    if pronunciation_file is not None:
        with st.spinner("Uploading pronunciation dictionary..."):
            try:
                pronunciation_dict = client.pronunciation_dictionary.add_from_file(
                    file=pronunciation_file.read(),
                    name=f"dictionary_{pronunciation_file.name}",
                )
                st.success("Pronunciation dictionary uploaded successfully!")
            except Exception as e:
                st.error(f"Error uploading pronunciation dictionary: {str(e)}")
                pronunciation_dict = None

    # Remember which voice is active so the QA-upload flow can use it when no
    # manifest is present.
    st.session_state["_active_voice_id"] = voice_option[1]
    st.session_state["_active_voice_name"] = voice_option[0]

    # Advanced: which TTS model to use. Hidden by default; dynamically queries
    # ElevenLabs so newly-released models show up automatically.
    with st.expander("Advanced settings", expanded=False):
        api_fp = hashlib.sha256(
            (os.environ.get("ELEVENLABS_API_KEY", "") + "::" +
             os.environ.get("ELEVENLABS_API_KEY_ALT", "")).encode()
        ).hexdigest()[:16]
        try:
            tts_models = list_tts_models(api_fp)
        except Exception as exc:
            st.warning(f"Couldn't fetch model list: {exc}")
            tts_models = [{"model_id": DEFAULT_MODEL_ID, "name": "Eleven v3"}]

        model_ids = [m["model_id"] for m in tts_models]
        if DEFAULT_MODEL_ID not in model_ids:
            tts_models = [{"model_id": DEFAULT_MODEL_ID, "name": "Eleven Turbo v2.5"}] + tts_models
            model_ids = [m["model_id"] for m in tts_models]

        default_idx = model_ids.index(DEFAULT_MODEL_ID) if DEFAULT_MODEL_ID in model_ids else 0
        labels = {m["model_id"]: f"{m['name']}  ({m['model_id']})" for m in tts_models}
        selected_model_id = st.selectbox(
            "TTS model",
            options=model_ids,
            format_func=lambda mid: labels.get(mid, mid),
            index=default_idx,
            key="tts_model_id",
            help="Default is eleven_turbo_v2_5 — compatible with the existing "
                 "Barbara / April voice clones AND it hard-locks language_code='en'. "
                 "Note: eleven_v3 and eleven_english_v1 use different voice "
                 "embeddings, so the same voice_id may sound noticeably different.",
        )
        st.caption(f"Using `{selected_model_id}` for TTS.")
        if selected_model_id in ("eleven_v3", "eleven_monolingual_v1"):
            st.warning(
                "⚠️ Older voices (Barbara / April) were cloned on "
                "multilingual_v2 — `eleven_v3` and `eleven_english_v1` use "
                "different latents and may sound noticeably different. "
                "If the voice doesn't sound right, fall back to "
                "`eleven_turbo_v2_5` or `eleven_multilingual_v2`."
            )

    # Base video + music: pick from library, or upload a new one.
    _select_or_upload_base_video(voice_option[1], voice_option[0])
    _select_or_upload_music(voice_option[1], voice_option[0])

    # New input fields for text customization
    text_before = st.text_input("Text Before Customization", "Hi")
    variables_input = st.text_area("Variables (one per line)")
    text_after = st.text_input("Text After Customization", "!")

    # Show example of the final message using the first variable
    variables = [var.strip() for var in variables_input.split("\n") if var.strip()]
    if variables:
        example_message = f"{text_before} {variables[0]} {text_after}"
        st.write(f"Example message: {example_message}")

    # Per-batch override: skip the QA library and regenerate everything fresh.
    force_fresh = st.checkbox(
        "Force fresh generation (ignore QA library)",
        value=False,
        help="When checked, every name is regenerated from scratch — useful "
             "if a previous QA'ed version went bad or settings changed.",
    )
    use_qa_library = not force_fresh

    # Live QA split preview — recalculates as the user types/changes inputs.
    qa_split = _compute_qa_split(variables, voice_option[1], use_qa_library)
    if variables:
        if force_fresh:
            st.warning(
                f"⚠️ Force-fresh is on — all {len(variables)} names will be "
                "regenerated, even if they're in the QA library."
            )
        elif qa_split["enabled"] and qa_split["already_qaed"]:
            with st.container():
                st.success(
                    f"✅ {len(qa_split['already_qaed'])} of {len(variables)} names "
                    f"are already QA'ed — only {len(qa_split['needs_generation'])} "
                    "will be generated fresh."
                )
                qa_names = [item["variable"] for item in qa_split["already_qaed"]]
                with st.expander(
                    f"Show the {len(qa_names)} already-QA'ed names",
                    expanded=False,
                ):
                    st.markdown(
                        "**Already QA'ed (will be reused):** "
                        + ", ".join(f"`{n}`" for n in qa_names)
                    )
                    if qa_split["needs_generation"]:
                        st.markdown(
                            "**Will be freshly generated:** "
                            + ", ".join(f"`{n}`" for n in qa_split["needs_generation"])
                        )
                    sample = qa_split["already_qaed"][0]
                    st.caption(f"Preview of **{sample['variable']}** from the library")
                    if os.path.isfile(sample["file_path"]):
                        st.video(sample["file_path"])
                    else:
                        st.warning(
                            f"Library file not found at {sample['file_path']}. "
                            "(Phase 0 seed may still be syncing.)"
                        )
        elif qa_split["enabled"]:
            st.info(
                f"ℹ️ None of these {len(variables)} names are in the QA library — "
                "all will be generated fresh."
            )

    clip_start = st.number_input(
        "Amount to clip from the start of the video", min_value=0.0, value=1.0
    )

    # Add volume sliders for each audio component with additional instructions
    st.markdown("**Voiceover Volume**")
    st.caption("0 is the volume you upload at")
    voiceover_volume = st.slider(
        "Voiceover Volume", min_value=-100, max_value=100, value=0, step=5
    )

    st.markdown("**Variable Audio Volume**")
    st.caption("0 is the volume you upload at")
    variable_audio_volume = st.slider(
        "Variable Audio Volume", min_value=-100, max_value=100, value=0, step=5
    )

    st.markdown("**Music Volume**")
    st.caption("0 is the volume you upload at")
    music_volume = st.slider(
        "Music Volume", min_value=-100, max_value=100, value=0, step=5
    )

    # Convert slider values to a scale factor
    voiceover_volume_factor = 10 ** (voiceover_volume / 20)
    variable_audio_volume_factor = 10 ** (variable_audio_volume / 20)
    music_volume_factor = 10 ** (music_volume / 20)

    # Pick the first name that ISN'T already in the QA library so the test
    # buttons exercise the fresh-generation path; fall back to variables[0]
    # if every name is already QA'ed.
    if variables:
        if qa_split["enabled"] and qa_split["needs_generation"]:
            test_variable = qa_split["needs_generation"][0]
        else:
            test_variable = variables[0]
    else:
        test_variable = None

    # Add a "Generate Test Audio" button
    if st.button("Generate Test Audio"):
        if not has_base_video() or not has_music() or not variables_input:
            st.error("Please provide all inputs.")
        else:
            with st.spinner("Generating test audio..."):
                input_folder, output_folder = get_session_paths()
                os.makedirs(input_folder, exist_ok=True)
                os.makedirs(output_folder, exist_ok=True)

                # Save base video + music (from library or upload)
                base_video_path = os.path.join(input_folder, "base_video.mp4")
                with open(base_video_path, "wb") as f:
                    f.write(read_base_video_bytes())

                music_path = os.path.join(input_folder, "music.wav")
                with open(music_path, "wb") as f:
                    f.write(read_music_bytes())

                # Create greetings folder
                greetings_folder = os.path.join(input_folder, "greetings")
                os.makedirs(greetings_folder, exist_ok=True)

                # Generate greeting for the chosen test name (skips library
                # picks so we hear fresh TTS output).
                if test_variable:
                    greeting_text = f"{text_before} {test_variable} {text_after}"
                    audio_filename = text_to_speech_file(
                        client,
                        greeting_text,
                        test_variable,
                        greetings_folder,
                        voice_option[1],
                        pronunciation_dict,
                        model_id=selected_model_id,
                    )

                    # Create the full audio track
                    video = VideoFileClip(base_video_path)
                    audio_path = os.path.join(greetings_folder, f"{test_variable}.mp3")
                    final_audio = create_audio_clip(
                        audio_path,
                        video,
                        clip_start,
                        variable_audio_volume_factor,
                        voiceover_volume_factor,
                        music_path,
                        music_volume_factor,
                    )
                    test_audio_path = os.path.join(
                        output_folder, f"test_{test_variable}.mp3"
                    )
                    final_audio.write_audiofile(test_audio_path, fps=44100)

                    st.success(f"Test audio generated for **{test_variable}**.")
                    st.audio(test_audio_path)

    # Add a "Generate Test Video" button
    if st.button("Generate Test Video"):
        if not has_base_video() or not has_music() or not variables_input:
            st.error("Please provide all inputs.")
        else:
            with st.spinner("Generating test video..."):
                input_folder, output_folder = get_session_paths()
                os.makedirs(input_folder, exist_ok=True)
                os.makedirs(output_folder, exist_ok=True)

                # Save base video + music (from library or upload)
                base_video_path = os.path.join(input_folder, "base_video.mp4")
                with open(base_video_path, "wb") as f:
                    f.write(read_base_video_bytes())

                music_path = os.path.join(input_folder, "music.wav")
                with open(music_path, "wb") as f:
                    f.write(read_music_bytes())

                # Create greetings folder
                greetings_folder = os.path.join(input_folder, "greetings")
                os.makedirs(greetings_folder, exist_ok=True)

                # Use the audio for the chosen test name (from Generate Test
                # Audio) to assemble the test video.
                if test_variable:
                    video = VideoFileClip(base_video_path)
                    audio_path = os.path.join(greetings_folder, f"{test_variable}.mp3")
                    final_audio = create_audio_clip(
                        audio_path,
                        video,
                        clip_start,
                        variable_audio_volume_factor,
                        voiceover_volume_factor,
                        music_path,
                        music_volume_factor,
                    )
                    final_video = video.set_audio(final_audio)
                    test_output_filename = f"test_{test_variable}.mp4"
                    test_output_path = os.path.join(output_folder, test_output_filename)
                    final_video.write_videofile(
                        test_output_path, codec="libx264", audio_codec="aac"
                    )

                    st.success(f"Test video generated for **{test_variable}**.")
                    st.video(test_output_path)

    # Move the "Generate Videos" button here
    if st.button("Generate Videos"):
        wave_name_clean = new_wave_name.strip()
        bv_src_check = st.session_state.get("_bv_source") or {}
        if not has_base_video() or not has_music() or not variables_input:
            st.error("Please provide all inputs.")
        elif not wave_name_clean:
            st.error("Please give the wave a name first (top of the page).")
        elif not bv_src_check.get("id"):
            st.error(
                "Generate requires a base video that's in the library. Either "
                "select one from the library, or upload one that matches an "
                "existing entry. (For brand-new base videos, we'll add an "
                "'Add to library' step later.)"
            )
        elif db.get_wave_by_name(wave_name_clean):
            st.error(
                f"A wave named **{wave_name_clean}** already exists. Pick a "
                "different name, or resume it from the Wave dropdown."
            )
        else:
            split = _compute_qa_split(variables, voice_option[1], use_qa_library)
            with st.spinner("Generating videos..."):
                input_folder, output_folder = get_session_paths()
                os.makedirs(input_folder, exist_ok=True)
                os.makedirs(output_folder, exist_ok=True)

                bv_src_for_wave = st.session_state.get("_bv_source") or {}
                music_src_for_wave = st.session_state.get("_music_source") or {}
                wave_id = db.create_wave(
                    name=wave_name_clean,
                    voice_id=voice_option[1],
                    voice_name=voice_option[0],
                    base_video_id=bv_src_for_wave["id"],
                    music_id=music_src_for_wave.get("id"),
                    text_before=text_before,
                    text_after=text_after,
                    settings={
                        "clip_start": float(clip_start),
                        "voiceover_volume": float(voiceover_volume),
                        "variable_audio_volume": float(variable_audio_volume),
                        "music_volume": float(music_volume),
                        "model_id": selected_model_id,
                    },
                )
                wave_storage = db.waves_dir() / str(wave_id)
                wave_storage.mkdir(parents=True, exist_ok=True)

                # Save base video + music (from library or upload)
                base_video_path = os.path.join(input_folder, "base_video.mp4")
                with open(base_video_path, "wb") as f:
                    f.write(read_base_video_bytes())

                music_path = os.path.join(input_folder, "music.wav")
                with open(music_path, "wb") as f:
                    f.write(read_music_bytes())

                # Generate TTS only for names that aren't in the QA library.
                to_generate = split["needs_generation"]
                already_qaed = split["already_qaed"]

                greetings_folder = os.path.join(input_folder, "greetings")
                os.makedirs(greetings_folder, exist_ok=True)
                for variable in to_generate:
                    greeting_text = f"{text_before} {variable} {text_after}"
                    text_to_speech_file(
                        client,
                        greeting_text,
                        variable,
                        greetings_folder,
                        voice_option[1],
                        pronunciation_dict,
                        model_id=selected_model_id,
                    )

                zip_filename = os.path.join(output_folder, "rendered_videos.zip")

                # manifest.json carries voice/base_video/music context so the
                # uploader can re-register QA'ed videos to the same library
                # entry without asking the user to pick again.
                bv_src = st.session_state.get("_bv_source") or {}
                music_src = st.session_state.get("_music_source") or {}
                manifest = {
                    "voice_id": voice_option[1],
                    "voice_name": voice_option[0],
                    "base_video_id": bv_src.get("id"),
                    "base_video_name": bv_src.get("name"),
                    "music_id": music_src.get("id"),
                    "music_name": music_src.get("name"),
                    "text_before": text_before,
                    "text_after": text_after,
                    "needs_qa_variables": list(to_generate),
                    "already_qaed_variables": [it["variable"] for it in already_qaed],
                }

                # Two-folder layout: 'already_qaed/' (copied from library) and
                # 'needs_qa/' (newly generated, awaiting QA).
                with zipfile.ZipFile(zip_filename, "w") as zipf:
                    import json as _json_for_manifest
                    zipf.writestr("manifest.json", _json_for_manifest.dumps(manifest, indent=2))
                    if to_generate:
                        video = VideoFileClip(base_video_path)
                        try:
                            for audio_filename in os.listdir(greetings_folder):
                                if not audio_filename.endswith(".mp3") or audio_filename == ".mp3":
                                    continue
                                audio_path = os.path.join(greetings_folder, audio_filename)
                                final_audio = create_audio_clip(
                                    audio_path,
                                    video,
                                    clip_start,
                                    variable_audio_volume_factor,
                                    voiceover_volume_factor,
                                    music_path,
                                    music_volume_factor,
                                )
                                final_video = video.set_audio(final_audio)
                                output_filename = f"{os.path.splitext(audio_filename)[0]}.mp4"
                                output_path = os.path.join(output_folder, output_filename)
                                final_video.write_videofile(
                                    output_path, codec="libx264", audio_codec="aac"
                                )
                                arcname = f"needs_qa/{output_filename}" if already_qaed else output_filename
                                zipf.write(output_path, arcname=arcname)
                                # Persist into the wave folder + register a
                                # pending wave_video for QA.
                                variable_name = os.path.splitext(output_filename)[0]
                                wave_file = wave_storage / output_filename
                                shutil.copy2(output_path, wave_file)
                                db.add_wave_video(
                                    wave_id=wave_id,
                                    variable=variable_name,
                                    file_path=str(wave_file),
                                    from_library=False,
                                )
                        finally:
                            video.close()

                    for item in already_qaed:
                        src = item["file_path"]
                        arcname = f"already_qaed/{item['variable']}.mp4"
                        if os.path.isfile(src):
                            zipf.write(src, arcname=arcname)
                            db.add_wave_video(
                                wave_id=wave_id,
                                variable=item["variable"],
                                file_path=src,
                                from_library=True,
                            )

                # Cleanup
                for audio_filename in os.listdir(greetings_folder):
                    file_path = os.path.join(greetings_folder, audio_filename)
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                if os.path.isfile(base_video_path):
                    os.remove(base_video_path)
                if os.path.isfile(music_path):
                    os.remove(music_path)

                # Surface the split result so the user knows what happened.
                if already_qaed:
                    summary = (
                        f"Wave **{wave_name_clean}** saved. "
                        f"Generated {len(to_generate)} new videos, "
                        f"reused {len(already_qaed)} from the QA library. "
                        "Switch to the wave from the Wave dropdown above to QA "
                        "the new videos."
                    )
                else:
                    summary = (
                        f"Wave **{wave_name_clean}** saved with "
                        f"{len(to_generate)} videos. Switch to the wave from "
                        "the Wave dropdown above to QA them."
                    )

                # Stash the zip path + the inputs signature so the download
                # button survives unrelated reruns. A later input change
                # (different name, base video, volume, etc.) will produce a
                # different signature and the button will disappear.
                st.session_state["_last_zip_path"] = zip_filename
                st.session_state["_last_zip_summary"] = summary
                st.session_state["_last_zip_signature"] = _generation_signature(
                    voice_id=voice_option[1], variables=variables,
                    text_before=text_before, text_after=text_after,
                    clip_start=clip_start, voiceover_volume=voiceover_volume,
                    variable_audio_volume=variable_audio_volume,
                    music_volume=music_volume, force_fresh=force_fresh,
                )

    # Persistent download — survives until any input that would change the
    # zip's contents changes (then the signature stops matching and the
    # button disappears, prompting a fresh Generate Videos click).
    if variables and st.session_state.get("_last_zip_path"):
        try:
            current_sig = _generation_signature(
                voice_id=voice_option[1], variables=variables,
                text_before=text_before, text_after=text_after,
                clip_start=clip_start, voiceover_volume=voiceover_volume,
                variable_audio_volume=variable_audio_volume,
                music_volume=music_volume, force_fresh=force_fresh,
            )
        except Exception:
            current_sig = None
        last_sig = st.session_state.get("_last_zip_signature")
        zip_path = st.session_state.get("_last_zip_path")
        if last_sig == current_sig and zip_path and os.path.isfile(zip_path):
            st.success(st.session_state.get("_last_zip_summary", "Ready to download."))
            with open(zip_path, "rb") as f:
                st.download_button(
                    "Download Rendered Videos",
                    data=f.read(),
                    file_name="rendered_videos.zip",
                    key="persistent_dl",
                )

    # -----------------------------------------------------------------
    # Phase 4: import QA'ed videos back into the library.
    # -----------------------------------------------------------------
    st.divider()
    st.subheader("Upload QA'ed videos to the library")
    st.caption(
        "When you (or whoever did the QA) approve a batch, drop the zip here "
        "to add the approved videos to the library. Future generations for the "
        "same voice / base video / music will reuse them automatically."
    )
    uploaded_qa_zip = st.file_uploader(
        "Upload a zip of QA'ed videos",
        type=["zip"],
        key="qa_upload",
        help="Should be the zip you downloaded earlier (contains manifest.json). "
             "Videos in `needs_qa/` and any loose .mp4 at the top level get "
             "imported. Files in `already_qaed/` are skipped (already in the library).",
    )
    if uploaded_qa_zip is not None:
        _handle_qa_upload(uploaded_qa_zip)
