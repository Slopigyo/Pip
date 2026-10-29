"""Opt-in Firefox capture regression: fake microphone, temporary profile, no model.

Run: .venv/Scripts/python.exe tests/smoke_speech_firefox.py <firefox-executable>
Serves the real UI/assets/security headers with synthetic, nonpersistent API data.
"""

import argparse
import json
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jobwolf.models import DOMAINS, Workspace
from jobwolf.security import SECURITY_HEADERS
from jobwolf.speech import decode_audio

WEB = Path(__file__).resolve().parents[1] / "src" / "jobwolf" / "web"
TOKEN = "synthetic-firefox-capture-token-only"
SCRIPT = br"""
(async () => {
  const result = {};
  const tracks = [];
  const getUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getUserMedia = async (...args) => {
    const stream = await getUserMedia(...args); tracks.push(...stream.getTracks()); return stream;
  };
  const byId = (id) => document.getElementById(id);
  const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
  async function wait(check, step) {
    const until = Date.now() + 8000;
    while (Date.now() < until) { if (check()) return; await sleep(50); }
    throw new Error('Timed out at ' + step + ': ' + byId('speech-status').textContent);
  }
  try {
    await wait(() => !byId('main').hidden && byId('model-select').value && !byId('speech-start').disabled, 'startup');
    byId('message-text').value = 'Existing synthetic draft';
    byId('message-text').dispatchEvent(new Event('input'));
    byId('speech-start').click();
    await wait(() => byId('speech-status').textContent.startsWith('Ready.'), 'speech setup');
    byId('speech-start').click();
    await wait(() => !byId('speech-stop').hidden || !byId('speech-start').disabled, 'capture');
    if (byId('speech-stop').hidden) throw new Error(byId('speech-status').textContent);
    await sleep(1000);
    byId('speech-stop').click();
    await wait(() => byId('message-text').value.includes('Synthetic dictated words.'), 'transcription draft');
    if (byId('message-text').value !== 'Existing synthetic draft\nSynthetic dictated words.') throw new Error('Draft was overwritten.');
    result.released = tracks.every(track => track.readyState === 'ended');
    result.success = result.released;
  } catch (error) {
    result.error = { name: error.name, message: error.message };
  } finally {
    tracks.forEach(track => track.stop());
  }
  await fetch('/result', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(result) });
})();
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firefox", type=Path)
    args = parser.parse_args()
    if args.firefox.name.lower() != "firefox.exe" or not args.firefox.is_file():
        parser.error("Supply an existing Firefox executable; never downloads a browser.")
    finished = threading.Event()
    results = []
    audio_durations = []
    unexpected_posts = []
    state = {"revision": 0, "workspace": Workspace().model_dump(mode="json"), "domains": DOMAINS, "storage": "Synthetic, no persistence"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, value, kind="application/json"):
            body = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            for name, header_value in SECURITY_HEADERS.items():
                self.send_header(name, header_value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                page = (WEB / "index.html").read_text(encoding="utf-8")
                self.respond(page.replace("</body>", '<script defer src="/probe.js"></script></body>').encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/probe.js":
                self.respond(SCRIPT, "text/javascript")
            elif self.path in ("/assets/app.js", "/assets/speech-recorder.js", "/assets/style.css"):
                self.respond((WEB / self.path.rsplit("/", 1)[1]).read_bytes(), "text/css" if self.path.endswith(".css") else "text/javascript")
            elif self.path == "/api/state":
                self.respond(state)
            elif self.path == "/api/models":
                self.respond({"models": ["Lyra:latest"], "preferred_model": "Lyra:latest", "endpoint": "http://127.0.0.1/synthetic"})
            elif self.path == "/api/speech/status":
                self.respond({"available": True, "model": "Synthetic speech backend", "sample_rate": 16000, "max_seconds": 120, "detail": "Synthetic ready"})
            else:
                self.send_error(404)

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size < 6 * 1024 * 1024:
                self.send_error(400)
                return
            body = json.loads(self.rfile.read(size))
            if self.path == "/result":
                results.append(body)
                self.respond({"ok": True})
                finished.set()
            elif self.path == "/api/speech/transcribe":
                assert self.headers.get("X-JobWolf-Token") == TOKEN
                assert set(body) == {"audio_base64"}
                audio = decode_audio(body["audio_base64"])
                audio_durations.append(audio.seconds)
                self.respond({"text": "Synthetic dictated words.", "audio_seconds": audio.seconds, "elapsed_seconds": .1, "model": "Synthetic speech backend"})
            else:
                unexpected_posts.append(self.path)
                self.send_error(400)

    with tempfile.TemporaryDirectory(prefix="pip-synthetic-firefox-") as directory:
        profile = Path(directory)
        # Only this disposable profile: fake input and scripted test clicks,
        # never real microphone permission or settings from the user's profile.
        (profile / "user.js").write_text(
            'user_pref("media.navigator.streams.fake", true);\n'
            'user_pref("media.navigator.permission.disabled", true);\n'
            'user_pref("media.autoplay.default", 0);\n'
            'user_pref("browser.shell.checkDefaultBrowser", false);\n'
            'user_pref("browser.startup.homepage_override.mstone", "ignore");\n'
            'user_pref("datareporting.policy.dataSubmissionEnabled", false);\n'
            'user_pref("toolkit.telemetry.enabled", false);\n', encoding="utf-8",
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        process = None
        try:
            process = subprocess.Popen(
                [str(args.firefox), "--headless", "--no-remote", "--profile", str(profile), f"http://127.0.0.1:{server.server_port}/#token={TOKEN}"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if not finished.wait(25):
                raise RuntimeError("Synthetic Firefox capture check timed out.")
            print(json.dumps(results[0], indent=2))
            assert results[0].get("success"), "Firefox capture must produce an editable draft."
            assert len(audio_durations) == 1 and .25 <= audio_durations[0] <= 120
            assert unexpected_posts == [], "Dictation must not save or submit chat."
            print(f"PASS: Firefox capture -> valid mono PCM16/16 kHz WAV ({audio_durations[0]:.2f}s) -> editable draft; microphone released. No model or real workspace.")
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    main()
