import os
import zipfile
import streamlit as st
import pandas as pd
from moviepy.editor import (
    AudioFileClip,
    AudioClip,
    concatenate_audioclips,
    CompositeAudioClip,
    VideoFileClip,
)
from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs


# Function to create a one-second silent audio clip
def create_silence(duration=1):
    return AudioClip(lambda t: 0, duration=duration)


# Function to generate greeting and save as MP3
def text_to_speech_file(client, text: str, name: str, output_folder: str) -> str:
    response = client.text_to_speech.convert(
        voice_id="Ro4VVDudw85O3XfD3nva",  # Barbara Pigg 1
        output_format="mp3_22050_32",
        text=text,
        model_id="eleven_multilingual_v2",
        voice_settings=VoiceSettings(
            stability=0.6,
            similarity_boost=0.9,
            style=0.1,
            use_speaker_boost=True,
        ),
    )

    save_file_path = os.path.join(output_folder, f"{name}.mp3")
    with open(save_file_path, "wb") as f:
        for chunk in response:
            if chunk:
                f.write(chunk)

    return save_file_path


# Streamlit UI
st.set_page_config(page_title="Video Greeting Generator", page_icon="🎬")
st.title("Video Greeting Generator")

# Read API key from environment variable
api_key = st.secrets["ELEVENLABS_API_KEY"]
if not api_key:
    st.error("ELEVENLABS_API_KEY environment variable not set.")
else:
    base_video = st.file_uploader("Upload Base Video", type=["mp4"])
    music = st.file_uploader("Upload Music", type=["wav"])
    names_csv = st.file_uploader("Upload CSV of Names", type=["csv"])
    clip_start = st.number_input(
        "Amount to clip from the start of the video", min_value=0.0, value=1.0
    )

    if st.button("Generate Videos"):
        if not base_video or not names_csv:
            st.error("Please provide all inputs.")
        else:
            client = ElevenLabs(api_key=api_key)
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

            names_df = pd.read_csv(names_csv)
            names = names_df.iloc[:, 0].tolist()

            # Generate greetings
            greetings_folder = os.path.join(input_folder, "greetings")
            os.makedirs(greetings_folder, exist_ok=True)
            for name in names:
                greeting_text = f"Hi {name}!"
                text_to_speech_file(client, greeting_text, name, greetings_folder)

            # Initialize progress bar
            progress_bar = st.progress(0)
            total_videos = len(names)

            # Process each audio file and create videos
            video = VideoFileClip(base_video_path)
            zip_filename = os.path.join(output_folder, "rendered_videos.zip")
            progress_counter = 0
            with zipfile.ZipFile(zip_filename, "w") as zipf:
                for idx, audio_filename in enumerate(os.listdir(greetings_folder)):
                    if audio_filename.endswith(".mp3") and not audio_filename == ".mp3":
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
                        output_filename = f"{os.path.splitext(audio_filename)[0]}.mp4"
                        output_path = os.path.join(output_folder, output_filename)
                        final_video.write_videofile(
                            output_path, codec="libx264", audio_codec="aac"
                        )
                        progress_counter += 1
                        progress_bar.progress(progress_counter / total_videos)
                        zipf.write(output_path, arcname=output_filename)

            st.success("Processing complete!")
            st.download_button(
                "Download Rendered Videos",
                data=open(zip_filename, "rb"),
                file_name="rendered_videos.zip",
            )
