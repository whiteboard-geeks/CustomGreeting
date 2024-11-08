import os
import zipfile
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
        "model_id": "eleven_monolingual_v1",
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


# Streamlit UI
st.set_page_config(page_title="Video Greeting Generator", page_icon="🎬")
st.title("Video Greeting Generator")

# Add a select box for voice selection
voice_option = st.selectbox(
    "Select Voice",
    options=[
        ("Barbara Pigg 1", "Ro4VVDudw85O3XfD3nva"),
        ("ZTS07a VO", "LEbsUt7al3JBfWgXlFFc"),
    ],
    format_func=lambda x: x[0],
)

# Read API key from environment variable
api_key = st.secrets["ELEVENLABS_API_KEY"]
if not api_key:
    st.error("ELEVENLABS_API_KEY environment variable not set.")
else:
    # Create ElevenLabs client first
    client = ElevenLabs(api_key=api_key)

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

    # Continue with other file uploaders
    base_video = st.file_uploader("Upload Base Video", type=["mp4"])
    music = st.file_uploader("Upload Music", type=["wav"])
    names_input = st.text_area("Enter Names (one per line)")
    clip_start = st.number_input(
        "Amount to clip from the start of the video", min_value=0.0, value=1.0
    )

    if st.button("Generate Videos"):
        if not base_video or not names_input:
            st.error("Please provide all inputs.")
        else:
            input_folder = "input"
            output_folder = "output"
            os.makedirs(input_folder, exist_ok=True)
            os.makedirs(output_folder, exist_ok=True)

            # Save uploaded files
            base_video_path = os.path.join(input_folder, "base_video.mp4")
            with open(base_video_path, "wb") as f:
                f.write(base_video.read())

            music_path = os.path.join(input_folder, "music.wav")
            with open(music_path, "wb") as f:
                f.write(music.read())

            # Parse names input
            names = [name.strip() for name in names_input.split("\n") if name.strip()]

            # Generate greetings
            greetings_folder = os.path.join(input_folder, "greetings")
            os.makedirs(greetings_folder, exist_ok=True)
            for name in names:
                greeting_text = f"{name}!"
                text_to_speech_file(
                    client,
                    greeting_text,
                    name,
                    greetings_folder,
                    voice_option[1],
                    pronunciation_dict,
                )

            # Initialize progress bar
            total_videos = len(names)

            # Process each audio file and create videos
            video = VideoFileClip(base_video_path)
            zip_filename = os.path.join(output_folder, "rendered_videos.zip")
            progress_counter = 0

            with st.spinner("Processing videos..."):
                with zipfile.ZipFile(zip_filename, "w") as zipf:
                    for idx, audio_filename in enumerate(os.listdir(greetings_folder)):
                        if (
                            audio_filename.endswith(".mp3")
                            and not audio_filename == ".mp3"
                        ):
                            audio_path = os.path.join(greetings_folder, audio_filename)
                            audio = AudioFileClip(audio_path)
                            video_voiceover_audio = video.audio.subclip(clip_start)
                            voiceover_audio_with_greeting = concatenate_audioclips(
                                [audio, video_voiceover_audio]
                            )
                            silence = create_silence(2)
                            voiceover_audio_with_intro_silence = concatenate_audioclips(
                                [silence, voiceover_audio_with_greeting]
                            )
                            music = AudioFileClip(music_path).volumex(0.05)
                            final_audio = CompositeAudioClip(
                                [voiceover_audio_with_intro_silence, music.set_start(0)]
                            )
                            final_video = video.set_audio(final_audio)
                            output_filename = (
                                f"{os.path.splitext(audio_filename)[0]}.mp4"
                            )
                            output_path = os.path.join(output_folder, output_filename)
                            final_video.write_videofile(
                                output_path, codec="libx264", audio_codec="aac"
                            )
                            progress_counter += 1
                            zipf.write(output_path, arcname=output_filename)

            # Clean up the greetings folder
            for audio_filename in os.listdir(greetings_folder):
                file_path = os.path.join(greetings_folder, audio_filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)

            # Remove the base video and music files
            if os.path.isfile(base_video_path):
                os.remove(base_video_path)
            if os.path.isfile(music_path):
                os.remove(music_path)

            st.success("Processing complete!")
            st.download_button(
                "Download Rendered Videos",
                data=open(zip_filename, "rb"),
                file_name="rendered_videos.zip",
            )
