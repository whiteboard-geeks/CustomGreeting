import os
import csv
from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs

# Import ElevenLabs API key from environment variable ELEVENLABS_API_KEY
api_key = os.getenv("ELEVENLABS_API_KEY")
client = ElevenLabs(api_key=api_key)

# Import a list of names from input/names.csv. It has a header row. It's the first column.
names = []
with open("input/names.csv", newline="") as csvfile:
    reader = csv.DictReader(csvfile)
    first_col_name = reader.fieldnames[0]
    for row in reader:
        names.append(row[first_col_name])


# Function to generate greeting and save as MP3
def text_to_speech_file(text: str, name: str) -> str:
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

    save_file_path = f"input/greetings/{name}.mp3"
    with open(save_file_path, "wb") as f:
        for chunk in response:
            if chunk:
                f.write(chunk)

    print(f"{save_file_path}: A new audio file was saved successfully!")
    return save_file_path


# Generate and save greeting for each name
for name in names:
    greeting_text = f"Hi {name}!"
    text_to_speech_file(greeting_text, name)
