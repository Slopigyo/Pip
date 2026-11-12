# Pip chat-first prototype

Pip — **Personal Insight Pathfinder** (formerly JobWolf) — is a Windows-local experiment for **conversation → reviewed Person Map → exploratory possibilities → portable insight/Search Brief**. It prioritizes useful self-insight, not a job index. The initial scope is the contiguous 48 United States plus Washington, DC; Alaska, Hawaii, and territories are not included.


## Start on Windows

This guide covers **v0.2.0**, the résumé and local dictation milestone. Check the code's version with `.venv\Scripts\python.exe -m jobwolf --version`; this does not open the workspace or start a model.

Python 3.14 is the reference development/CI environment. The package requires Python 3.12+; other Python versions have not been covered by the single-version CI configuration. Windows is required for the runtime storage adapter.

From PowerShell in the project folder:

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -c constraints.txt -e ".[test]"
.venv\Scripts\python.exe -m jobwolf
```

Installation downloads Python packages from PyPI; it does not upload a profile, buy a service, or download a model. Later launches need only the last command, or double-click `start-bench.cmd` after installation.

The app opens a browser on `127.0.0.1:8765`. Its terminal prints a startup link with a per-run access token in the URL fragment. Treat that link as private. Leave the terminal open; Ctrl+C stops the app. Restarting changes the token, so reopen the new startup link. `--no-browser` only prints the link; `--port 8766` chooses another port. Do not expose this prototype through a tunnel or reverse proxy.

Ollama should already be running with a suitable local model installed. **Lyra:latest** is a historical development-bench preference and is selected when present, not a required or bundled model. Choose your own installed model in the interface; model selection never starts inference. No chat models are downloaded automatically. The app uses `http://127.0.0.1:11434`; `--ollama-url` accepts only HTTP loopback endpoints.

## Chat first

1. Choose **Ollama** and an installed model.
2. Type a natural message and press **Enter**. This saves the user message locally and starts a streaming Ollama reply.
3. Use **Shift+Enter** for a newline. The composer stays editable while Pip replies, so you can type your next draft while waiting; the draft is not sent automatically.

Ordinary chat does **not** automatically mutate the Person Map or produce proposals. Structured extraction, discovery, and briefs are separate, deliberate bench actions.

### Bounded context and saved replies

Chat includes the newest saved user message and its explicitly attached résumé excerpts whole, then as much consecutive recent history as fits, up to **12 messages total**. Reviewed Person Map statements are added only within the remaining conservative input-byte allowance for the selected local context window (**123,904 bytes by default**), including instructions and serialized framing. This is bounded context, not the entire conversation or map. Saved text is not shortened or deleted to fit; if the newest message plus attachments is too large, it stays saved and you need a shorter follow-up with fewer document lines. Retry uses the same saved attachments. Earlier messages' attachments are not automatically loaded again; conversation text and reviewed map notes can still repeat source information.

The first stream event, `context`, discloses the selected context and counts of omitted messages and map statements. The UI shows omission counts while waiting and streaming, and keeps the selected IDs/source text in **What Pip used for the last chat reply** under the bench tools, so you can inspect the limits of what Pip is using. Structured-task checkboxes do not control this automatic chat selection.

Assistant text is transient while streaming and is saved only after the complete reply is received and validated. A failed or incomplete stream keeps the already-saved user message but does not save partial assistant text. **Retry reply** first refetches workspace state to check whether a reply was already saved, then retries the latest unanswered user message if needed, without saving a duplicate user turn. There is no automatic retry.

### Local context window

**128,000 tokens is now the default requested Ollama window**, for both chat and structured proposals. In **Connection & privacy → Local context window (tokens)**, choose 16,384, 32,768, 65,536, or 128,000. Changes apply to the next request (including a retry), trigger a fresh structured size check, and do not modify the workspace, select extra sources, or call a model. The choice lasts for the current page session; a reload restores 128,000.

Token capacity and input bytes are **not interchangeable**. Pip does not have an exact tokenizer for each installed model. Its preflight conservatively allows one input byte per context token after reserving **4,096 tokens** for output and model framing:

| Requested window (tokens) | Conservative input guard (bytes) |
|---|---:|
| 16,384 | 12,288 |
| 32,768 | 28,672 |
| 65,536 | 61,440 |
| 128,000 | 123,904 |

This is a safety policy, **not a token count or full utilization of 128K tokens of input**. Typical English uses far fewer tokens than bytes, so substantial capacity is deliberately left unused until model-specific token counting is available. The UI shows both units explicitly. Selected text is not silently shortened to pass the check. The model must actually support the window; requesting it cannot extend a model's supported capacity.

A 128K window can substantially increase KV-cache memory, latency, or CPU/system-RAM offload. A model advertising 128K does not guarantee that it fits in a 24 GB GPU. If the model fails or becomes too slow, lower this setting and retry. No inference or hardware-allocation test runs merely by changing the setting. The existing 12-message chat-history bound, 24-message bench selection bound, output limits, and explicit document-selection rules remain unchanged. Manual handoff retains its independent 65,536-byte packet limit and is not controlled by this Ollama setting.

## Optional local dictation (Granite)

Dictation uses **IBM Granite Speech 5.0 2B, Q8**, separately from the Ollama chat model. It is speech input, not a hands-free voice assistant.

**One-time Windows x64 setup:** after installing the bench environment, double-click `setup-speech.cmd`, or run:

```powershell
.venv\Scripts\python.exe -m jobwolf.speech_setup --install
```

This explicitly downloads about **3.2 GB** from GitHub/Hugging Face: pinned llama.cpp **b11935** Windows Vulkan binaries and IBM's model/audio encoder. Setup verifies sizes and SHA-256 hashes, retains third-party licenses, and installs no Python packages or GPU drivers. Allow roughly **4.5 GB of free disk space** for assets and staging. Repeating setup verifies/repairs designated assets rather than downloading valid ones again; it refuses to replace an unrecognized runtime directory. Without `--install`, the command only reports local presence, without hashing, downloading, or loading a model.

Resources live in `%LOCALAPPDATA%\JobWolfBench\speech`, independently of the private database and `--data-dir`. For an isolated installation, add `--directory "C:\your\speech-folder"` to setup and `--speech-dir "C:\your\speech-folder"` to the app launcher. There is no workspace migration or reset. The installed assets remain on disk between sessions; GPU/model memory does not.

1. Restart Pip if upgrading from a version without speech, and open its new startup link in a current Chrome or Firefox.
2. Choose **Dictate** below the composer. On the first use in a tab it checks setup only; once it says Ready, press **Dictate** again and allow microphone access.
3. Speak, then choose **Stop & transcribe**. Recording also stops and transcribes at **two minutes**. **Cancel** discards the recording/result. You can continue typing while it works; sending and notebook actions wait until dictation finishes or is canceled.
4. The transcript is appended to your **current editable draft**, never automatically sent. Review words, names, numbers, and corrections, then press Enter/Send yourself. If it would exceed the composer's 12,000-character limit, it is held separately for editing/copying rather than cut or overwritten.

**Privacy and resource handling:** a local AudioWorklet captures mono PCM16 at the browser's native audio rate (supported range: 8–96 kHz). After stopping the microphone, the browser's built-in offline audio renderer converts it to the mono 16 kHz WAV required by Granite. This avoids Firefox's inability to connect microphone input to a forced non-native-rate audio context. The two-minute cap applies at the actual capture rate; cancellation during conversion discards the result without sending it. No browser/cloud speech service is used. Only audio and a fixed transcription instruction go to a short-lived, authenticated loopback llama.cpp process; no conversation, résumé, selected context, or Person Map is supplied. There is no second-model cleanup or automatic translation step. The app writes neither raw audio files nor an unsent transcript to the workspace. Microphone resources are released on stop/cancel/failure; the runtime is terminated and reaped after transcription. Memory is not a secure-erasure guarantee: OS paging/crash dumps, browser extensions, and other processes running as you remain outside this protection. If you send the resulting text, it becomes an ordinary saved user message under the chosen chat workflow.

The local runtime has tools/web UI disabled, no prompt logs or persistent prompt cache, explicit local model paths, and offline mode; it does not use ambient proxies or provider credentials. Setup is the only speech step that intentionally contacts public download hosts. Status checks do not start a model or verify GPU readiness.

**Latency and limits:** each transcription includes a cold model load, avoiding a permanently resident second model. Chat, structured generation, and speech share one in-app inference lock, but an idle Ollama model can still occupy VRAM. Speech has a **225-second processing deadline**, plus bounded cleanup; the page waits up to 240 seconds. Cancel/closing the tab releases the microphone and discards late results but may not immediately stop an already-running bounded transcription. Other inference requests may remain busy until cleanup finishes. No automatic retry or cloud fallback occurs.

On the development RX 7900 XTX, the pinned Vulkan runtime detected the GPU and one **6.75-second synthetic sentence** transcribed correctly in **8.33 seconds including load and cleanup**. This is a compatibility smoke check, not a throughput benchmark or proof of real-voice/long-recording accuracy. Start with short personal trials. The level guard rejects silent/near-silent recordings but is not voice detection; background noise can still produce invented words. If startup fails, verify assets with setup, check the AMD Vulkan driver, and free GPU memory if needed. Keyboard input always remains available.

If recording fails immediately, the local recorder script may be blocked or the device may be unavailable. Script-load errors now identify that step; check the blocker’s report for `/assets/speech-recorder.js` and use a site-specific exception only if it is actually blocked. Do not disable tracking protection or extensions globally. The native-rate capture fix itself requires no blocker changes or model reinstall; preserve unsent drafts and refresh the page to load updated scripts.

## Optional résumé upload

1. Click **Add a résumé** below the chat input. Choose a **text-based PDF, DOCX, TXT, or Markdown** file, or paste text into the preview.
2. Parsing is local and calls no model. Check the extracted text, correct layout/date mistakes, remove unnecessary contact details or other sensitive material, and use a simple display name without a filesystem path. There is no automatic-redaction guarantee.
3. Click **Save reviewed text**. This adds a source to the notebook; it does **not** replace your workspace, call Ollama, or create map notes. Only approved text, format, display name, timestamp, ID, and SHA-256 snapshot hash are retained in encrypted storage. No original binary or parsing temporary file is saved; your original file is untouched.
4. In **Notebook & bench tools → Résumé sources**, inspect the numbered text and choose start/end lines. **Attach to next message** places that excerpt above the composer for review; write or edit your message and press Send. The displayed provider receives only those excerpts plus the normal bounded chat context. Pip is instructed to reflect tentatively and ask about preferences, changes, or what the résumé leaves out—not assume past work is your desired future.
5. Alternatively, **Select for bench task**, choose **Find notes for my Person Map**, and generate/review a proposal. Source-only tasks work without a saved conversation. A résumé claim is labeled **Document** and cites exact source lines/quotes, separately from direct user speech and inference. Acceptance does not change that provenance. Add a correction or confirmation in chat and select it for a new note; do not relabel a document quotation as user speech.

Up to **8 non-overlapping excerpts** may be selected per task/message. Selection is explicit: choosing a message does not automatically include its old attachments in a bench task. Saved documents are never automatically added to ordinary chat; reviewed map notes derived from them may be. Manual handoff freezes the chosen line ranges with the rest of the packet and sends nothing automatically. The same prompt-size checks apply; start with a small excerpt and select additional sections in later tasks rather than silently truncating a long résumé.

**Limits:** 2 MiB original; PDF 1–20 pages; approved text 60,000 characters / 2,000 lines; at most 20 source snapshots. TXT/Markdown accept UTF-8 or BOM-marked UTF-16. DOCX supports ordinary UTF-8 document/header/footer XML and table paragraphs, not layout reconstruction. No OCR, image résumé, legacy `.doc`, encrypted PDF, or macros. Complex formatting and tracked changes may be imperfect; use the editable preview or paste fallback. A 15-second parser timeout, bounded decompression/structure checks, and one preview worker at a time limit processing. The child process is not an OS sandbox or strict total-memory cap; this remains a local prototype.

**Citation handling:** structured prompts show explicit original line numbers, so the model does not have to count them. The per-request output schema allows message citations only to selected user messages; with none selected, it omits message evidence and disallows `source=explicit`. Résumé-only proposals do not require a conversation.

If a new proposal has a wrong locator or whitespace-only quote mismatch, Pip searches **only the selected excerpts of that same document**. It repairs the citation only when there is one exact match, or one match differing solely in whitespace; the stored quotation is copied from the original source with its actual line range. The same check can recover a selected document's ID mistakenly placed in message evidence: the verified quote becomes document evidence, never fabricated user speech. Arbitrary IDs are not guessed from quotations, selected message identities are never overridden, and invalid evidence is not silently discarded. An `explicit` note with only recovered document evidence becomes `document`; `inferred` stays inferred, and document claims still cannot establish hard limits.

Repaired notes are visibly flagged during proposal review, and the notice survives pending-proposal JSON export/import. Valid citations are left unchanged. There is no fuzzy matching, punctuation/case substitution, cross-document lookup, hidden context expansion, or extra model call. Ambiguous, paraphrased, or invented quotes still reject the proposal atomically; errors identify the note/citation position without echoing private text. A verified quote is not proof that the proposed interpretation is correct.

**Corrections:** use **Correct as new snapshot**. Existing text/citations remain unchanged; identical saved text is rejected as a duplicate. Inspect related map notes yourself—newer user corrections should take precedence, but exact-quote validation cannot prove semantic correctness or resolve every conflict.

**Deletion:** **Delete source…** previews directly citing map notes, message attachment references, and derived outputs. Confirming removes the source, directly citing notes/quotes, their relationship links, and all pending proposals, saved possibilities, and the brief (free text can repeat claims without formal links). It also clears this tab's frozen packet and last-chat preview. **Conversation text, uncited notes, other snapshots, original files, earlier exports and external copies remain** and may repeat the information. Review those separately or reset the active workspace for broader deletion; this is not forensic erasure.

## Notebook & bench tools

Expand the collapsed **Notebook & bench tools** (`bench-tools`) when you deliberately want structured work:

1. Check exactly the saved messages, existing map statements, and document excerpts to include. **Structured tasks use only explicitly selected context**, not the automatic chat selection.
2. Choose **Find notes for my Person Map** (the `conversation` task), **Discover**, or **Brief**. For Ollama, wait for the local size check beside **Generate a proposal** to say the selection fits, then generate.
3. **Find notes for my Person Map** requests supported map statements, not possibilities or a Search Brief. If the selected context supports no new notes, Pip should explain that rather than invent any. Read the proposed changes, inspect evidence, source, conditions, uncertainty, and tensions, then check only the items you want to retain and accept them.
4. Edit the map directly when the interpretation is wrong. Source and your confirmation remain separate: accepting an inference does not turn it into an explicit quotation.
5. Request a brief, review it, and export Markdown. JSON export is the complete portable workspace, including conversations and pending proposals.

### Connected paths and a useful brief

**Discover — explore connected paths** asks for up to three distinct possibilities connecting relevant work, place, community, and everyday life. Each rationale should explain why it may fit and sketch a possible ordinary week with assumptions labeled. Tradeoffs remain explicit; unknowns start with a question that could change whether to pursue the path, and next steps start with a small, low-commitment investigation. Moving, homeownership, self-employment, and unpaid work are not assumed goals.

**Brief — compare paths and plan next steps** asks for a self-contained comparison: a short reflection on what matters and how the paths differ, individually presented path sketches, key questions, an ordered investigation plan, and public search vocabulary. The brief uses only selected messages, map notes, and document excerpts; saved possibilities are **not automatically supplied** to the model. The Markdown export includes the current Person Map and saved possibilities as clearly separate reference sections, with map connections and stale/link warnings retained.

The connected-path changes are prompt and presentation improvements, not proof of model quality or verified research. The existing `rationale`, `tradeoffs`, `unknowns`, `next_steps`, and brief fields carry the richer content; those connected-path changes need no new storage format, model download, or workspace reset (résumé intake has the separate v2 migration below). Earlier saved results remain readable but are not automatically rewritten into richer paths. They remain separate from Person Map extraction and ordinary chat. Both Ollama and newly prepared manual packets use the updated instructions.

### Context size and review

The local size check counts the exact serialized bytes of context, instructions, and output schema—not exact tokens. It calls neither Ollama nor model discovery and does not alter your workspace. Its guard follows the requested local window: 123,904 bytes by default, or 12,288 at the smaller 16,384-token setting. The 24-message selection limit is **not** a promise that 24 messages fit. If the warning says **Too much context**, select a larger local window or fewer/shorter messages, map notes, or document excerpts. Nothing is silently dropped, summarized, or sent. Manual handoff retains its separate, independent budget.

Progress and errors appear beside the bench controls. An oversized selection or a known provider failure that saved nothing leaves the controls editable; you can adjust context or retry without reloading. A stale workspace, lost connection during a write, or unknown server/storage failure still requires reconciliation, with **Reload state to continue** available next to the task.

A backlog of structured proposals does not block ordinary chat. Normal workspace limits and the shared local-inference lock still apply.

Possibilities are **unverified hypotheses**. There is no built-in browsing, live job search, sourced world-data catalog, salary verification, or affordability calculator. LLM insight is useful here without pretending that model knowledge is verified research. Search queries in a brief are text to take elsewhere, not automatic network requests.

Feedback on a possibility becomes a user conversation message and can be selected for the next structured task. It does not silently rewrite preferences. Changing the map marks previous briefs and possibilities stale. Deleting a statement clears derived outputs and pending proposals, but does not erase the original conversation; full reset clears the active workspace.

## Manual provider: local save, then handoff

With **Manual handoff — Zed / ChatGPT** selected, **Enter** saves your message locally but does not stream a reply or contact a subscription service. Expand **Notebook & bench tools**, select saved context and a structured task, prepare a prompt, inspect it, then copy it into the chat interface you already use. Paste that interface's JSON reply into the bench and validate it before review. A complete fenced `json` block is accepted too.

This deliberately works without an API account, a paid middleware service, browser automation, or extracting authentication cookies. Pip does not reuse Zed's credentials or provide or promise subscription login. Subscription entitlement and automated API access are not interchangeable guarantees; the handoff remains manual.

Copying is an explicit privacy decision. The selected context goes to the other tool's provider when you paste/send it, under that provider's terms. Windows clipboard history/sync may also retain it. No clipboard history is managed or cleared by JobWolf.

The prompt is frozen at its original workspace revision. If you edit or save something before importing the reply, prepare a new prompt. Stale responses are rejected rather than silently applied to different context.

## Storage, backup, and recovery

Default database:

```text
%LOCALAPPDATA%\JobWolfBench\workspace.sqlite3
```

Pip reuses this existing JobWolf workspace. The `jobwolf` package, `start-bench.cmd` launcher, storage identifiers, and wire-format names remain unchanged. The chat-first UI rebrand requires no workspace reset or data migration.

Structured JSON is canonical and is encrypted **before** being stored in SQLite with Windows user-scoped DPAPI. This is encrypted payload storage, **not whole-file SQLCipher encryption**. Table names, revisions, sizes, and SQLite headers remain visible. No API keys or app encryption keys are stored in the database. No plaintext fallback exists.

DPAPI is a Windows OS facility, not a new commercial encryption library. Backups are generally tied to the Windows account/machine and its recovery material. Do not assume copying the database to another computer is sufficient. Use an explicit **plaintext JSON export** to move profiles or prepare recovery before reinstalling Windows. Protect that file yourself; it contains highly sensitive information. Import is version-checked, validated, and replaces rather than merges data, after confirmation.

Encryption at rest does not protect data in browser/server/model memory or from malware running as you. It does not cover exports, copied prompts, screenshots, OS/cloud backups, or content already sent elsewhere. Reset deletes active application records, not previously exported copies or guaranteed forensic traces on storage media. Decryption/schema failures never silently reset your data.

The app has no telemetry, external web assets, public-search calls, or automatic external-AI fallback. Its chat adapter contacts only the configured loopback Ollama service and ignores ambient HTTP proxies. Optional dictation contacts only its owned loopback speech runtime after explicit asset setup, as described above. Ollama is a separate program: use genuinely local model weights and configure its own cloud/network settings appropriately. Localhost transport alone cannot control another process's network behavior.

## Limits and troubleshooting

- Model output can still misunderstand you. For structured tasks, schema checks and exact evidence references are safeguards, not proof of semantic truth. Review remains essential.
- Chat requests **128,000 context tokens by default**, adjustable down to 16,384. It still allows at most **1,536 output tokens** and a **12,000-character reply**. Its automatic input selection uses the conservative byte guard described above.
- Structured bench calls use the **same selected token window as chat** (default 128,000), at most **3,072 output tokens**, and its conservative byte guard (default **123,904 bytes**) including schema and instructions. Preflight and the adapter enforce the same choice before model inference. Manual packets still allow up to **65,536 input bytes** including instructions/schema.
- Workspace limits: 500 conversation messages, 200 statements, 100 saved possibilities, 10 pending proposals, and 20 document snapshots. Completed assistant replies count toward the message limit. Export/reset for a new experiment rather than treating this as unlimited archival software.
- At most one in-app local inference (chat, structured, or speech) runs at a time. Chat/structured calls have **no instant Stop/Cancel**; dictation has a separate discard/cancel flow described above. Closing the tab or disconnecting does not guarantee immediate cancellation: a blocking provider read can hold the inference lock until it returns or times out (**180 seconds** by default). Wait for the lock to release; **Retry reply** refetches state before retrying chat. Ollama may still be processing after a client disconnect.
- If a possibility contains an optional Person Map link outside the selected context, the invalid link is omitted without guessing a replacement or expanding the context. A visible warning remains with the proposal, saved possibility, and JSON/Markdown exports. The possibility stays an unverified hypothesis and still requires review. This recovery needs no additional model call.
- For structured tasks, invalid JSON, fabricated quotations, invalid personal-statement evidence/relationships, and inferred or document-derived hard limits still reject the proposed state change; the saved user message remains. Retry, reduce the task, or use manual handoff. No raw private response is logged.
- AMD GPU use is controlled by your Ollama installation. `ollama ps` shows actual placement; 24 GB VRAM does not guarantee every model/context combination fits entirely on GPU.
- This is a bench, not a signed installer or a public product. There are no OAuth integrations, mobile app, password recovery service, automatic updates, or cross-device synchronization yet.

## Development and tests

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
node --check src/jobwolf/web/app.js
```

For résumé changes alone, run `.venv\Scripts\python.exe -m unittest discover -s tests -p test_documents.py -v` and the JS syntax check; broaden only when affected interfaces warrant it.

For speech changes, use `.venv\Scripts\python.exe -m unittest discover -s tests -p test_speech.py`, the setup-specific `test_speech_setup.py` when installer behavior changes, and `node --test tests/test_speech_ui.mjs` for mocked capture/composer lifecycle checks. These use synthetic audio/temporary assets and no model downloads. No full suite or browser sweep is needed for a narrow speech edit.

The unit/HTTP tests use synthetic data and never invoke a model. Windows storage tests exercise actual DPAPI using temporary directories outside the repository. No real conversations belong in committed fixtures.

Opt-in live checks (not part of unittest discovery):

For either Chrome script, explicitly configure an **existing** Chrome/Chromium executable. There is no machine-specific default, automatic discovery, download, or Edge fallback. From PowerShell, enter the full path without surrounding quotes:

```powershell
$env:CHROME_EXECUTABLE_PATH = Read-Host 'Full path to existing chrome.exe or chromium.exe'
```

Run only the check relevant to the change. These browser checks use disposable profiles, not your normal browser profile.

- For capture compatibility only, `node tests/smoke_speech_capture.mjs .venv/Scripts/python.exe` checks the configured Chrome/Chromium, and `.venv\Scripts\python.exe tests\smoke_speech_firefox.py "C:\Program Files\Mozilla Firefox\firefox.exe"` checks an existing Firefox installation. Each uses a disposable profile, fake microphone, real UI/recorder scripts, and a synthetic transcription response. They check native-rate capture, valid 16 kHz WAV output, draft preservation, and microphone release, without accessing a real microphone, user workspace, or model. These are narrow optional regressions, not a browser sweep to run after every edit.

- `.venv\Scripts\python.exe tests\smoke_speech.py --run` uses the installed Windows voice to synthesize a fixed sentence in memory, then runs Granite **once** against already-installed assets. It reads no private workspace or microphone and reports cold transcription latency. This is a developer smoke tool, not a text-to-speech app feature or an assessment of the user's speech.

- `.venv\Scripts\python.exe tests\smoke_ollama.py --model Lyra:latest` runs two structured local-model requests against a synthetic profile in a temporary encrypted database, then removes it. Add **`--chat-only`** to check one real streaming reply instead, including complete-only persistence and no map/proposal mutation. Model variability can cause a safe validation failure; a passing run is not a guarantee of reasoning quality.
- `node tests/smoke_browser.mjs .venv/Scripts/python.exe` uses the configured `CHROME_EXECUTABLE_PATH`; an optional second positional argument can override it with an absolute path to an existing Chrome/Chromium executable. The browser smoke uses a **delayed fake streaming provider** in `tests/browser_server.py` and a **temporary encrypted database** to exercise streaming, split UTF-8/NDJSON, Enter/Shift+Enter and IME guards, next-draft entry, failure/retry, a lost completion after a saved answer, scroll preservation, manual handoff, explicit review, safe text rendering, narrow layouts, and reload persistence. It uses temporary browser/profile directories, never downloads a browser, never uses Edge, and never invokes a real model. Optional `PIP_SMOKE_SCREENSHOT` writes a synthetic desktop screenshot to the supplied file path.

Modules:

- `models.py`: versioned workspace, distinct model-output schema, evidence/relationship types.
- `service.py`: context minimization, deterministic mutations, review, constraints, feedback, report export.
- `conversation.py`: bounded automatic chat context and explicit document attachments, separate from structured proposals.
- `context_limits.py`: shared token-window defaults/options and conservative byte-guard policy.
- `documents.py`: local, bounded file-to-text previews; no original-file persistence or model calls.
- `speech.py`: bounded audio validation and ephemeral local Granite adapter; no workspace access.
- `speech_assets.py` / `speech_setup.py`: pinned optional assets and explicit Windows installer; independent of inference and private storage.
- `storage.py`: encrypted SQLite persistence, revision checks, safe failure.
- `providers.py`: provider-independent handoff format and loopback-only Ollama generation/streaming adapter.
- `app.py` / `security.py`: authenticated local HTTP boundary and redacted errors.
- `web/`: dependency-free browser UI; untrusted text is never rendered as HTML.

**Workspace JSON is now version 2** to add document snapshots, excerpt selections, and document evidence. Version-1 workspaces/exports are upgraded in memory with empty document fields while preserving messages, IDs, map state, and revision; merely loading never writes a migration. The next ordinary revision-checked save persists v2. The SQLite encrypted-envelope version and default workspace path are unchanged; **no reset is needed**. JSON exports include all approved source text and citations; Markdown briefs include source labels/line references on map notes, not a full résumé dump. Older app builds may reject v2 exports. Unsupported future versions fail without resetting data. Keep a private JSON backup before moving between builds. This is not a complete historical audit of every map edit.
