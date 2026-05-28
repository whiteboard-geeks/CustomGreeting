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


# Function to generate greeting and save as MP3
def text_to_speech_file(
    client,
    text: str,
    name: str,
    output_folder: str,
    voice_id: str,
    pronunciation_dict=None,
) -> str:
    kwargs = {
        "voice_id": voice_id,
        "output_format": "mp3_44100_192",
        "text": text,
        "language_code": "en",
        "model_id": "eleven_multilingual_v2",
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


# Add a select box for voice selection
voice_option = st.selectbox(
    "Select Voice",
    options=[
        ("Barbara Pigg", "Ro4VVDudw85O3XfD3nva", "primary"),
        ("ZTS07a VO", "LEbsUt7al3JBfWgXlFFc", "primary"),
        ("April Lowrie Pro", "Ww6IPT0jYNzyTUBnXTDG", "alt"),
        ("Dale - YourCause", "xUeVfoX4TgqQTP9P2Rns", "alt"),
        ("Mark Davis", "gVSJ0zoFJ1Wgov5gD1Pi", "alt"),
    ],
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
                with st.expander(f"Preview an already-QA'ed video", expanded=False):
                    sample = qa_split["already_qaed"][0]
                    st.caption(f"Showing **{sample['variable']}** from the library")
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

                # Generate greeting for the first variable
                if variables:
                    greeting_text = f"{text_before} {variables[0]} {text_after}"
                    audio_filename = text_to_speech_file(
                        client,
                        greeting_text,
                        variables[0],  # Use the first variable as the filename
                        greetings_folder,
                        voice_option[1],
                        pronunciation_dict,
                    )

                    # Create the full audio track
                    video = VideoFileClip(base_video_path)
                    audio_path = os.path.join(greetings_folder, f"{variables[0]}.mp3")
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
                        output_folder, f"test_{variables[0]}.mp3"
                    )
                    final_audio.write_audiofile(test_audio_path, fps=44100)

                    st.success("Test audio generated successfully!")
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

                # Use the existing audio file to create a test video
                if variables:
                    video = VideoFileClip(base_video_path)
                    audio_path = os.path.join(greetings_folder, f"{variables[0]}.mp3")
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
                    test_output_filename = f"test_{variables[0]}.mp4"
                    test_output_path = os.path.join(output_folder, test_output_filename)
                    final_video.write_videofile(
                        test_output_path, codec="libx264", audio_codec="aac"
                    )

                    st.success("Test video generated successfully!")
                    st.video(test_output_path)

    # Move the "Generate Videos" button here
    if st.button("Generate Videos"):
        if not has_base_video() or not has_music() or not variables_input:
            st.error("Please provide all inputs.")
        else:
            split = _compute_qa_split(variables, voice_option[1], use_qa_library)
            with st.spinner("Generating videos..."):
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
                    )

                zip_filename = os.path.join(output_folder, "rendered_videos.zip")

                # Two-folder layout: 'already_qaed/' (copied from library) and
                # 'needs_qa/' (newly generated, awaiting QA).
                with zipfile.ZipFile(zip_filename, "w") as zipf:
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
                        finally:
                            video.close()

                    for item in already_qaed:
                        src = item["file_path"]
                        arcname = f"already_qaed/{item['variable']}.mp4"
                        if os.path.isfile(src):
                            zipf.write(src, arcname=arcname)

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
                    st.success(
                        f"Generated {len(to_generate)} new videos, "
                        f"reused {len(already_qaed)} from the QA library. "
                        "Zip contains `needs_qa/` and `already_qaed/` folders."
                    )
                else:
                    st.success(f"Generated {len(to_generate)} videos.")

                st.download_button(
                    "Download Rendered Videos",
                    data=open(zip_filename, "rb"),
                    file_name="rendered_videos.zip",
                )
