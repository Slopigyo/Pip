// Opt-in, dependency-free Windows/Chrome smoke test (Node 22+).
// node tests/smoke_browser.mjs <python-executable> [<shared-chrome-executable>]
// Only the shared Chrome below is permitted; never download a browser, use Edge,
// or open an existing browser profile/workspace. All content/provider output is synthetic.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { access, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

const chrome = "C:/Users/S/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe";
const [python, requestedChrome, ...extra] = process.argv.slice(2);
if (!python || extra.length) throw new Error("Supply a Python executable; the shared Chrome path is optional.");
if (process.platform !== "win32") throw new Error("This smoke requires Windows DPAPI; no plaintext fallback is allowed.");
if (requestedChrome && resolve(requestedChrome).toLowerCase() !== resolve(chrome).toLowerCase()) {
  throw new Error(`Only the shared Chrome is permitted: ${chrome}`);
}
await access(chrome);
const root = resolve(import.meta.dirname, "..");
const temporary = await mkdtemp(join(tmpdir(), "jobwolf-browser-"));
let server, browser, ws, origin, token, deadlineTimer, fatal;
let startup = "", serverErrors = "", sequence = 0, closing = false;
const errors = [], blockedRequests = [], requests = [];
const pending = new Map(), network = new Map();

async function waitFor(check, label, timeout = 20000) {
  const deadline = Date.now() + timeout;
  let lastError;
  while (Date.now() < deadline) {
    if (fatal) throw fatal;
    try {
      const result = await check();
      if (result) return result;
    } catch (error) { lastError = error; }
    await sleep(50);
  }
  throw new Error(`Timed out: ${label}`, { cause: lastError });
}

function watchChild(child, name) {
  child.on("error", (error) => { if (!closing && !fatal) fatal = new Error(`${name} failed to start`, { cause: error }); });
  child.on("exit", (code) => { if (!closing && !fatal) fatal = new Error(`${name} exited unexpectedly (${code}).`); });
  return child;
}

function rejectPending(error) {
  for (const request of pending.values()) {
    clearTimeout(request.timer);
    request.reject(error);
  }
  pending.clear();
}

function rpc(method, params = {}) {
  if (fatal) return Promise.reject(fatal);
  if (ws?.readyState !== WebSocket.OPEN) return Promise.reject(new Error("Chrome debugging connection is not open."));
  return new Promise((resolveResult, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`Chrome command timed out: ${method}`));
    }, 10000);
    pending.set(id, { resolve: resolveResult, reject, timer });
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await rpc("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  }
  return result.result.value;
}

async function input(id, value) {
  await evaluate(`(() => {
    const e = document.getElementById(${JSON.stringify(id)});
    if (!e || e.disabled) throw new Error('Input unavailable: ' + ${JSON.stringify(id)});
    e.value = ${JSON.stringify(value)};
    e.dispatchEvent(new Event('input', {bubbles:true}));
    e.dispatchEvent(new Event('change', {bubbles:true}));
  })()`);
}

async function clickSelector(selector) {
  const point = await evaluate(`(() => {
    const e = document.querySelector(${JSON.stringify(selector)});
    if (!e || e.disabled || !e.checkVisibility()) throw new Error('Control unavailable: ' + ${JSON.stringify(selector)});
    e.scrollIntoView({block:'center', inline:'nearest', behavior:'instant'});
    const r = e.getBoundingClientRect();
    return {x:r.x + r.width / 2, y:r.y + r.height / 2};
  })()`);
  await rpc("Input.dispatchMouseEvent", { type: "mousePressed", ...point, button: "left", clickCount: 1 });
  await rpc("Input.dispatchMouseEvent", { type: "mouseReleased", ...point, button: "left", clickCount: 1 });
}

const click = (id) => clickSelector(`#${id}`);
const type = (text) => rpc("Input.insertText", { text });
async function enter({ shift = false, repeat = false } = {}) {
  const key = { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, modifiers: shift ? 8 : 0 };
  await rpc("Input.dispatchKeyEvent", { type: "keyDown", ...key, text: "\r", unmodifiedText: "\r", autoRepeat: repeat });
  await rpc("Input.dispatchKeyEvent", { type: "keyUp", ...key });
}

const idle = () => waitFor(() => evaluate("!document.getElementById('reload-state').disabled"), "UI request to finish");
const value = (id) => evaluate(`document.getElementById(${JSON.stringify(id)}).value`);
const text = (id) => evaluate(`document.getElementById(${JSON.stringify(id)}).textContent`);
const visible = (id) => evaluate(`document.getElementById(${JSON.stringify(id)}).checkVisibility()`);
const count = (selector) => evaluate(`document.querySelectorAll(${JSON.stringify(selector)}).length`);
const uiRequests = (path, method = "POST") => requests.filter((request) => request.path === path && request.method === method);

// Observer/control requests use Node, not page fetch: CDP then records only real
// UI requests, so retry's fresh GET /api/state cannot be satisfied by our polling.
async function api(path, body) {
  const response = await fetch(origin + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-JobWolf-Token": token, Origin: origin, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(10000), redirect: "error",
  });
  assert.equal(response.status, 200, `Observer request failed: ${path}`);
  return response.json();
}
const apiState = () => api("/api/state");
const providerState = () => api("/api/smoke");
const advance = (call, stage) => api("/api/smoke/advance", { call, stage });

async function partial(prefix) {
  await waitFor(() => evaluate(`(() => {
    const cards = document.querySelectorAll('#message-list .message-card.assistant.streaming');
    return cards.length === 1 && cards[0].textContent.includes(${JSON.stringify(prefix)});
  })()`), "real incremental text in one streaming assistant bubble");
  assert.equal(await evaluate("document.getElementById('reload-state').disabled"), true, "Partial text must arrive before completion.");
}

function noProposals(state) {
  assert.deepEqual(state.workspace.proposals, [], "Ordinary chat must never stage proposals.");
  assert.deepEqual(state.workspace.statements, []);
  assert.deepEqual(state.workspace.opportunities, []);
  assert.equal(state.workspace.brief, null);
  assert.equal(state.workspace.map_version, 0);
}

async function safeRendering() {
  assert.equal(await evaluate("typeof window.__injected"), "undefined");
  assert.equal(await count("#message-list script, #message-list img, #message-list iframe, #message-list svg, #message-list object"), 0,
    "Untrusted markup must be literal text, not merely prevented from executing by CSP.");
}

async function transcript(state) {
  const cards = await evaluate(`Array.from(document.querySelectorAll('#message-list .message-card'), e => ({
    user:e.classList.contains('user'), assistant:e.classList.contains('assistant'), text:e.textContent
  }))`);
  assert.equal(cards.length, state.workspace.messages.length, "One bubble per saved message after completion.");
  state.workspace.messages.forEach((message, index) => {
    assert.equal(cards[index].user, message.role === "user");
    assert.equal(cards[index].assistant, message.role === "assistant");
    assert.ok(cards[index].text.includes(message.text), `Transcript message ${index + 1} must show saved text verbatim.`);
  });
  assert.equal(await count("#message-list input[type=checkbox], #message-list .streaming"), 0);
  await safeRendering();
}

async function composer(expected, busy) {
  assert.deepEqual(await evaluate(`(() => {
    const e = document.getElementById('message-text');
    return {value:e.value, disabled:e.disabled, readOnly:e.readOnly, focus:document.activeElement.id,
      sendDisabled:document.getElementById('save-message').disabled,
      busy:document.getElementById('reload-state').disabled};
  })()`), { value: expected, disabled: false, readOnly: false, focus: "message-text", sendDisabled: busy || !expected.trim(), busy });
}

async function narrowLayout(label) {
  for (const width of [390, 320]) {
    await rpc("Emulation.setDeviceMetricsOverride", { width, height: 844, deviceScaleFactor: 1, mobile: false });
    await evaluate("new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))");
    const dimensions = await evaluate(`({
      viewport:document.documentElement.clientWidth,
      document:document.documentElement.scrollWidth, body:document.body.scrollWidth,
      regions:Array.from(document.querySelectorAll('#message-list, #message-list .message-card'), e => ({
        width:e.clientWidth, scroll:e.scrollWidth
      })),
      controls:['message-form','message-text','save-message'].map(id => {
        const e = document.getElementById(id), r = e.getBoundingClientRect();
        return {id, visible:e.checkVisibility(), left:r.left, right:r.right, width:r.width};
      })
    })`);
    assert.ok(dimensions.document <= dimensions.viewport + 1 && dimensions.body <= dimensions.viewport + 1,
      `${label}: page overflow at ${width}px: ${JSON.stringify(dimensions)}`);
    for (const region of dimensions.regions) assert.ok(region.width > 0 && region.scroll <= region.width + 1,
      `${label}: transcript must wrap long untrusted text at ${width}px.`);
    for (const control of dimensions.controls) assert.ok(control.visible && control.width > 0 && control.left >= -1 && control.right <= width + 1,
      `${label}: ${control.id} must remain usable at ${width}px.`);
  }
  await rpc("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
}

try {
  deadlineTimer = setTimeout(() => {
    fatal = new Error("Browser smoke exceeded its 150-second deadline.");
    rejectPending(fatal);
    server?.kill();
    browser?.kill();
  }, 150000);
  server = watchChild(spawn(python, ["-u", "tests/browser_server.py", "--temporary-dir", temporary], {
    cwd: root, env: { ...process.env, PYTHONPATH: join(root, "src"), PYTHONDONTWRITEBYTECODE: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  }), "Synthetic server");
  // Never echo the session-bearing startup link or raw provider diagnostics.
  server.stdout.on("data", (chunk) => { startup += chunk; });
  server.stderr.on("data", (chunk) => { serverErrors += chunk; });
  const url = await waitFor(() => startup.match(/http:\/\/127\.0\.0\.1:\d+\/#token=[A-Za-z0-9_-]+/)?.[0], "synthetic server startup");
  origin = new URL(url).origin;
  token = new URLSearchParams(new URL(url).hash.slice(1)).get("token");
  const fixture = await waitFor(providerState, "synthetic provider readiness");
  assert.equal(fixture.model, "Lyra:latest");
  assert.deepEqual(fixture.message_ids, []);
  assert.equal(fixture.generate_calls, 0);

  const profile = join(temporary, "chrome");
  browser = watchChild(spawn(chrome, [
    "--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
    "--disable-extensions", "--disable-sync", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank",
  ], { stdio: "ignore" }), "Shared Chrome");
  const debugPort = await waitFor(async () => {
    const info = await readFile(join(profile, "DevToolsActivePort"), "utf8");
    const port = Number(info.split("\n")[0]);
    return Number.isInteger(port) && port > 0 ? port : false;
  }, "shared Chrome debugging endpoint");
  const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json`, { signal: AbortSignal.timeout(5000) })).json();
  ws = new WebSocket(targets.find((target) => target.type === "page").webSocketDebuggerUrl);
  await new Promise((done, reject) => {
    const timer = setTimeout(() => reject(new Error("Chrome debugging connection timed out.")), 10000);
    ws.addEventListener("open", () => { clearTimeout(timer); done(); }, { once: true });
    ws.addEventListener("error", () => { clearTimeout(timer); reject(new Error("Chrome debugging connection failed.")); }, { once: true });
  });
  ws.addEventListener("close", () => rejectPending(new Error("Chrome debugging connection closed.")));
  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const request = pending.get(message.id);
      pending.delete(message.id);
      clearTimeout(request.timer);
      if (message.error) request.reject(new Error(message.error.message));
      else request.resolve(message.result);
    }
    if (message.method === "Runtime.exceptionThrown") errors.push(message.params.exceptionDetails.text);
    if (message.method === "Network.requestWillBeSent") {
      const { requestId, request } = message.params;
      const path = new URL(request.url).pathname;
      if (path.startsWith("/api/")) {
        const record = { id: requestId, path, method: request.method, body: request.postData ? JSON.parse(request.postData) : undefined };
        requests.push(record);
        network.set(requestId, record);
      }
    }
    if (message.method === "Network.responseReceived") {
      const record = network.get(message.params.requestId);
      if (record) Object.assign(record, { status: message.params.response.status, mime: message.params.response.mimeType });
    }
    if (message.method === "Network.loadingFinished") {
      const record = network.get(message.params.requestId);
      if (record) record.finished = true;
    }
    if (message.method === "Network.loadingFailed") {
      const record = network.get(message.params.requestId);
      if (record) record.failure = message.params.errorText;
    }
    if (message.method === "Fetch.requestPaused") {
      const { requestId, request } = message.params;
      const allowed = new URL(request.url).origin === origin;
      if (!allowed) blockedRequests.push(new URL(request.url).origin);
      rpc(allowed ? "Fetch.continueRequest" : "Fetch.failRequest", {
        requestId, ...(allowed ? {} : { errorReason: "BlockedByClient" }),
      }).catch((error) => { if (!closing) errors.push(error.message); });
    }
  });
  await rpc("Runtime.enable");
  await rpc("Page.enable");
  await rpc("Network.enable");
  await rpc("Fetch.enable", { patterns: [{ urlPattern: "*", requestStage: "Request" }] });
  await rpc("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
  await rpc("Page.navigate", { url });
  await waitFor(() => evaluate("document.getElementById('main') && !document.getElementById('main').hidden"), "authenticated UI");
  await waitFor(() => value("model-select").then((model) => model === fixture.model), "synthetic Lyra model selection");
  await idle();

  const initial = await apiState();
  assert.equal(initial.revision, 0);
  assert.deepEqual(initial.workspace.messages, []);
  assert.match(initial.storage, /DPAPI-encrypted/);
  noProposals(initial);
  assert.equal(await evaluate("location.hash"), "");
  assert.equal(await count('script[src^="http"]'), 0);
  assert.equal(await value("provider-select"), "ollama");
  assert.equal((await text("save-message")).trim(), "Send");
  assert.equal(await evaluate("document.getElementById('save-message').disabled"), true);
  assert.deepEqual(await evaluate(`({
    tag:document.getElementById('bench-tools').tagName, open:document.getElementById('bench-tools').open,
    contextInTools:!!document.getElementById('message-context').closest('#bench-tools'),
    transcriptInTools:!!document.getElementById('message-list').closest('#bench-tools')
  })`), { tag: "DETAILS", open: false, contextInTools: true, transcriptInTools: false });
  assert.equal(await visible("retry-reply"), false);

  // Keep the real HTTP/provider path, but fragment its bytes deliberately so the
  // production decoder sees split UTF-8 and split NDJSON records. Capture those
  // bytes here: Chrome's diagnostic body cache can discard handled error streams.
  await evaluate(`(() => {
    const originalFetch = window.fetch.bind(window);
    window.__smokeChatStreams = [];
    window.fetch = async (...args) => {
      const response = await originalFetch(...args);
      if (args[0] !== '/api/chat' || !response.ok) return response;
      const captured = {text:'', ended:false, splitUtf8:0};
      const dropDone = window.__smokeDropCompletion === true;
      window.__smokeDropCompletion = false;
      window.__smokeChatStreams.push(captured);
      const decoder = new TextDecoder();
      let pendingLines = '';
      const fragment = (bytes, controller) => {
        for (let offset = 0; offset < bytes.length; offset += 7) {
          const end = Math.min(offset + 7, bytes.length);
          if (end < bytes.length && (bytes[end] & 0xc0) === 0x80) captured.splitUtf8++;
          controller.enqueue(bytes.slice(offset, end));
        }
      };
      const transform = new TransformStream({
        transform(bytes, controller) {
          const decoded = decoder.decode(bytes, {stream:true});
          captured.text += decoded;
          if (!dropDone) { fragment(bytes, controller); return; }
          pendingLines += decoded;
          let newline;
          while ((newline = pendingLines.indexOf('\\n')) !== -1) {
            const line = pendingLines.slice(0, newline);
            pendingLines = pendingLines.slice(newline + 1);
            if (JSON.parse(line).type !== 'done') fragment(new TextEncoder().encode(line + '\\n'), controller);
          }
        },
        flush() { captured.text += decoder.decode(); captured.ended = true; }
      });
      return new Response(response.body.pipeThrough(transform), {status:response.status, headers:response.headers});
    };
  })()`);
  console.log("Checking keyboard/IME handling and real incremental chat…");
  await input("message-text", "   ");
  assert.equal(await evaluate("document.getElementById('save-message').disabled"), true);
  await evaluate("document.getElementById('message-form').requestSubmit()");
  await input("message-text", "");
  await click("message-text");
  const firstLine = "Synthetic browser test: I enjoy repairing bicycles.";
  await type(firstLine);
  await enter({ shift: true });
  assert.equal(await value("message-text"), firstLine + "\n", "Shift+Enter must insert a real newline, not submit.");
  await type(fixture.untrusted);
  const firstMessage = firstLine + "\n" + fixture.untrusted;
  // Headless Chrome cannot drive an OS IME. Exercise both composition guards
  // with DOM events, then use native CDP Enter for the actual submission.
  await evaluate(`(() => {
    const e = document.getElementById('message-text');
    e.dispatchEvent(new CompositionEvent('compositionstart', {bubbles:true}));
    e.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', code:'Enter', keyCode:13, isComposing:true, bubbles:true, cancelable:true}));
    e.dispatchEvent(new CompositionEvent('compositionend', {bubbles:true}));
    e.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', code:'Enter', keyCode:229, bubbles:true, cancelable:true}));
  })()`);
  await sleep(150);
  assert.equal(await value("message-text"), firstMessage);
  assert.deepEqual(await apiState(), initial, "Whitespace, Shift+Enter, and IME confirmation must not save or infer.");
  assert.equal(uiRequests("/api/messages").length, 0);
  assert.equal(uiRequests("/api/chat").length, 0);
  await enter();
  await evaluate("document.getElementById('message-form').requestSubmit(); document.getElementById('message-form').requestSubmit()");
  await partial(fixture.chunks[0]);
  const firstSaved = await apiState();
  assert.equal(firstSaved.revision, initial.revision + 1);
  assert.deepEqual(firstSaved.workspace.messages.map(({ role, text }) => ({ role, text })), [{ role: "user", text: firstMessage }]);
  noProposals(firstSaved);
  assert.equal(await count("#message-list .message-card.user"), 1);
  assert.equal(await count("#message-list .message-card.assistant"), 1);
  assert.ok((await text("chat-status")).trim(), "Chat must expose a status while streaming.");
  assert.equal((await text("message-list")).includes(fixture.chunks[1]), false);
  await composer("", true);

  const nextDraft = "Synthetic next draft — I can keep typing while Pip replies.";
  await type(nextDraft); // Deliberately do not refocus: Send must return focus to the composer.
  await composer(nextDraft, true);
  await enter();
  await enter({ repeat: true });
  await evaluate(`document.getElementById('message-form').requestSubmit();
    document.getElementById('message-form').requestSubmit(); document.getElementById('save-message').click()`);
  await advance(1, 1);
  await partial(fixture.chunks[0] + fixture.chunks[1]);
  assert.deepEqual(await apiState(), firstSaved, "Even the second visible chunk must not persist an assistant message.");
  assert.equal(uiRequests("/api/messages").length, 1, "Busy/repeated submissions must not issue another save.");
  assert.equal(uiRequests("/api/chat").length, 1, "Busy/repeated submissions must not start another stream.");
  await composer(nextDraft, true);
  await safeRendering();
  await advance(1, 2);
  await waitFor(async () => (await apiState()).workspace.messages.length === 2, "completed assistant save");
  await idle();
  const completed = await apiState();
  assert.equal(completed.revision, firstSaved.revision + 1);
  assert.equal(completed.workspace.messages[1].text, fixture.chunks.join(""));
  noProposals(completed);
  await transcript(completed);
  await composer(nextDraft, false);
  assert.equal(await visible("retry-reply"), false);
  assert.equal(await evaluate("document.getElementById('bench-tools').open"), false);
  if (process.env.PIP_SMOKE_SCREENSHOT) {
    const screenshot = await rpc("Page.captureScreenshot", { format: "png" });
    await writeFile(resolve(process.env.PIP_SMOKE_SCREENSHOT), Buffer.from(screenshot.data, "base64"));
  }
  const firstChat = uiRequests("/api/chat")[0];
  assert.deepEqual(firstChat.body, { revision: firstSaved.revision, message_id: firstSaved.workspace.messages[0].id, model: fixture.model });
  assert.equal(firstChat.mime, "application/x-ndjson");
  assert.ok(requests.indexOf(uiRequests("/api/messages")[0]) < requests.indexOf(firstChat));

  console.log("Checking partial failure, sanitized diagnostics, and retry without a duplicate user…");
  await input("message-text", fixture.failure_message);
  await click("save-message");
  await partial(fixture.failed_chunk);
  const beforeFailure = await apiState();
  assert.equal(beforeFailure.workspace.messages.length, 3);
  const failedUser = beforeFailure.workspace.messages.at(-1);
  assert.equal(failedUser.text, fixture.failure_message);
  await composer("", true);
  const retryDraft = "Synthetic unsent draft kept through failure and retry.";
  await type(retryDraft);
  await advance(2, 1);
  await idle();
  assert.equal(await visible("retry-reply"), true);
  assert.deepEqual(await apiState(), beforeFailure, "Failed partial reply must not alter saved history or revision.");
  const failedCard = await evaluate(`Array.from(document.querySelectorAll('#message-list .message-card.assistant'))
    .find(e => e.textContent.includes(${JSON.stringify(fixture.failed_chunk)}))?.textContent`);
  assert.match(failedCard || "", /not[\s-]+saved|unsaved/i, "The failed partial bubble must visibly say it was not saved.");
  assert.equal(await visible("error-notice"), true);
  const wire = await evaluate("window.__smokeChatStreams[1]");
  assert.equal(wire.ended, true, "The server's failure event must reach EOF cleanly.");
  const failureBody = wire.text;
  const events = failureBody.trim().split("\n").map((line) => JSON.parse(line));
  const failure = events.find((event) => event.type === "error");
  assert.ok(failure);
  assert.match(failure.detail, /local model could not complete this reply/i);
  assert.match(failure.detail, /no assistant reply was saved/i);
  assert.ok((await text("error-notice")).includes(failure.detail), "Display the API's sanitized failure, not the provider exception.");
  assert.equal(events.some((event) => event.type === "done"), false);
  assert.equal(failureBody.includes(fixture.private_error), false);
  assert.equal((await evaluate("document.body.textContent")).includes(fixture.private_error), false);
  await composer(retryDraft, false);
  assert.deepEqual((await providerState()).message_ids, [firstSaved.workspace.messages[0].id, failedUser.id]);

  const retryStart = requests.length;
  await click("retry-reply");
  await evaluate("document.getElementById('retry-reply').click(); document.getElementById('retry-reply').click()");
  await partial(fixture.chunks[0]);
  assert.deepEqual(await apiState(), beforeFailure);
  const retriedRequests = requests.slice(retryStart);
  const reloadIndex = retriedRequests.findIndex((request) => request.path === "/api/state" && request.method === "GET");
  const chatIndex = retriedRequests.findIndex((request) => request.path === "/api/chat" && request.method === "POST");
  assert.ok(reloadIndex >= 0 && chatIndex > reloadIndex && retriedRequests[reloadIndex].finished,
    "Retry must finish a fresh state fetch before /api/chat; observer polling does not count.");
  assert.equal(retriedRequests.some((request) => request.path === "/api/messages"), false);
  assert.deepEqual(retriedRequests[chatIndex].body, { revision: beforeFailure.revision, message_id: failedUser.id, model: fixture.model });
  await composer(retryDraft, true);
  await advance(3, 1);
  await partial(fixture.chunks[0] + fixture.chunks[1]);
  assert.deepEqual(await apiState(), beforeFailure);
  await advance(3, 2);
  await waitFor(async () => (await apiState()).workspace.messages.length === 4, "retry saves one completed reply");
  await idle();
  const retried = await apiState();
  assert.equal(retried.revision, beforeFailure.revision + 1);
  assert.deepEqual(retried.workspace.messages.slice(0, 3), beforeFailure.workspace.messages);
  assert.equal(retried.workspace.messages[3].text, fixture.chunks.join(""));
  assert.equal(retried.workspace.messages.filter((message) => message.text === fixture.failure_message).length, 1);
  assert.deepEqual((await providerState()).message_ids, [firstSaved.workspace.messages[0].id, failedUser.id, failedUser.id]);
  assert.equal(uiRequests("/api/messages").length, 2);
  assert.equal(uiRequests("/api/chat").length, 3);
  noProposals(retried);
  assert.ok(await evaluate("window.__smokeChatStreams.some(stream => stream.splitUtf8 > 0)"), "The decoder must have received a deliberately split multibyte character.");
  await transcript(retried);
  await composer(retryDraft, false);
  assert.equal(await visible("retry-reply"), false);

  console.log("Checking scroll position and a lost completion after the server saved…");
  await input("message-text", "Synthetic lost-completion test: keep the saved answer rather than asking twice.");
  await evaluate("window.__smokeDropCompletion = true");
  await click("save-message");
  await partial(fixture.chunks[0]);
  const lostUser = (await apiState()).workspace.messages.at(-1);
  await evaluate("const list = document.getElementById('message-list'); list.scrollTop = 0; list.dispatchEvent(new Event('scroll'))");
  assert.equal(await visible("jump-latest"), true);
  await advance(4, 1);
  await partial(fixture.chunks[0] + fixture.chunks[1]);
  assert.equal(await evaluate("document.getElementById('message-list').scrollTop"), 0, "Streaming must not drag a reader away from older messages.");
  await advance(4, 2);
  await waitFor(async () => (await apiState()).workspace.messages.length === 6, "server saves reply whose completion event is lost");
  await idle();
  const savedDespiteDrop = await apiState();
  assert.equal(await visible("retry-reply"), true);
  assert.match(await text("message-list"), /saving was not confirmed/i);
  assert.equal(await visible("sync-notice"), true);
  const requestsBeforeReconcile = requests.length;
  await click("retry-reply");
  await idle();
  const reconciled = await apiState();
  assert.deepEqual(reconciled, savedDespiteDrop);
  assert.equal(requests.slice(requestsBeforeReconcile).some((request) => request.method === "POST"), false,
    "A retry after a committed answer must only reload, never save or call the model again.");
  await transcript(reconciled);
  noProposals(reconciled);
  assert.equal(await visible("retry-reply"), false);
  assert.equal(await visible("sync-notice"), false);
  assert.equal(await evaluate("document.getElementById('message-list').scrollTop"), 0, "Reconciliation must preserve reading position too.");
  await click("jump-latest");
  assert.ok(await evaluate("document.getElementById('message-list').scrollTop > 0"));

  console.log("Checking manual-only save, explicit context, review, and durable invalid-link warnings…");
  await input("message-text", "");
  await clickSelector("#bench-tools > summary");
  assert.equal(await count("#message-context input[type=checkbox]"), 6);
  assert.equal(await count("#message-list input[type=checkbox]"), 0);
  await input("provider-select", "manual");
  const manualStart = requests.length;
  const manualText = "Synthetic manual-only message: save locally without inference.";
  await click("message-text");
  await type(manualText);
  await enter();
  await waitFor(async () => (await apiState()).workspace.messages.length === 7, "manual-mode local save");
  await idle();
  const manualSaved = await apiState();
  assert.equal(manualSaved.revision, reconciled.revision + 1);
  assert.equal(manualSaved.workspace.messages.at(-1).text, manualText);
  assert.equal(manualSaved.workspace.messages.at(-1).role, "user");
  noProposals(manualSaved);
  await transcript(manualSaved);
  await composer("", false);
  assert.equal(requests.slice(manualStart).some((request) => ["/api/chat", "/api/generate"].includes(request.path)), false);
  await click("messages-clear");
  assert.equal(await count("#message-context input:checked"), 0);
  await click("messages-all");
  assert.equal(await count("#message-context input:checked"), manualSaved.workspace.messages.length);
  await input("task-select", "brief");
  await click("prepare-packet");
  await waitFor(() => value("manual-prompt").then((prompt) => prompt.includes(firstLine) && prompt.includes(manualText)), "manual context packet");
  await idle();
  const frozenPrompt = await value("manual-prompt");
  const frozen = uiRequests("/api/context").at(-1).body;
  assert.deepEqual(frozen.selection.message_ids, manualSaved.workspace.messages.map((message) => message.id));
  await click("messages-clear");
  await input("task-select", "conversation");
  assert.equal(await value("manual-prompt"), frozenPrompt, "Live selection/task changes must not rebuild a frozen packet.");
  const reply = {
    assistant_text: "A synthetic reflection. " + fixture.untrusted,
    proposed_statements: [{ text: "Enjoys bicycle repair", domain: "work", source: "explicit", evidence: [{ message_id: firstSaved.workspace.messages[0].id, quote: "I enjoy repairing bicycles." }] }],
    opportunities: [{ title: "Explore repair work", kind: "career", rationale: "An unverified hypothesis", statement_ids: [firstSaved.workspace.messages[0].id], unknowns: ["Whether paid work would feel the same"] }],
    brief: { title: "A synthetic plan", summary: "Explore hands-on work without assuming it is a career fit.", next_steps: ["Talk with someone doing repair work"] },
  };
  await input("manual-reply", JSON.stringify(reply));
  await click("submit-reply");
  await waitFor(() => count(".proposal").then((total) => total === 1), "review cards");
  await idle();
  const staged = await apiState();
  assert.deepEqual(uiRequests("/api/proposals/manual").at(-1).body, { ...frozen, reply_text: JSON.stringify(reply) });
  assert.equal(await count(".proposal input:checked"), 0);
  assert.equal(staged.workspace.statements.length, 0);
  assert.equal(staged.workspace.opportunities.length, 0);
  assert.equal(staged.workspace.brief, null);
  assert.equal(staged.workspace.proposals.length, 1);
  assert.ok((await text("proposal-list")).includes("Unverified Person Map links were omitted"));
  await transcript(staged);
  await evaluate("document.querySelectorAll('.proposal-item input[type=checkbox]').forEach(e => e.click())");
  await clickSelector("[data-accept-proposal]");
  await waitFor(async () => (await apiState()).workspace.statements.length === 1, "explicit acceptance");
  await idle();
  const accepted = await apiState();
  assert.equal(accepted.workspace.proposals.length, 0);
  assert.equal(accepted.workspace.opportunities.length, 1);
  assert.deepEqual(accepted.workspace.opportunities[0].statement_ids, []);
  assert.equal(accepted.workspace.opportunities[0].unverified_links, true);
  assert.ok((await text("opportunity-list")).includes("Unverified Person Map links were omitted"));
  assert.equal(accepted.workspace.brief.title, "A synthetic plan");
  assert.equal(accepted.workspace.statements[0].confirmed, true);
  assert.equal(accepted.workspace.statements[0].source, "explicit");
  assert.equal(accepted.workspace.statements[0].evidence[0].message_id, firstSaved.workspace.messages[0].id);
  assert.equal(await evaluate("document.getElementById('export-brief').disabled"), false);

  console.log("Checking stale manual packets, narrow layouts, and reload persistence…");
  await input("task-select", "conversation");
  await click("messages-all");
  await click("prepare-packet");
  await waitFor(() => visible("manual-packet"), "second frozen packet");
  await idle();
  const stalePacket = uiRequests("/api/context").at(-1).body;
  const stalePrompt = await value("manual-prompt");
  const staleReply = JSON.stringify({ assistant_text: "Synthetic stale manual reply must never be saved." });
  await input("manual-reply", staleReply);
  await input("message-text", "Synthetic manual follow-up invalidates the frozen packet.");
  await click("save-message");
  await waitFor(async () => (await apiState()).revision === accepted.revision + 1, "manual save while packet is frozen");
  await idle();
  const afterManualFollowup = await apiState();
  assert.equal(await visible("packet-stale"), true);
  assert.equal(await value("manual-prompt"), stalePrompt);
  assert.equal(await value("manual-reply"), staleReply);
  await click("submit-reply");
  await waitFor(() => visible("error-notice"), "stale packet rejection");
  await idle();
  const staleRequest = uiRequests("/api/proposals/manual").at(-1);
  assert.equal(staleRequest.status, 409);
  assert.deepEqual(staleRequest.body, { ...stalePacket, reply_text: staleReply });
  assert.deepEqual(await apiState(), afterManualFollowup, "Stale manual reply must never be saved or silently rebased.");
  assert.match(await text("error-notice"), /workspace changed|stale|reload/i);
  await click("reload-state");
  await idle();
  assert.equal(await visible("packet-stale"), true);
  assert.equal(await value("manual-prompt"), stalePrompt, "State reload must retain the frozen packet.");
  assert.equal(await value("manual-reply"), staleReply, "State reload must retain the pasted reply.");
  await narrowLayout("Open advanced tools and frozen packet");
  await click("discard-packet");
  await waitFor(() => evaluate("document.getElementById('confirm-dialog').open"), "explicit discard confirmation");
  await click("confirm-submit");
  await waitFor(() => visible("manual-packet").then((shown) => !shown), "packet discard");
  assert.equal(await value("manual-reply"), "");
  await clickSelector("#bench-tools > summary");
  await narrowLayout("Collapsed tools and long chat bubbles");
  await rpc("Page.reload");
  await waitFor(() => evaluate(`document.getElementById('main') && !document.getElementById('main').hidden && document.querySelectorAll('#message-list .message-card').length === ${afterManualFollowup.workspace.messages.length}`), "saved transcript after page reload");
  await waitFor(() => value("model-select").then((model) => model === fixture.model), "model selection after reload");
  await idle();
  assert.deepEqual(await apiState(), afterManualFollowup);
  await transcript(afterManualFollowup);
  assert.equal(await evaluate("document.getElementById('bench-tools').open"), false);
  assert.equal(await visible("retry-reply"), true, "A saved unanswered user message must be retryable after reload.");
  const finalProvider = await providerState();
  assert.deepEqual(finalProvider.message_ids, [firstSaved.workspace.messages[0].id, failedUser.id, failedUser.id, lostUser.id]);
  assert.equal(finalProvider.generate_calls, 0, "Neither chat nor manual handoff may call structured generation.");
  assert.equal(uiRequests("/api/generate").length, 0);
  assert.equal(uiRequests("/api/chat").length, 4);
  assert.equal(uiRequests("/api/messages").length, 5);
  console.log("Checking 18-message proposal sizing, stale checks, and safe failure recovery…");
  let largeState = await apiState();
  while (largeState.workspace.messages.length < 18) {
    largeState = await api("/api/messages", {
      revision: largeState.revision,
      text: "Synthetic context-size test: I enjoy practical repairs and time outdoors. ".repeat(5),
    });
  }
  await click("reload-state");
  await idle();
  await clickSelector("#bench-tools > summary");
  const beforeSizing = await providerState();
  const generatesBeforeSizing = uiRequests("/api/generate").length;
  await click("messages-all");
  await waitFor(() => text("input-size").then((value) => value.includes("Too much context")), "18-message input warning beside Generate");
  assert.equal(await count("#message-context input:checked"), 18);
  assert.equal(await evaluate("document.getElementById('generate-button').disabled"), true);
  assert.equal(await count("#message-context input:disabled"), 0, "An oversize selection must remain editable.");
  assert.equal(await visible("sync-notice"), false);
  assert.equal(uiRequests("/api/generate").length, generatesBeforeSizing);
  assert.deepEqual(await providerState(), beforeSizing, "Sizing must not even call model discovery.");
  assert.deepEqual(await apiState(), largeState, "Sizing cannot change saved data.");
  await narrowLayout("Oversized selected-context notice");

  // Return an older fitting check after a newer oversized check. Only the
  // current revision/selection may decide whether Generate is enabled.
  await evaluate(`(() => {
    const originalFetch = window.fetch.bind(window);
    window.fetch = async (...args) => {
      if (args[0] === '/api/context/size' && window.__smokeFailSize) {
        window.__smokeFailSize = false;
        throw new TypeError('Synthetic size-check transport failure');
      }
      if (args[0] === '/api/generate' && window.__smokeUnknownFailure) {
        window.__smokeUnknownFailure = false;
        return new Response(JSON.stringify({detail:'Synthetic uncertain server error.'}), {status:500, headers:{'Content-Type':'application/json'}});
      }
      const response = await originalFetch(...args);
      if (args[0] === '/api/context/size' && window.__smokeHoldNextSize) {
        window.__smokeHoldNextSize = false;
        window.__smokeSizePaused = true;
        await new Promise(resolve => { window.__smokeReleaseSize = resolve; });
      }
      return response;
    };
  })()`);
  await click("messages-clear");
  await click("statements-clear");
  await evaluate("window.__smokeHoldNextSize = true; document.querySelector('#message-context label:last-child input').click()");
  await waitFor(() => evaluate("window.__smokeSizePaused === true"), "held old size check");
  await click("messages-all");
  await waitFor(() => text("input-size").then((value) => value.includes("Too much context")), "new oversized check");
  await evaluate("window.__smokeReleaseSize()");
  await sleep(250);
  assert.match(await text("input-size"), /Too much context/);
  assert.equal(await evaluate("document.getElementById('generate-button').disabled"), true, "Late fitting responses must not enable an oversized selection.");
  assert.deepEqual(await providerState(), beforeSizing);

  await click("messages-clear");
  await evaluate("window.__smokeFailSize = true; document.querySelector('#message-context label:last-child input').click()");
  await waitFor(() => visible("recheck-size"), "read-only sizing failure recovery");
  assert.equal(await visible("sync-notice"), false, "A failed read-only size check must not imply an uncertain write.");
  assert.equal(await count("#message-context input:disabled"), 0);
  await click("recheck-size");
  await waitFor(() => evaluate("!document.getElementById('generate-button').disabled"), "small fitting selection");
  assert.match(await text("input-size"), /Local input fits/);
  const smallerSelection = await evaluate("Array.from(document.querySelectorAll('#message-context input:checked')).length");
  assert.equal(smallerSelection, 1);
  await click("generate-button");
  await waitFor(() => visible("bench-notice"), "in-place proposal outcome");
  await idle();
  assert.match(await text("bench-notice"), /Synthetic proposal provider failure/);
  assert.deepEqual(await apiState(), largeState);
  assert.equal(await visible("sync-notice"), false, "Known provider failure confirms no state change; no reload lock is appropriate.");
  assert.equal(await count("#message-context input:disabled"), 0);
  assert.equal(await evaluate("document.getElementById('generate-button').disabled"), false);
  assert.ok(await evaluate("(() => { const r = document.getElementById('bench-notice').getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; })()"), "The failure must be brought into view, not left above the workspace.");
  const failedGenerate = uiRequests("/api/generate").at(-1);
  assert.equal(failedGenerate.status, 502);
  const reloadedBeforeRetry = uiRequests("/api/state", "GET").length;
  await click("generate-button");
  await waitFor(async () => (await apiState()).workspace.proposals.length === 1, "successful retry stages a reviewed proposal");
  await idle();
  const proposed = await apiState();
  assert.equal(proposed.revision, largeState.revision + 1);
  assert.equal(proposed.workspace.messages.length, 19);
  assert.deepEqual(proposed.workspace.statements, largeState.workspace.statements, "Generation must not accept map changes.");
  assert.deepEqual(proposed.workspace.proposals[0].selection.message_ids, [largeState.workspace.messages.at(-1).id]);
  assert.equal(uiRequests("/api/state", "GET").length, reloadedBeforeRetry, "Safe failure can be retried without reloading.");
  assert.equal((await providerState()).generate_calls, 2);

  await waitFor(() => evaluate("!document.getElementById('generate-button').disabled"), "size check at the new revision");
  await evaluate("window.__smokeUnknownFailure = true");
  await click("generate-button");
  await idle();
  assert.equal(await visible("sync-notice"), true, "Unknown 500s must still require reconciliation.");
  assert.equal(await visible("bench-reload-state"), true);
  assert.ok(await count("#message-context input:disabled") > 0);
  await click("bench-reload-state");
  await idle();
  await waitFor(() => evaluate("!document.getElementById('generate-button').disabled"), "local recovery control unlocks after reload");
  assert.equal(await visible("sync-notice"), false);
  assert.deepEqual(await apiState(), proposed);
  assert.equal((await providerState()).generate_calls, 2);
  assert.equal(serverErrors.includes(fixture.private_error), false);
  const database = await readFile(join(temporary, "workspace", "workspace.sqlite3"));
  assert.equal(database.includes(Buffer.from(firstLine)), false, "Synthetic private text must not be plaintext in SQLite.");
  assert.deepEqual(blockedRequests, [], "The page must not attempt requests outside the synthetic server.");
  assert.deepEqual(errors, []);
  console.log("PASS: streaming chat, keyboard/drafts/retry, manual handoff/review, narrow layouts, persistence, 18-message size rejection, late preflight responses, safe proposal retry, and unknown-error reconciliation.");
  console.log("Synthetic provider only; temporary DPAPI workspace/profile removed on exit. No Ollama, Edge, browser downloads, or user workspace.");
} finally {
  closing = true;
  clearTimeout(deadlineTimer);
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ id: ++sequence, method: "Browser.close" }));
    await sleep(300);
    ws.close();
  }
  rejectPending(new Error("Browser smoke cleanup."));
  browser?.kill();
  server?.kill();
  await sleep(500);
  await rm(temporary, { recursive: true, force: true, maxRetries: 10, retryDelay: 300 });
}
