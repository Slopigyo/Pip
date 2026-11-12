// Focused opt-in Chrome capture check; no real microphone, model, or user workspace.
// node tests/smoke_speech_capture.mjs <python-executable>
// Set CHROME_EXECUTABLE_PATH to an absolute Chrome/Chromium executable path.
// Use an existing installation; never download a browser, use Edge, or open an existing profile.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, realpath, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, isAbsolute, join, resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

const [python, ...extra] = process.argv.slice(2);
assert.ok(python && !extra.length, "Supply the bench Python executable.");
const configuredChrome = process.env.CHROME_EXECUTABLE_PATH;
if (!configuredChrome || !isAbsolute(configuredChrome) || !/^(chrome|chromium)\.exe$/i.test(basename(configuredChrome))) {
  throw new Error("Set CHROME_EXECUTABLE_PATH to an absolute chrome.exe/chromium.exe path. Edge is not permitted.");
}
const chrome = await realpath(configuredChrome);
if (!/^(chrome|chromium)\.exe$/i.test(basename(chrome)) || !(await stat(chrome)).isFile()) {
  throw new Error("The configured path must resolve to an existing Chrome/Chromium executable, not Edge or a launcher.");
}
const root = resolve(import.meta.dirname, "..");
const temporary = await mkdtemp(join(tmpdir(), "jobwolf-browser-"));
const pending = new Map();
let server, browser, ws, sequence = 0, startup = "", fatal, closing = false;
let transcriptions = 0, mutations = 0;
const deadline = setTimeout(() => {
  fatal = new Error("Speech capture smoke exceeded its 60-second deadline.");
  browser?.kill(); server?.kill();
}, 60000);

async function waitFor(check, label) {
  const until = Date.now() + 10000;
  while (Date.now() < until) {
    if (fatal) throw fatal;
    const result = await check();
    if (result) return result;
    await sleep(50);
  }
  throw new Error(`Timed out: ${label}`);
}
function rpc(method, params = {}) {
  if (fatal) return Promise.reject(fatal);
  return new Promise((resolveResult, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Timed out: ${method}`)); }, 10000);
    pending.set(id, { resolve: resolveResult, reject, timer });
    ws.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const result = await rpc("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  return result.result.value;
}
function watch(child) {
  child.on("error", (error) => { if (!closing) fatal = error; });
  child.on("exit", (code) => { if (!closing) fatal = new Error(`Synthetic child exited (${code}).`); });
  return child;
}
async function fulfill(requestId, body) {
  return rpc("Fetch.fulfillRequest", {
    requestId, responseCode: 200,
    responseHeaders: [{ name: "Content-Type", value: "application/json" }],
    body: Buffer.from(JSON.stringify(body)).toString("base64"),
  });
}

try {
  // A synthetic 48 kHz signal exercises the browser's actual device-rate path.
  const frames = 48000 * 4;
  const wav = Buffer.alloc(44 + frames * 2);
  wav.write("RIFF"); wav.writeUInt32LE(wav.length - 8, 4); wav.write("WAVEfmt ", 8);
  wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(48000, 24); wav.writeUInt32LE(96000, 28);
  wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34); wav.write("data", 36);
  wav.writeUInt32LE(frames * 2, 40);
  for (let i = 0; i < frames; i++) wav.writeInt16LE(Math.round(8000 * Math.sin(2 * Math.PI * 440 * i / 48000)), 44 + i * 2);
  const fakeAudio = join(temporary, "synthetic.wav");
  await writeFile(fakeAudio, wav);
  server = watch(spawn(python, ["-u", "tests/browser_server.py", "--temporary-dir", temporary], {
    cwd: root, env: { ...process.env, PYTHONPATH: join(root, "src") }, stdio: ["ignore", "pipe", "pipe"],
  }));
  server.stdout.on("data", (chunk) => { startup += chunk; });
  // This server contains only synthetic fixtures. Never print its session link.
  server.stderr.on("data", () => {});
  const url = await waitFor(() => startup.match(/http:\/\/127\.0\.0\.1:\d+\/#token=[\w-]+/)?.[0], "synthetic server");
  const origin = new URL(url).origin;
  const profile = join(temporary, "chrome");
  browser = watch(spawn(chrome, [
    "--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
    "--disable-extensions", "--disable-sync", "--remote-debugging-port=0", `--user-data-dir=${profile}`,
    "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", `--use-file-for-fake-audio-capture=${fakeAudio}`,
    "about:blank",
  ], { stdio: "ignore" }));
  const port = await waitFor(async () => {
    try { return Number((await readFile(join(profile, "DevToolsActivePort"), "utf8")).split("\n")[0]); }
    catch (error) { if (error.code !== "ENOENT") throw error; return false; }
  }, "shared Chrome");
  const targets = await (await fetch(`http://127.0.0.1:${port}/json`, { signal: AbortSignal.timeout(5000) })).json();
  ws = new WebSocket(targets.find((target) => target.type === "page").webSocketDebuggerUrl);
  await new Promise((resolveOpen, reject) => {
    ws.addEventListener("open", resolveOpen, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  ws.addEventListener("message", ({ data }) => {
    const message = JSON.parse(data);
    if (pending.has(message.id)) {
      const call = pending.get(message.id);
      clearTimeout(call.timer); pending.delete(message.id);
      if (message.error) call.reject(new Error(message.error.message)); else call.resolve(message.result);
    }
    if (message.method === "Fetch.requestPaused") void (async () => {
      const { requestId, request } = message.params;
      const requestUrl = new URL(request.url);
      if (requestUrl.origin !== origin) {
        await rpc("Fetch.failRequest", { requestId, errorReason: "BlockedByClient" });
        throw new Error("Unexpected external page request.");
      }
      if (requestUrl.pathname === "/api/speech/status") {
        await fulfill(requestId, { available: true, model: "Synthetic speech backend", sample_rate: 16000, max_seconds: 120, detail: "Synthetic ready" });
      } else if (requestUrl.pathname === "/api/speech/transcribe") {
        transcriptions++;
        const body = JSON.parse(request.postData);
        assert.deepEqual(Object.keys(body), ["audio_base64"]);
        const audio = Buffer.from(body.audio_base64, "base64");
        assert.equal(audio.toString("ascii", 0, 4), "RIFF");
        assert.equal(audio.readUInt32LE(24), 16000);
        assert.equal(audio.readUInt16LE(22), 1);
        assert.equal(audio.readUInt16LE(34), 16);
        assert.equal(audio.readUInt32LE(40), audio.length - 44);
        const seconds = (audio.length - 44) / 32000;
        assert.ok(seconds >= .25 && seconds <= 120);
        await fulfill(requestId, { text: "Synthetic dictated words.", audio_seconds: seconds, elapsed_seconds: .1, model: "Synthetic speech backend" });
      } else {
        if (request.method !== "GET") mutations++;
        await rpc("Fetch.continueRequest", { requestId });
      }
    })().catch((error) => { fatal = error; });
  });
  await rpc("Page.enable");
  await rpc("Runtime.enable");
  await rpc("Fetch.enable", { patterns: [{ urlPattern: "*", requestStage: "Request" }] });
  await rpc("Page.addScriptToEvaluateOnNewDocument", { source: `
    window.__captureErrors = [];
    window.__captureStreams = [];
    const capture = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async (...args) => {
      const stream = await capture(...args); window.__captureStreams.push(stream); return stream;
    };
    const source = AudioContext.prototype.createMediaStreamSource;
    AudioContext.prototype.createMediaStreamSource = function (...args) {
      try { return source.apply(this, args); }
      catch (error) { window.__captureErrors.push({ stage: 'source', name: error.name, message: error.message }); throw error; }
    };
    const module = Worklet.prototype.addModule;
    Worklet.prototype.addModule = async function (...args) {
      try { return await module.apply(this, args); }
      catch (error) { window.__captureErrors.push({ stage: 'worklet', name: error.name, message: error.message }); throw error; }
    };
  ` });
  await rpc("Page.navigate", { url });
  await waitFor(() => evaluate("document.getElementById('main') && !document.getElementById('main').hidden && document.getElementById('model-select').value === 'Lyra:latest' && !document.getElementById('speech-start').disabled"), "composer ready for dictation");
  await evaluate("document.getElementById('message-text').value = 'Existing synthetic draft'; document.getElementById('message-text').dispatchEvent(new Event('input')); document.getElementById('speech-start').click()");
  await waitFor(() => evaluate("document.getElementById('speech-status').textContent.startsWith('Ready.')"), "speech ready");
  await evaluate("document.getElementById('speech-start').click()");
  await waitFor(() => evaluate("!document.getElementById('speech-stop').hidden || !document.getElementById('speech-start').disabled"), "capture startup");
  const startupResult = await evaluate("({ recording: !document.getElementById('speech-stop').hidden, status: document.getElementById('speech-status').textContent, errors: window.__captureErrors })");
  console.log("Capture startup:", JSON.stringify(startupResult));
  assert.ok(startupResult.recording, "Recording must stay active after permission is granted.");
  await sleep(1000);
  await evaluate("document.getElementById('speech-stop').click()");
  await waitFor(() => evaluate("document.getElementById('message-text').value.includes('Synthetic dictated words.')"), "editable transcript");
  assert.equal(await evaluate("document.getElementById('message-text').value"), "Existing synthetic draft\nSynthetic dictated words.");
  assert.equal(await evaluate("window.__captureStreams.every(stream => stream.getTracks().every(track => track.readyState === 'ended'))"), true);
  assert.equal(transcriptions, 1);
  assert.equal(mutations, 0);
  console.log("PASS: real Chrome audio capture, PCM16/16 kHz WAV, microphone release, editable draft, no automatic chat submission.");
} catch (error) {
  if (ws?.readyState === WebSocket.OPEN && !fatal) console.log("Synthetic capture diagnostics:", await evaluate("({ status: document.getElementById('speech-status')?.textContent, errors: window.__captureErrors })"));
  throw error;
} finally {
  closing = true;
  clearTimeout(deadline);
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ id: ++sequence, method: "Browser.close" }));
    await sleep(250); ws.close();
  }
  for (const call of pending.values()) { clearTimeout(call.timer); call.reject(new Error("Capture test cleanup.")); }
  browser?.kill(); server?.kill();
  await sleep(300);
  await rm(temporary, { recursive: true, force: true, maxRetries: 10, retryDelay: 150 });
}
