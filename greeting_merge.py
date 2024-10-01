import os
from moviepy.editor import VideoFileClip, AudioFileClip, concatenate_audioclips

# Define paths
input_folder = 'input'
audio_folder = os.path.join(input_folder, 'audio')
video_file = os.path.join(input_folder, 'Mckesson_Land+Expand.mp4')

# Load the video file
video = VideoFileClip(video_file)

# Process each audio file in the audio folder
for audio_filename in os.listdir(audio_folder):
    if audio_filename.endswith('.mp3'):
        audio_path = os.path.join(audio_folder, audio_filename)
        
        # Load the audio file
        audio = AudioFileClip(audio_path)
        
        # Concatenate the audio with the video's audio
        final_audio = concatenate_audioclips([audio, video.audio])
        
        # Set the new audio to the video
        final_video = video.set_audio(final_audio)
        
        # Define the output filename
        output_filename = f"{os.path.splitext(audio_filename)[0]}_Mckesson_Land+Expand.mp4"
        output_path = os.path.join(input_folder, output_filename)
        
        # Write the final video to a file
        final_video.write_videofile(output_path, codec='libx264', audio_codec='aac')

print("Processing complete!")