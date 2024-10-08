import os
from moviepy.editor import (
    AudioFileClip,
    AudioClip,
    concatenate_audioclips,
    CompositeAudioClip,
    VideoFileClip,
)


# Create a one-second silent audio clip
def create_silence(duration=1):
    return AudioClip(lambda t: 0, duration=duration)


# Define paths
input_folder = "input"
video_file = os.path.join(input_folder, "LabCorp_Land+Expand.mp4")
output_folder = "output/LabCorp"
os.makedirs(output_folder, exist_ok=True)

audio_folder = os.path.join(input_folder, "greetings")
# Load the video file
video = VideoFileClip(video_file)

# Process each audio file in the audio folder
for audio_filename in os.listdir(audio_folder):
    if audio_filename.endswith(".mp3"):
        audio_path = os.path.join(audio_folder, audio_filename)

        # Load the audio file
        audio = AudioFileClip(audio_path)

        # Trim a bit the beginning of the video's audio. By trimming 1 second, it leaves about 0.5 seconds of silence between the greeting and the rest of the voiceover.
        video_voiceover_audio = video.audio.subclip(1.5)

        # Concatenate the new audio with the trimmed video audio
        voiceover_audio_with_greeting = concatenate_audioclips(
            [audio, video_voiceover_audio]
        )

        silence = create_silence(2)

        # Concatenate the silence with the new audio
        voiceover_audio_with_intro_silence = concatenate_audioclips(
            [silence, voiceover_audio_with_greeting]
        )

        music = AudioFileClip(os.path.join(input_folder, "music.wav")).volumex(0.05)

        # Composite the music with the voiceover audio
        final_audio = CompositeAudioClip(
            [voiceover_audio_with_intro_silence, music.set_start(0)]
        )

        # Set the new audio to the video
        final_video = video.set_audio(final_audio)

        # Define the output filename
        output_filename = f"{os.path.splitext(audio_filename)[0]}_Land+Expand.mp4"
        output_path = os.path.join(output_folder, output_filename)

        # Write the final video to a file
        final_video.write_videofile(output_path, codec="libx264", audio_codec="aac")

print("Processing complete!")
