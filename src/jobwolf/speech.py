"""Ephemeral local dictation, independent of chat, documents, and persistence.

Only the composer's bounded PCM WAV format is accepted. Audio stays in memory;
llama.cpp runs for one request with no tools, logging, or persistent prompt cache.
This is a local adapter, not an OS sandbox against other processes of the user.
"""

from __future__ import annotations

import base64
import binascii
import http.client
import json
import os
import platform
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from .speech_assets import (
    MODEL_ASSETS,
    MODEL_FILENAME,
    MODEL_LABEL,
    PROJECTOR_FILENAME,
    RUNTIME_DIR_NAME,
)

SAMPLE_RATE = 16_000
MAX_SECONDS = 120
MAX_TEXT_CHARS = 12_000
MAX_AUDIO_BYTES = 44 + SAMPLE_RATE * 2 * MAX_SECONDS
MAX_BASE64_CHARS = 4 * ((MAX_AUDIO_BYTES + 2) // 3)
MAX_RESPONSE_BYTES = 128 * 1024
TRANSCRIBE_SECONDS = 225  # Leave time for cleanup before the browser's 240s deadline.
STARTUP_SECONDS = 90
TRANSCRIPTION_PROMPT = "transcribe the speech with proper punctuation and capitalization."


class SpeechError(Exception):
    """Safe-to-display failure; never include runtime output or private audio/text."""

    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.status_code = status_code


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    audio_base64: str = Field(min_length=1, max_length=MAX_BASE64_CHARS, repr=False)


class SpeechStatus(BaseModel):
    available: bool
    model: str = MODEL_LABEL
    max_seconds: int = MAX_SECONDS
    sample_rate: int = SAMPLE_RATE
    detail: str


class SpeechResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    text: str = Field(max_length=MAX_TEXT_CHARS, repr=False)
    audio_seconds: float = Field(gt=0, le=MAX_SECONDS)
    elapsed_seconds: float = Field(ge=0)
    model: str = MODEL_LABEL


@dataclass(frozen=True, repr=False)
class SpeechAudio:
    wav: bytes
    seconds: float


class SpeechProvider(Protocol):
    def status(self) -> SpeechStatus: ...
    def transcribe(self, audio: SpeechAudio) -> SpeechResult: ...


def unavailable_status() -> SpeechStatus:
    return SpeechStatus(available=False, detail="Local speech is not configured. Run setup-speech.cmd and restart Pip.")


def decode_audio(encoded: str) -> SpeechAudio:
    """Accept only the recorder's canonical header; no compressed/media decoder."""
    invalid = "Invalid recording. Use Dictate to record mono 16 kHz PCM16 audio, up to two minutes."
    if not isinstance(encoded, str) or not 1 <= len(encoded) <= MAX_BASE64_CHARS:
        raise SpeechError(invalid)
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise SpeechError(invalid) from None
    if not 44 < len(raw) <= MAX_AUDIO_BYTES:
        raise SpeechError(invalid)
    if (raw[:4], raw[8:16], raw[36:40]) != (b"RIFF", b"WAVEfmt ", b"data"):
        raise SpeechError(invalid)
    riff_size = struct.unpack_from("<I", raw, 4)[0]
    format_fields = struct.unpack_from("<IHHIIHH", raw, 16)
    data_size = struct.unpack_from("<I", raw, 40)[0]
    if (
        riff_size != len(raw) - 8 or data_size != len(raw) - 44 or data_size % 2
        or format_fields != (16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16)
    ):
        raise SpeechError(invalid)
    seconds = data_size / (SAMPLE_RATE * 2)
    if seconds < 0.25:
        raise SpeechError("The recording is too short. Speak for at least a moment, then stop and transcribe.")
    # A level guard, not voice detection: noise can still produce wrong words.
    if not any(abs(sample[0]) > 32 for sample in struct.iter_unpack("<h", memoryview(raw)[44:])):
        raise SpeechError("No audible audio was captured. Check your microphone and try again.")
    return SpeechAudio(raw, seconds)


def _runtime_environment(runtime: Path, key: str) -> dict[str, str]:
    # Do not inherit LLAMA_*, provider keys, proxy settings, or user tool/config
    # overrides. Windows and the GPU driver still need their ordinary OS paths.
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA"}
    environment = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    windows = next((value for name, value in environment.items() if name.upper() == "SYSTEMROOT"), "")
    environment["PATH"] = str(runtime) + (os.pathsep + str(Path(windows) / "System32") if windows else "")
    environment["LLAMA_API_KEY"] = key
    return environment


def _request(port: int, key: str, path: str, *, timeout: float, body: dict | None = None) -> tuple[int, dict]:
    # Numeric loopback only. http.client neither uses proxies/credentials nor
    # follows redirects. No runtime-supplied URL is ever requested.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=max(0.01, timeout))
    try:
        headers = {"Authorization": "Bearer " + key, "Accept": "application/json", "Connection": "close"}
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request("POST" if body is not None else "GET", path, body=payload, headers=headers)
        with connection.getresponse() as response:
            if response.status not in (200, 503):
                raise SpeechError("The local speech runtime rejected the request. Check setup or try a shorter recording.", 502)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise SpeechError("The local speech response exceeded its size limit. Try a shorter recording.", 502)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise SpeechError("The local speech runtime returned an invalid response.", 502)
            return response.status, value
    finally:
        connection.close()


def _transcript(reply: dict) -> str:
    invalid = "Granite did not return a complete transcript. Try a shorter recording; your draft is unchanged."
    choices = reply.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise SpeechError(invalid, 502)
    choice = choices[0]
    message = choice.get("message")
    if (
        choice.get("finish_reason") != "stop" or not isinstance(message, dict)
        or message.get("role") != "assistant" or message.get("tool_calls")
        or message.get("reasoning_content") or message.get("refusal")
        or not isinstance(message.get("content"), str)
    ):
        raise SpeechError(invalid, 502)
    text = message["content"]
    if len(text) > MAX_TEXT_CHARS or any(ord(char) < 32 and char not in "\n\r\t" for char in text):
        raise SpeechError(invalid, 502)
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise SpeechError(invalid, 502) from None
    # No summary, inferred corrections, translation, or second-model cleanup.
    return text.strip()


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class GraniteSpeech:
    def __init__(self, directory: Path):
        self.directory = directory.absolute()
        self.runtime = self.directory / RUNTIME_DIR_NAME
        self.executable = self.runtime / "llama-server.exe"

    def status(self) -> SpeechStatus:
        if sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64"):
            return SpeechStatus(available=False, detail="This speech runtime requires Windows x64.")
        try:
            present = self.executable.is_file() and (self.runtime / "ggml-vulkan.dll").is_file()
            present = present and all(
                (self.directory / "models" / asset.filename).stat().st_size == asset.size
                for asset in MODEL_ASSETS
            )
        except OSError:
            present = False
        return SpeechStatus(
            available=present,
            detail=("Local assets are present; the model loads only when you transcribe. GPU readiness is checked then."
                    if present else "Granite speech assets are missing or incomplete. Run setup-speech.cmd."),
        )

    def _command(self, port: int) -> list[str]:
        return [
            str(self.executable),
            "--model", str(self.directory / "models" / MODEL_FILENAME),
            "--mmproj", str(self.directory / "models" / PROJECTOR_FILENAME),
            "--host", "127.0.0.1", "--port", str(port), "--alias", "pip-granite-speech",
            "--gpu-layers", "all", "--ctx-size", "8192", "--parallel", "1",
            "--n-predict", "4096", "--no-context-shift", "--no-warmup",
            "--offline", "--no-webui", "--no-agent", "--no-slots",
            "--no-cache-prompt", "--cache-ram", "0", "--log-disable",
            "--timeout", str(TRANSCRIBE_SECONDS),
        ]

    def _wait_ready(self, process: subprocess.Popen, port: int, key: str, deadline: float) -> None:
        startup_end = min(deadline, time.monotonic() + STARTUP_SECONDS)
        while time.monotonic() < startup_end:
            if process.poll() is not None:
                raise SpeechError(
                    "Granite could not start. Run setup-speech.cmd to verify assets, check your Vulkan graphics driver, "
                    "and free GPU memory (for example, unload an idle Ollama model). No cloud fallback was used.", 503,
                )
            try:
                status, body = _request(port, key, "/health", timeout=min(1, startup_end - time.monotonic()))
                if status == 200 and body.get("status") == "ok" and process.poll() is None:
                    return
            except (OSError, http.client.HTTPException):
                pass  # The owned child may not have bound its socket yet.
            time.sleep(0.15)
        raise SpeechError("Granite took too long to load. Free GPU memory or check speech setup, then retry.", 504)

    def transcribe(self, audio: SpeechAudio) -> SpeechResult:
        status = self.status()
        if not status.available:
            raise SpeechError(status.detail, 503)
        started = time.monotonic()
        deadline = started + TRANSCRIBE_SECONDS
        key = secrets.token_urlsafe(32)
        process = None
        watchdog = None
        expired = threading.Event()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
                if sys.platform == "win32":
                    reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            process = subprocess.Popen(
                self._command(port), cwd=self.runtime, env=_runtime_environment(self.runtime, key),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            def expire() -> None:
                expired.set()
                try:
                    if process.poll() is None:
                        process.kill()
                except OSError:
                    pass  # It may have exited between poll and kill.

            watchdog = threading.Timer(max(0, deadline - time.monotonic()), expire)
            watchdog.daemon = True
            watchdog.start()
            self._wait_ready(process, port, key, deadline)
            payload = {
                "model": "pip-granite-speech", "stream": False, "temperature": 0,
                "max_tokens": 4096, "cache_prompt": False,
                "messages": [{"role": "user", "content": [
                    {"type": "input_audio", "input_audio": {
                        "data": base64.b64encode(audio.wav).decode("ascii"), "format": "wav",
                    }},
                    {"type": "text", "text": TRANSCRIPTION_PROMPT},
                ]}],
            }
            code, reply = _request(port, key, "/v1/chat/completions", timeout=deadline - time.monotonic(), body=payload)
            if expired.is_set() or time.monotonic() >= deadline:
                raise SpeechError("Local transcription timed out. Try a shorter recording; your draft is unchanged.", 504)
            if code != 200:
                raise SpeechError("The local speech runtime was not ready. Try again after checking setup.", 503)
            text = _transcript(reply)
        except (OSError, http.client.HTTPException, ValueError):
            if expired.is_set() or time.monotonic() >= deadline:
                raise SpeechError("Local transcription timed out. Try a shorter recording; your draft is unchanged.", 504) from None
            raise SpeechError("Local speech failed. Check speech setup, Vulkan support, and free GPU memory. Your draft is unchanged.", 502) from None
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if process is not None:
                _stop_process(process)
        return SpeechResult(text=text, audio_seconds=audio.seconds, elapsed_seconds=time.monotonic() - started)
