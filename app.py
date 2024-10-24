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
from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs
import ffmpeg  # Add this import at the top


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
    base_video = st.file_uploader(
        "Upload Base Video", type=["mp4", "avi"]
    )  # Added 'avi'
    music = st.file_uploader("Upload Music", type=["wav"])
    names_input = st.text_area("Enter Names (one per line)")
    clip_start = st.number_input(
        "Amount to clip from the start of the video", min_value=0.0, value=1.0
    )
    output_format = st.selectbox(
        "Select Output Format",
        options=["mp4", "avi"],
        index=0,
        help="Choose the format for the output videos",
    )

    if st.button("Generate Videos"):
        if not base_video or not names_input:
            st.error("Please provide all inputs.")
        else:
            client = ElevenLabs(api_key=api_key)
            input_folder = "input"
            output_folder = "output"
            os.makedirs(input_folder, exist_ok=True)
            os.makedirs(output_folder, exist_ok=True)

            # Save uploaded files with debug info
            base_video_path = os.path.join(input_folder, "base_video.mp4")
            with open(base_video_path, "wb") as f:
                f.write(base_video.read())

            st.write(f"Debug: Saved video to {base_video_path}")
            st.write(f"Debug: File size: {os.path.getsize(base_video_path)} bytes")

            # Pre-process with FFmpeg to ensure compatibility
            preprocessed_path = os.path.join(input_folder, "preprocessed_video.mp4")
            try:
                # Convert to a standard format that MoviePy handles well
                ffmpeg.input(base_video_path).output(
                    preprocessed_path,
                    vcodec="libx264",
                    acodec="aac",
                    **{"b:v": "2000k"},  # Ensure good quality
                ).overwrite_output().run(capture_stdout=True, capture_stderr=True)

                # Load and verify video
                video = VideoFileClip(preprocessed_path)
                st.write(f"Debug: Video duration: {video.duration} seconds")
                st.write(f"Debug: Video size: {video.size}")
                st.write(f"Debug: Video fps: {video.fps}")

                if video.duration < 0.1:  # Still suspicious duration
                    st.error(
                        "Video appears to not be loading correctly. Please check the file."
                    )
                    video.close()
                    st.stop()

            except Exception as e:
                st.error(f"Error processing video: {str(e)}")
                if os.path.isfile(base_video_path):
                    os.remove(base_video_path)
                if os.path.isfile(preprocessed_path):
                    os.remove(preprocessed_path)
                st.stop()

            music_path = os.path.join(input_folder, "music.wav")
            with open(music_path, "wb") as f:
                f.write(music.read())

            # Parse names input
            names = [name.strip() for name in names_input.split("\n") if name.strip()]

            # Generate greetings
            greetings_folder = os.path.join(input_folder, "greetings")
            os.makedirs(greetings_folder, exist_ok=True)
            for name in names:
                greeting_text = f"Hi {name}!"
                text_to_speech_file(client, greeting_text, name, greetings_folder)

            # Initialize progress bar
            total_videos = len(names)

            # Process each audio file and create videos
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

                            # Export intermediate video with audio
                            temp_output = os.path.join(output_folder, "temp_output.mp4")
                            final_video = video.set_audio(final_audio)
                            final_video.write_videofile(
                                temp_output, codec="libx264", audio_codec="aac"
                            )

                            # Final output path
                            output_filename = (
                                f"{os.path.splitext(audio_filename)[0]}.{output_format}"
                            )
                            output_path = os.path.join(output_folder, output_filename)

                            # Use FFmpeg for final conversion
                            if output_format == "mp4":
                                ffmpeg.input(temp_output).output(
                                    output_path, vcodec="libx264", acodec="aac"
                                ).overwrite_output().run(
                                    capture_stdout=True, capture_stderr=True
                                )
                            else:  # avi format
                                ffmpeg.input(temp_output).output(
                                    output_path,
                                    vcodec="mjpeg",
                                    acodec="adpcm_ima_wav",
                                    video_bitrate="1976k",
                                    audio_bitrate="88k",
                                    s="320x240",
                                    r=21.68,
                                    ar="22050",
                                    ac=1,
                                ).overwrite_output().run(
                                    capture_stdout=True, capture_stderr=True
                                )

                            # Clean up temp file
                            if os.path.exists(temp_output):
                                os.remove(temp_output)

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
