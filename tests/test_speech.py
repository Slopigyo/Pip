"""Focused speech tests: synthetic audio, mocked runtime, no workspace or network."""

import base64
import io
import json
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from test_app import TOKEN, SyntheticProvider
from test_service import MemoryStore

from jobwolf import speech
from jobwolf.app import create_app


def recording(seconds: float = 1, value=500, rate=16000, channels=1, width=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(struct.pack("<h", value) * (int(seconds * rate) * channels))
    return base64.b64encode(output.getvalue()).decode("ascii")


def completion(text: object = "I enjoy fixing bicycles.", finish="stop"):
    return {"choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": text}}]}


class AudioTests(unittest.TestCase):
    def test_recorder_wav_and_full_two_minutes_are_accepted(self):
        for seconds in (1, 120):
            audio = speech.decode_audio(recording(seconds))
            self.assertEqual(audio.seconds, seconds)
            self.assertEqual(len(audio.wav), 44 + seconds * 32000)
            self.assertNotIn("RIFF", repr(audio))

    def test_invalid_formats_sizes_silence_and_durations_fail(self):
        for encoded in ("not base64!", "https://private.invalid/audio", recording(value=0),
                        recording(.1), recording(121), recording(rate=8000),
                        recording(channels=2), recording(width=1)):
            with self.subTest(length=len(encoded)), self.assertRaises(speech.SpeechError):
                speech.decode_audio(encoded)

    def test_truncated_trailing_and_forged_lengths_fail(self):
        raw = base64.b64decode(recording())
        forged = bytearray(raw)
        struct.pack_into("<I", forged, 40, 2)
        for bad in (raw[:-2], raw + b"extra", bytes(forged), raw[:16] + b"\x12" + raw[17:]):
            with self.assertRaises(speech.SpeechError):
                speech.decode_audio(base64.b64encode(bad).decode())

    def test_incomplete_or_nontext_output_is_never_silently_trimmed(self):
        for reply in (completion(finish="length"), completion(None), completion("x" * 12001),
                      completion("bad\x00text"), completion("\ud800"), {}, {"choices": [None]}):
            with self.assertRaises(speech.SpeechError):
                speech._transcript(reply)
        self.assertEqual(speech._transcript(completion(" I meant Tuesday, not Monday. ")), "I meant Tuesday, not Monday.")
        self.assertEqual(speech._transcript(completion("")), "")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pip-speech-test-")
        self.addCleanup(temporary.cleanup)
        self.adapter = speech.GraniteSpeech(Path(temporary.name) / "absent")
        self.audio = speech.decode_audio(recording())

    def test_status_does_not_create_files_load_model_or_contact_network(self):
        with patch.object(speech.subprocess, "Popen", side_effect=AssertionError), patch.object(speech, "_request", side_effect=AssertionError):
            self.assertFalse(self.adapter.status().available)
        self.assertFalse(self.adapter.directory.exists())

    def test_environment_does_not_inherit_provider_keys_proxies_or_runtime_overrides(self):
        with patch.dict(speech.os.environ, {"LLAMA_ARG_MODEL_URL": "bad", "LLAMA_API_KEY": "old", "HF_TOKEN": "private", "HTTPS_PROXY": "bad", "PATH": "untrusted"}, clear=True):
            environment = speech._runtime_environment(self.adapter.runtime, "new-session-key")
        self.assertEqual(environment, {"PATH": str(self.adapter.runtime), "LLAMA_API_KEY": "new-session-key"})
        command = self.adapter._command(12345)
        for flag in ("--offline", "--no-agent", "--log-disable", "--no-webui", "--no-cache-prompt"):
            self.assertIn(flag, command)
        self.assertNotIn("new-session-key", command)
        self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")

    def run_mocked(self, response=None, error=None, startup_error=None):
        process = Mock()
        process.poll.return_value = None
        reservation = Mock()
        reservation.__enter__ = Mock(return_value=reservation)
        reservation.__exit__ = Mock(return_value=False)
        reservation.getsockname.return_value = ("127.0.0.1", 12345)
        with (
            patch.object(self.adapter, "status", return_value=speech.SpeechStatus(available=True, detail="Synthetic")),
            patch.object(self.adapter, "_wait_ready", side_effect=startup_error),
            patch.object(speech.socket, "socket", return_value=reservation),
            patch.object(speech.subprocess, "Popen", return_value=process) as launch,
            patch.object(speech, "_request", return_value=(200, response or completion()), side_effect=error) as request,
        ):
            try:
                result = self.adapter.transcribe(self.audio)
            finally:
                process.terminate.assert_called_once()
                process.wait.assert_called_once_with(timeout=5)
        return result, request, launch

    def test_success_uses_only_audio_and_fixed_prompt_and_reaps_child(self):
        result, request, launch = self.run_mocked()
        self.assertEqual(result.text, "I enjoy fixing bicycles.")
        self.assertEqual(result.audio_seconds, 1)
        args, kwargs = request.call_args
        self.assertEqual(args[2], "/v1/chat/completions")
        body = kwargs["body"]
        content = body["messages"][0]["content"]
        self.assertEqual(len(body["messages"]), 1)
        self.assertEqual(content[0]["input_audio"]["data"], recording())
        self.assertEqual(content[1]["text"], speech.TRANSCRIPTION_PROMPT)
        self.assertFalse(body["cache_prompt"])
        self.assertNotIn("tools", body)
        self.assertEqual(launch.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("shell", launch.call_args.kwargs)

    def test_network_startup_and_incomplete_output_failures_reap_child(self):
        for kwargs in ({"error": TimeoutError("PRIVATE")}, {"response": completion(finish="length")},
                       {"startup_error": speech.SpeechError("Synthetic failure", 503)}):
            with self.subTest(kwargs=tuple(kwargs)), self.assertRaises(speech.SpeechError) as error:
                self.run_mocked(**kwargs)
            self.assertNotIn("PRIVATE", str(error.exception))

    def test_cleanup_kills_a_child_that_does_not_terminate(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("synthetic", 5), 0]
        speech._stop_process(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_http_transport_is_loopback_bounded_authenticated_and_never_redirects(self):
        connection = Mock()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        connection.getresponse.return_value = response
        response.status = 200
        response.read.return_value = json.dumps(completion()).encode()
        with patch.object(speech.http.client, "HTTPConnection", return_value=connection) as connect:
            speech._request(12345, "synthetic-key", "/v1/chat/completions", timeout=2, body={"synthetic": True})
            connect.assert_called_once_with("127.0.0.1", 12345, timeout=2)
            self.assertEqual(connection.request.call_args.kwargs["headers"]["Authorization"], "Bearer synthetic-key")
            response.read.assert_called_once_with(speech.MAX_RESPONSE_BYTES + 1)
            response.status = 302
            with self.assertRaises(speech.SpeechError):
                speech._request(12345, "synthetic-key", "/health", timeout=2)
        self.assertEqual(connection.close.call_count, 2)


class SpeechRouteTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.provider = SyntheticProvider()
        self.speech = Mock(spec=speech.SpeechProvider)
        self.speech.status.return_value = speech.SpeechStatus(available=True, detail="Synthetic ready")
        self.speech.transcribe.return_value = speech.SpeechResult(text="A synthetic draft.", audio_seconds=1.0, elapsed_seconds=.2)
        app = create_app(store=self.store, provider=self.provider, token=TOKEN, speech=self.speech)
        self.client = TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-JobWolf-Token": TOKEN})
        self.addCleanup(self.client.close)
        self.body = {"audio_base64": recording()}

    def test_status_transcription_do_not_read_or_write_workspace_or_call_chat(self):
        with patch.object(self.store, "load", side_effect=AssertionError), patch.object(self.store, "save", side_effect=AssertionError):
            self.assertTrue(self.client.get("/api/speech/status").json()["available"])
            result = self.client.post("/api/speech/transcribe", json=self.body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["text"], "A synthetic draft.")
        self.assertFalse(self.provider.contexts)
        self.assertEqual(self.speech.transcribe.call_args.args[0].seconds, 1)

    def test_auth_origin_and_microphone_asset_policy(self):
        self.assertEqual(self.client.get("/api/speech/status", headers={"X-JobWolf-Token": "wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/api/speech/transcribe", json=self.body, headers={"Origin": "https://outside.invalid"}).status_code, 403)
        self.speech.transcribe.assert_not_called()
        asset = self.client.get("/assets/speech-recorder.js")
        self.assertEqual(asset.status_code, 200)
        self.assertEqual(asset.headers["permissions-policy"], "camera=(), microphone=(self), geolocation=()")

    def test_invalid_input_and_private_failures_are_nonmutating_and_safe(self):
        for body in ({"audio_base64": "PRIVATE"}, {**self.body, "context": "PRIVATE"}):
            result = self.client.post("/api/speech/transcribe", json=body)
            self.assertIn(result.status_code, (400, 422))
            self.assertFalse(result.json()["state_changed"])
            self.assertNotIn("PRIVATE", result.text)
        self.speech.transcribe.assert_not_called()
        self.speech.transcribe.side_effect = RuntimeError("PRIVATE")
        result = self.client.post("/api/speech/transcribe", json=self.body)
        self.assertEqual(result.status_code, 502)
        self.assertNotIn("PRIVATE", result.text)
        self.assertFalse(result.json()["state_changed"])
        self.speech.transcribe.side_effect = None
        self.assertEqual(self.client.post("/api/speech/transcribe", json=self.body).status_code, 200)
        self.assertEqual(self.store.revision, 0)

    def test_speech_and_ollama_share_inference_lease_without_sync_mutation(self):
        saved = self.client.post("/api/messages", json={"revision": 0, "text": "Synthetic message."}).json()
        generation = {"revision": saved["revision"], "model": "Lyra:latest", "selection": {"message_ids": [saved["workspace"]["messages"][0]["id"]]}}
        nested = []
        def during_speech(audio):
            nested.append(self.client.post("/api/generate", json=generation))
            nested.append(self.client.post("/api/speech/transcribe", json=self.body))
            return speech.SpeechResult(text="Synthetic.", audio_seconds=1.0, elapsed_seconds=0.0)
        self.speech.transcribe.side_effect = during_speech
        self.assertEqual(self.client.post("/api/speech/transcribe", json=self.body).status_code, 200)
        self.assertEqual(nested[0].status_code, 409)
        self.assertEqual(nested[1].status_code, 409)
        self.assertFalse(nested[1].json()["state_changed"])
        self.assertEqual(self.store.revision, saved["revision"])
        self.speech.transcribe.side_effect = None
        self.assertEqual(self.client.post("/api/speech/transcribe", json=self.body).status_code, 200)


if __name__ == "__main__":
    unittest.main()
