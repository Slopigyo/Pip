"""Opt-in Windows Granite smoke check; no microphone, profile, or private files.

Uses the installed Windows voice to synthesize a fixed sentence into memory.
This checks the runtime/protocol and cold latency, not real-voice ASR quality.
Run: .venv/Scripts/python.exe tests/smoke_speech.py --run
"""

import argparse
import base64
import io
import subprocess
import wave
from pathlib import Path

from jobwolf.speech import GraniteSpeech, decode_audio
from jobwolf.speech_assets import asset_directory


def synthetic_audio() -> str:
    script = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer
$stream = New-Object System.IO.MemoryStream
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
try {
    $voice.SetOutputToAudioStream($stream, $format)
    $voice.Speak('I enjoy fixing bicycles and solving practical problems. I would like a four day work week.')
    $voice.SetOutputToNull()
    [Console]::Write([Convert]::ToBase64String($stream.ToArray()))
} finally {
    $voice.Dispose()
    $stream.Dispose()
}
"""
    encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script],
        capture_output=True, timeout=30, check=True,
    )
    pcm = base64.b64decode(result.stdout.strip(), validate=True)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return base64.b64encode(output.getvalue()).decode("ascii")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Explicitly run the installed local Granite model once.")
    parser.add_argument("--speech-dir", type=Path)
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to opt in to a single synthetic local inference.")
    adapter = GraniteSpeech(args.speech_dir if args.speech_dir is not None else asset_directory())
    print(adapter.status().model_dump_json(), flush=True)
    audio = decode_audio(synthetic_audio())
    print(f"Synthetic audio: {audio.seconds:.2f}s; loading Granite for one request...", flush=True)
    result = adapter.transcribe(audio)
    print(f"Transcript: {result.text}")
    print(f"Cold transcription including load/cleanup: {result.elapsed_seconds:.2f}s for {result.audio_seconds:.2f}s audio")
    if "bicycles" not in result.text.lower() or "practical problems" not in result.text.lower():
        raise SystemExit("Protocol completed but synthetic content was not recognized as expected.")
    print("Synthetic speech smoke passed; model process released. Real-voice assessment remains manual.")


if __name__ == "__main__":
    main()
