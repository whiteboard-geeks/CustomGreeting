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


# Streamlit UI
st.set_page_config(page_title="Video Greeting Generator", page_icon="🎬")
st.title("Video Greeting Generator")

# Add a select box for voice selection
voice_option = st.selectbox(
    "Select Voice",
    options=[
        ("Barbara Pigg", "Ro4VVDudw85O3XfD3nva", "primary"),
        ("ZTS07a VO", "LEbsUt7al3JBfWgXlFFc", "primary"),
        ("April Lowrie Pro", "Ww6IPT0jYNzyTUBnXTDG", "alt"),
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

    # Continue with other file uploaders
    base_video = st.file_uploader("Upload Base Video", type=["mp4"])
    music = st.file_uploader("Upload Music", type=["wav"])

    # New input fields for text customization
    text_before = st.text_input("Text Before Customization", "Hi")
    variables_input = st.text_area("Variables (one per line)")
    text_after = st.text_input("Text After Customization", "!")

    # Show example of the final message using the first variable
    variables = [var.strip() for var in variables_input.split("\n") if var.strip()]
    if variables:
        example_message = f"{text_before} {variables[0]} {text_after}"
        st.write(f"Example message: {example_message}")

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
        if not base_video or not variables_input:
            st.error("Please provide all inputs.")
        else:
            with st.spinner("Generating test audio..."):
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
        if not base_video or not variables_input:
            st.error("Please provide all inputs.")
        else:
            with st.spinner("Generating test video..."):
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
        if not base_video or not variables_input:
            st.error("Please provide all inputs.")
        else:
            with st.spinner("Generating videos..."):
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

                # Generate greetings
                greetings_folder = os.path.join(input_folder, "greetings")
                os.makedirs(greetings_folder, exist_ok=True)
                for variable in variables:
                    greeting_text = f"{text_before} {variable} {text_after}"
                    text_to_speech_file(
                        client,
                        greeting_text,
                        variable,  # Use variable as the filename
                        greetings_folder,
                        voice_option[1],
                        pronunciation_dict,
                    )

                # Initialize progress bar
                total_videos = len(variables)

                # Process each audio file and create videos
                video = VideoFileClip(base_video_path)
                zip_filename = os.path.join(output_folder, "rendered_videos.zip")
                progress_counter = 0

                with zipfile.ZipFile(zip_filename, "w") as zipf:
                    for idx, audio_filename in enumerate(os.listdir(greetings_folder)):
                        if (
                            audio_filename.endswith(".mp3")
                            and not audio_filename == ".mp3"
                        ):
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
