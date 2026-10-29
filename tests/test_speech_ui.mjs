// Focused, synthetic frontend tests: no browser, microphone, server or model.
// Run with: node --test tests/test_speech_ui.mjs
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const appSource = await readFile(new URL("../src/jobwolf/web/app.js", import.meta.url), "utf8");
const html = await readFile(new URL("../src/jobwolf/web/index.html", import.meta.url), "utf8");
const workletSource = await readFile(new URL("../src/jobwolf/web/speech-recorder.js", import.meta.url), "utf8");
const model = "Granite Speech 5.0 2B · Q8";
const ready = { available: true, model, max_seconds: 120, sample_rate: 16000, detail: "Ready" };
const transcript = (text = "Synthetic dictated words") => ({ text, audio_seconds: 1, elapsed_seconds: 2, model });
const response = (body, status = 200) => ({ ok: status < 400, status, json: async () => body });
const settle = () => new Promise(setImmediate);
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function stream() {
  const track = { stopped: false, onended: null, stop() { this.stopped = true; } };
  return { track, getTracks: () => [track] };
}

function harness(config = {}) {
  const elements = new Map();
  const events = new Map();
  const timers = new Map();
  const requests = [], contexts = [], recorders = [], permissions = [], resamplers = [];
  let timerId = 0;
  class Element {
    value = ""; textContent = ""; hidden = false; disabled = false; open = false;
    dataset = {}; events = new Map(); children = []; scrollTop = 0;
    selectionStart = 0; selectionEnd = 0;
    constructor(tag = "div") { this.tag = tag; }
    addEventListener(name, callback) { this.events.set(name, callback); }
    emit(name, event = {}) { return this.events.get(name)?.({ preventDefault() {}, ...event }); }
    setAttribute(name, value) { this[name] = value; }
    replaceChildren(...children) { this.children = children; }
    append(...children) { this.children.push(...children); }
    reset() {}
    focus() { document.activeElement = this; }
    scrollIntoView() {}
    reportValidity() { return true; }
    setRangeText(text, start, end, mode) {
      assert.equal(mode, "preserve");
      this.value = this.value.slice(0, start) + text + this.value.slice(end);
    }
  }
  for (const match of html.matchAll(/<(\w+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element = new Element(match[1]);
    element.hidden = /\bhidden\b/.test(match[2]);
    if (/\bdata-lock\b/.test(match[2])) element.dataset.lock = "";
    elements.set(match[3], element);
  }
  const $ = (id) => {
    assert.ok(elements.has(id), `HTML includes ${id}`);
    return elements.get(id);
  };
  $("provider-select").value = "ollama";
  $("model-select").value = "synthetic-chat-model";
  $("context-tokens").value = "128000";
  $("task-select").value = "conversation";
  $("message-text").value = "Original draft";
  const document = {
    getElementById: $, createElement: (tag) => new Element(tag),
    querySelector: () => [...elements.values()].find((el) => el.tag === "dialog" && el.open),
    querySelectorAll: (selector) => selector === "[data-lock]"
      ? [...elements.values()].filter((el) => "lock" in el.dataset) : [],
  };
  const media = config.stream || stream();
  class AudioContext {
    state = "suspended"; sampleRate = config.rate || 16000; destination = {}; closeCount = 0;
    constructor(options) { assert.equal(options, undefined, "Capture must use the browser's native rate for Firefox."); contexts.push(this); }
    get audioWorklet() { return { addModule: (path) => {
      assert.equal(path, "/assets/speech-recorder.js");
      return config.module || Promise.resolve();
    } }; }
    resume() { this.state = "running"; return config.resume || Promise.resolve(); }
    close() { this.state = "closed"; this.closeCount += 1; return Promise.resolve(); }
    createMediaStreamSource(input) {
      assert.equal(input, media);
      this.source = { connect() {}, disconnect() { this.disconnected = true; } };
      return this.source;
    }
  }
  class OfflineAudioContext {
    destination = {};
    constructor(channels, length, rate) {
      assert.equal(channels, 1);
      assert.equal(rate, 16000);
      this.length = length;
      resamplers.push(this);
    }
    createBuffer(channels, length, rate) {
      assert.equal(channels, 1);
      const samples = new Float32Array(length);
      this.input = { length, rate, getChannelData: () => samples };
      return this.input;
    }
    createBufferSource() {
      this.source = { connect() {}, start() {}, disconnect() { this.disconnected = true; } };
      return this.source;
    }
    startRendering() {
      return config.render || Promise.resolve({ getChannelData: () => new Float32Array(this.length).fill(0.25) });
    }
  }
  class AudioWorkletNode {
    port = {
      onmessage: null, closed: false, sent: [],
      postMessage(message) { this.sent.push(message); },
      close() { this.closed = true; },
    };
    constructor(context, name, options) {
      assert.equal(name, "pip-speech-recorder");
      assert.equal(options.channelCount, 1);
      assert.ok(options.processorOptions.maxFrames <= context.sampleRate * 120);
      recorders.push(this);
    }
    connect() {}
    disconnect() { this.disconnected = true; }
    emit(data) { this.port.onmessage?.({ data }); }
  }
  const network = {
    status: () => response(config.status || ready),
    transcribe: () => response(transcript()),
  };
  const sandbox = {
    document, navigator: { mediaDevices: { getUserMedia: (options) => {
      permissions.push(options);
      return config.permission ? config.permission() : Promise.resolve(media);
    } } },
    window: {
      AudioContext, AudioWorkletNode, OfflineAudioContext, isSecureContext: true,
      addEventListener: (name, handler) => events.set(name, handler),
    },
    AbortController, Int16Array, Uint8Array, Float32Array, DataView, TextDecoder,
    btoa: (binary) => Buffer.from(binary, "binary").toString("base64"),
    performance: { now: () => 1000 },
    setTimeout: (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; },
    setInterval: (fn, ms) => { timers.set(++timerId, { fn, ms, interval: true }); return timerId; },
    clearTimeout: (id) => timers.delete(id), clearInterval: (id) => timers.delete(id),
    fetch: async (path, options) => {
      requests.push({ path, options });
      if (path === "/api/speech/status") return network.status(options);
      if (path === "/api/speech/transcribe") return network.transcribe(options);
      throw new Error(`Unexpected request: ${path}`);
    },
  };
  // Expose lexical functions only in this VM; production has no test hooks/exports.
  assert.equal(appSource.split("  init();").length, 2);
  vm.runInNewContext(appSource.replace("  init();", `
    token = "synthetic-token";
    state = { revision: 4, workspace: { map_version: 1, messages: [], statements: [], documents: [], opportunities: [], proposals: [], brief: null }, domains: {} };
    globalThis.ui = {
      checkSpeechStatus, startSpeech, stopSpeech, discardSpeech, clearPrivateDrafts,
      appendHeldTranscript, sendMessage, runAction,
      get session() { return speechSession; },
      get syncRequired() { return syncRequired; },
      get busy() { return busy; },
      get selections() { return JSON.stringify([Array.from(selectedMessages), Array.from(selectedStatements), selectedDocuments, attachedDocuments]); },
    };
    bindEvents();
    updateControls();
  `), sandbox);
  return {
    ui: sandbox.ui, $, network, media, contexts, recorders, requests, permissions, timers, sandbox, resamplers,
    event: (name) => events.get(name)?.({}),
    fire: (ms) => {
      const entry = [...timers].find(([, timer]) => timer.ms === ms);
      assert.ok(entry, `timer for ${ms} ms exists`);
      if (!entry[1].interval) timers.delete(entry[0]);
      entry[1].fn();
    },
    async record() { await sandbox.ui.checkSpeechStatus(); await sandbox.ui.startSpeech(); },
    finish(samples = new Int16Array([0, 500, -500])) {
      const recorder = recorders.at(-1);
      sandbox.ui.stopSpeech();
      recorder.emit({ type: "chunk", pcm: samples });
      recorder.emit({ type: "done", frames: samples.length });
    },
  };
}
function released(h) {
  assert.equal(h.ui.session, null);
  assert.equal(h.media.track.stopped, true);
  assert.ok(h.contexts.every((context) => context.state === "closed"));
  assert.ok(h.recorders.every((recorder) => recorder.port.closed && recorder.disconnected));
  assert.equal(h.timers.size, 0);
  assert.equal(h.ui.syncRequired, false);
}

test("setup check is authenticated, independent of chat, explicit and repeatable", async () => {
  const h = harness();
  const waiting = deferred();
  h.network.status = () => waiting.promise;
  const first = h.ui.startSpeech();
  assert.equal(h.ui.busy, false);
  assert.equal(h.$("save-message").disabled, false);
  let ran = false;
  await h.ui.runAction("Synthetic action", async () => { ran = true; });
  assert.equal(ran, true);
  assert.equal(h.permissions.length, 0);
  waiting.resolve(response({ ...ready, available: false, detail: "Weights not installed." }));
  await first;
  assert.match(h.$("speech-status").textContent, /setup-speech\.cmd/);
  assert.equal(h.contexts.length, 0);
  h.network.status = () => response(ready);
  await h.ui.checkSpeechStatus();
  assert.match(h.$("speech-status").textContent, /Press Dictate/);
  assert.equal(h.permissions.length, 0);
  for (const { path, options } of h.requests) {
    assert.equal(path, "/api/speech/status");
    assert.equal(options.method, "GET");
    assert.equal(options.headers["X-JobWolf-Token"], "synthetic-token");
    assert.equal(options.cache, "no-store");
    assert.equal(options.credentials, "omit");
    assert.equal(options.redirect, "error");
    assert.equal(options.body, undefined);
  }
});

test("recording and transcription keep typing, block mutations, and append to the latest draft", async () => {
  const h = harness();
  const waiting = deferred();
  h.network.transcribe = () => waiting.promise;
  const selections = h.ui.selections;
  await h.record();
  assert.equal(h.permissions[0].video, false);
  assert.equal(h.$("message-text").disabled, false);
  assert.equal(h.$("speech-stop").hidden, false);
  assert.equal(h.$("speech-cancel").disabled, false);
  assert.equal(h.$("save-message").disabled, true);
  assert.equal(h.$("retry-reply").disabled, true);
  assert.equal(h.$("reset-workspace").disabled, true);
  assert.match(h.$("speech-timer").textContent, /0:00 \/ 2:00/);
  let mutations = 0;
  await h.ui.runAction("Blocked", async () => { mutations++; });
  await h.ui.sendMessage({ preventDefault() {} });
  assert.equal(mutations, 0);
  h.finish(new Int16Array([-32768, 32767, 0]));
  assert.equal(h.ui.session.phase, "transcribing");
  assert.equal(h.media.track.stopped, true);
  assert.equal(h.contexts[0].state, "closed");
  assert.equal(h.recorders[0].port.closed, true);
  assert.equal(h.$("message-text").disabled, false);
  assert.equal(h.$("speech-cancel").disabled, false);
  await h.ui.runAction("Still blocked", async () => { mutations++; });
  assert.equal(mutations, 0);
  h.$("message-text").value = "Edited while transcribing";
  h.$("message-text").selectionStart = h.$("message-text").selectionEnd = 3;
  h.$("message-text").emit("input");
  const post = h.requests.find(({ path }) => path === "/api/speech/transcribe");
  const body = JSON.parse(post.options.body);
  assert.deepEqual(Object.keys(body), ["audio_base64"]);
  assert.equal(post.options.headers["X-JobWolf-Token"], "synthetic-token");
  const wav = Buffer.from(body.audio_base64, "base64");
  assert.equal(wav.length, 50);
  assert.equal(wav.toString("ascii", 0, 4), "RIFF");
  assert.equal(wav.readUInt32LE(4), wav.length - 8);
  assert.equal(wav.toString("ascii", 8, 16), "WAVEfmt ");
  assert.equal(wav.readUInt16LE(20), 1);
  assert.equal(wav.readUInt16LE(22), 1);
  assert.equal(wav.readUInt32LE(24), 16000);
  assert.equal(wav.readUInt32LE(28), 32000);
  assert.equal(wav.readUInt16LE(34), 16);
  assert.equal(wav.readUInt32LE(40), 6);
  assert.equal(wav.readInt16LE(44), -32768);
  assert.equal(wav.readInt16LE(46), 32767);
  waiting.resolve(response(transcript("New words")));
  await settle();
  assert.equal(h.$("message-text").value, "Edited while transcribing\nNew words");
  assert.equal(h.$("message-text").selectionStart, 3);
  assert.equal(h.ui.selections, selections);
  assert.equal(h.$("save-message").disabled, false);
  assert.deepEqual(h.requests.map(({ path }) => path), ["/api/speech/status", "/api/speech/transcribe"]);
  released(h);
});

for (const stage of ["permission", "module", "resume"]) {
  test(`cancel cleans up even when ${stage} finishes late`, async () => {
    const waiting = deferred();
    const media = stream();
    const config = { stream: media };
    if (stage === "permission") config.permission = () => waiting.promise;
    else config[stage] = waiting.promise;
    const h = harness(config);
    await h.ui.checkSpeechStatus();
    const starting = h.ui.startSpeech();
    await settle();
    assert.equal(h.ui.session.phase, "starting");
    assert.equal(h.$("speech-cancel").disabled, false);
    h.ui.discardSpeech();
    waiting.resolve(stage === "permission" ? media : undefined);
    await starting;
    assert.equal(h.recorders.length, 0);
    assert.equal(h.$("message-text").value, "Original draft");
    released(h);
  });
}

test("unsupported capture rates are rejected before recording", async () => {
  const h = harness({ rate: 192000 });
  await h.record();
  assert.equal(h.ui.session, null);
  assert.equal(h.contexts[0].state, "closed");
  assert.equal(h.permissions.length, 0);
  assert.equal(h.timers.size, 0);
  assert.match(h.$("speech-status").textContent, /unsupported audio rate/);
});

for (const rate of [44100, 48000]) {
  test(`native ${rate} Hz capture is bounded and resampled to a real 16 kHz WAV`, async () => {
    const h = harness({ rate });
    await h.record();
    assert.equal(h.ui.session.phase, "recording");
    assert.equal(h.ui.session.maxFrames, rate * 120);
    assert.equal(h.permissions[0].audio.sampleRate, undefined);
    h.finish(new Int16Array(480).fill(8192));
    assert.equal(h.media.track.stopped, true, "Microphone stops before resampling.");
    await settle();
    const resampler = h.resamplers[0];
    assert.equal(resampler.input.rate, rate);
    assert.equal(resampler.input.length, 480);
    assert.equal(resampler.input.getChannelData(0)[0], 0.25);
    assert.equal(resampler.source.disconnected, true);
    const body = JSON.parse(h.requests.at(-1).options.body);
    const wav = Buffer.from(body.audio_base64, "base64");
    assert.equal(wav.readUInt32LE(24), 16000);
    assert.equal(wav.readUInt32LE(40), Math.round(480 * 16000 / rate) * 2);
    assert.equal(h.$("message-text").value, "Original draft\nSynthetic dictated words");
    released(h);
  });
}

for (const outcome of ["cancel", "failure"]) {
  test(`resampling ${outcome} sends no audio and preserves the draft`, async () => {
    const rendering = deferred();
    const h = harness({ rate: 48000, render: rendering.promise });
    await h.record();
    h.finish(new Int16Array(480).fill(8192));
    assert.equal(h.media.track.stopped, true);
    if (outcome === "cancel") {
      h.ui.discardSpeech();
      rendering.resolve({ getChannelData: () => new Float32Array(160).fill(0.25) });
    } else {
      rendering.reject(new Error("PRIVATE_SYNTHETIC_AUDIO_FAILURE"));
    }
    await settle();
    assert.equal(h.requests.length, 1);
    assert.equal(h.$("message-text").value, "Original draft");
    assert.doesNotMatch(h.$("speech-status").textContent, /PRIVATE_SYNTHETIC/);
    if (outcome === "failure") assert.match(h.$("speech-status").textContent, /could not convert/);
    assert.equal(h.resamplers[0].source.disconnected, true);
    released(h);
  });
}

for (const failure of ["permission", "worklet load", "processor", "disconnection", "suspension"]) {
  test(`${failure} error releases resources without locking workspace sync`, async () => {
    const pendingModule = deferred();
    const config = failure === "permission"
      ? { permission: () => Promise.reject(Object.assign(new Error(), { name: "NotAllowedError" })) }
      : failure === "worklet load" ? { module: pendingModule.promise } : {};
    const h = harness(config);
    const recording = h.record();
    if (failure === "worklet load") { await settle(); pendingModule.reject(new Error("Synthetic failure")); }
    await recording;
    if (failure === "worklet load") assert.match(h.$("speech-status").textContent, /could not load Pip's local audio recorder/);
    if (failure === "processor") h.recorders[0].onprocessorerror();
    if (failure === "disconnection") h.media.track.onended();
    if (failure === "suspension") { h.contexts[0].state = "suspended"; h.contexts[0].onstatechange(); }
    assert.equal(h.ui.session, null);
    assert.ok(h.contexts.every((context) => context.state === "closed"));
    assert.equal(h.timers.size, 0);
    assert.equal(h.ui.syncRequired, false);
    assert.equal(h.$("message-text").value, "Original draft");
    if (failure !== "permission") assert.equal(h.media.track.stopped, true);
  });
}

for (const action of ["cancel", "dismiss", "clear drafts for reset/import", "pagehide"]) {
  test(`late HTTP response cannot restore drafts after ${action}`, async () => {
    const h = harness();
    const waiting = deferred();
    h.network.transcribe = () => waiting.promise; // Deliberately ignores fetch cancellation.
    await h.record();
    h.finish();
    const request = h.requests.at(-1);
    if (action === "cancel") h.ui.discardSpeech();
    else if (action === "dismiss") h.ui.discardSpeech({ hide: true });
    else if (action === "pagehide") h.event("pagehide");
    else h.ui.clearPrivateDrafts();
    const draft = h.$("message-text").value;
    assert.equal(request.options.signal.aborted, true);
    assert.equal(h.timers.size, 0);
    waiting.resolve(response(transcript("Must be discarded")));
    await settle();
    assert.equal(h.$("message-text").value, draft);
    assert.equal(h.$("speech-transcript").value, "");
    assert.equal(h.$("speech-overflow").hidden, true);
    released(h);
  });
}

test("canceled recording ignores queued chunks and releases every resource", async () => {
  const h = harness();
  await h.record();
  const session = h.ui.session;
  const receive = h.recorders[0].port.onmessage;
  receive({ data: { type: "chunk", pcm: new Int16Array([500]) } });
  h.ui.discardSpeech();
  assert.equal(session.chunks.length, 0);
  receive({ data: { type: "done", frames: 1 } });
  assert.equal(h.requests.length, 1);
  assert.equal(h.$("message-text").value, "Original draft");
  released(h);
});

test("overflow is kept in full, editable, and can be explicitly appended later", async () => {
  const h = harness();
  h.$("message-text").value = "x".repeat(11999);
  h.network.transcribe = () => response(transcript("Complete transcript"));
  await h.record();
  h.finish();
  await settle();
  assert.equal(h.$("message-text").value, "x".repeat(11999));
  assert.equal(h.$("speech-transcript").value, "Complete transcript");
  assert.equal(h.$("speech-overflow").hidden, false);
  assert.equal(h.$("speech-start").disabled, true);
  h.ui.appendHeldTranscript();
  assert.equal(h.$("speech-transcript").value, "Complete transcript");
  h.$("message-text").value = "Kept edits";
  h.$("speech-transcript").value = "Reviewed transcript";
  h.ui.appendHeldTranscript();
  assert.equal(h.$("message-text").value, "Kept edits\nReviewed transcript");
  assert.equal(h.$("speech-transcript").value, "");
  assert.equal(h.$("speech-overflow").hidden, true);
  released(h);
});

test("a transcript longer than the composer limit is never silently truncated", async () => {
  const h = harness();
  const text = "t".repeat(12001);
  h.network.transcribe = () => response(transcript(text));
  await h.record();
  h.finish();
  await settle();
  assert.equal(h.$("message-text").value, "Original draft");
  assert.equal(h.$("speech-transcript").value, text);
  h.ui.discardSpeech({ hide: true });
  assert.equal(h.$("speech-transcript").value, "");
  assert.equal(h.$("message-text").value, "Original draft");
});

test("IME composition keeps the transcript separate until explicitly added", async () => {
  const h = harness();
  await h.record();
  h.$("message-text").emit("compositionstart");
  h.finish();
  await settle();
  assert.equal(h.$("message-text").value, "Original draft");
  assert.equal(h.$("speech-append").disabled, true);
  assert.equal(h.$("speech-transcript").value, "Synthetic dictated words");
  h.$("message-text").emit("compositionend");
  assert.equal(h.$("message-text").value, "Original draft");
  h.ui.appendHeldTranscript();
  assert.equal(h.$("message-text").value, "Original draft\nSynthetic dictated words");
});

for (const silence of ["no audio", "empty transcript"]) {
  test(`${silence} reports no speech without altering the draft`, async () => {
    const h = harness();
    h.network.transcribe = () => response(transcript(" \n "));
    await h.record();
    h.finish(silence === "no audio" ? new Int16Array(128) : undefined);
    await settle();
    assert.equal(h.requests.length, silence === "no audio" ? 1 : 2);
    assert.match(h.$("speech-status").textContent, /No (audible audio|speech)/);
    assert.equal(h.$("message-text").value, "Original draft");
    released(h);
  });
}

for (const failure of ["timeout", "network", "409", "503"]) {
  test(`speech ${failure} never triggers the workspace sync lock`, async () => {
    const h = harness();
    h.network.transcribe = (options) => {
      if (failure === "timeout") return new Promise((resolve, reject) => {
        options.signal.addEventListener("abort", () => reject(new Error("Aborted")), { once: true });
      });
      if (failure === "network") throw new Error("Offline");
      return response({ detail: "Synthetic inference unavailable" }, Number(failure));
    };
    await h.record();
    h.finish();
    if (failure === "timeout") h.fire(240000);
    await settle();
    assert.equal(h.$("save-message").disabled, false);
    assert.equal(h.$("message-text").value, "Original draft");
    released(h);
  });
}

test("wall-clock limit stops recording and a missing flush fails safely", async () => {
  const h = harness();
  await h.record();
  h.fire(120000);
  assert.equal(h.media.track.stopped, true);
  assert.equal(h.ui.session.phase, "stopping");
  assert.equal(h.$("speech-cancel").disabled, false);
  assert.equal(h.recorders[0].port.sent[0].type, "stop");
  h.fire(3000);
  assert.match(h.$("speech-status").textContent, /did not finish/);
  released(h);
});

test("main thread also rejects audio beyond the recording memory bound", async () => {
  const h = harness({ status: { ...ready, max_seconds: 1 } });
  await h.record();
  for (let i = 0; i < 4; i++) h.recorders[0].emit({ type: "chunk", pcm: new Int16Array(4096) });
  assert.equal(h.requests.length, 1);
  assert.match(h.$("speech-status").textContent, /oversized/);
  released(h);
});

test("permission startup deadline stops a subsequently granted microphone", async () => {
  const waiting = deferred();
  const h = harness({ permission: () => waiting.promise });
  await h.ui.checkSpeechStatus();
  const starting = h.ui.startSpeech();
  h.fire(30000);
  waiting.resolve(h.media);
  await starting;
  assert.match(h.$("speech-status").textContent, /did not become ready/);
  released(h);
});

test("pagehide during recording releases capture without a transcription request", async () => {
  const h = harness();
  await h.record();
  h.event("pagehide");
  assert.equal(h.requests.length, 1);
  released(h);
});

test("dismissed setup check cannot reopen the speech panel on a late result", async () => {
  const waiting = deferred();
  const h = harness();
  h.network.status = () => waiting.promise;
  const checking = h.ui.checkSpeechStatus();
  h.ui.discardSpeech({ hide: true });
  waiting.resolve(response(ready));
  await checking;
  assert.equal(h.$("speech-panel").hidden, true);
  assert.doesNotMatch(h.$("speech-status").textContent, /Ready/);
  assert.equal(h.contexts.length, 0);
  assert.equal(h.timers.size, 0);
});

function worklet(maxFrames, rate = 16000) {
  const messages = [];
  let Processor;
  vm.runInNewContext(workletSource, {
    sampleRate: rate, Int16Array,
    AudioWorkletProcessor: class {
      port = { postMessage(data, transfer = []) {
        if (data.type === "chunk") assert.equal(transfer[0], data.pcm.buffer);
        messages.push(structuredClone(data, { transfer: Array.from(transfer) }));
      } };
    },
    registerProcessor(name, value) { assert.equal(name, "pip-speech-recorder"); Processor = value; },
  });
  return { processor: new Processor({ processorOptions: { maxFrames } }), messages };
}

test("worklet aggregates PCM, downmixes mono, silences playback and flushes partial chunks", () => {
  const { processor, messages } = worklet(16000);
  const left = new Float32Array([1, -1, 0.5, 4, -4]);
  const right = new Float32Array([-1, 1, 0.5, 4, -4]);
  const output = new Float32Array(5).fill(1);
  assert.equal(processor.process([[left, right]], [[output]]), true);
  assert.equal(messages.length, 0);
  assert.deepEqual([...output], [0, 0, 0, 0, 0]);
  processor.port.onmessage({ data: { type: "stop" } });
  assert.deepEqual([...messages[0].pcm], [0, 0, 16384, 32767, -32768]);
  assert.equal(messages[1].type, "done");
  assert.equal(messages[1].frames, 5);
  processor.port.onmessage({ data: { type: "stop" } });
  assert.equal(processor.process([[left]], [[output]]), false);
  assert.equal(messages.length, 2);
});

test("worklet caps native-rate capture without assuming 16 kHz input", () => {
  const { processor, messages } = worklet(480, 48000);
  const input = new Float32Array(128).fill(0.5);
  const output = new Float32Array(128);
  for (let block = 0; block < 4; block++) processor.process([[input]], [[output]]);
  assert.equal(messages[0].pcm.length, 480);
  assert.equal(messages[1].type, "done");
  assert.equal(messages[1].frames, 480);
});

test("worklet hard-caps audio at 120 seconds even without main-thread timers", () => {
  const { processor, messages } = worklet(1920000);
  const input = new Float32Array(128).fill(0.5);
  const output = new Float32Array(128);
  let active = true;
  for (let block = 0; block < 15000; block++) active = processor.process([[input]], [[output]]);
  assert.equal(active, false);
  const chunks = messages.filter(({ type }) => type === "chunk");
  assert.equal(chunks.reduce((sum, { pcm }) => sum + pcm.length, 0), 1920000);
  assert.equal(chunks.length, Math.ceil(1920000 / 4096));
  assert.ok(chunks.every(({ pcm }) => pcm.length <= 4096 && pcm[0] === 16384));
  assert.equal(messages.at(-1).type, "done");
  assert.equal(messages.at(-1).frames, 1920000);
  const count = messages.length;
  processor.process([[input]], [[output]]);
  assert.equal(messages.length, count);
  assert.throws(() => worklet(1920001), /bounded native-rate/);
  assert.throws(() => worklet(96000 * 120 + 1, 96000), /bounded native-rate/);
  assert.throws(() => worklet(16000, 192000), /bounded native-rate/);
});
