# Pip — Personal Insight Pathfinder

**Places, Interests & Possibilities · Find Yourself.**

Pip (formerly **JobWolf**) is a private, local-first prototype that helps someone explore work, places, and life paths that fit them. A conventional job site starts with a title and location; Pip starts with the person.

The goal is useful self-insight and a practical exploration plan—not candidate scoring, résumé optimization, or mass applications.

**Status:** Windows-local work in progress, **v0.2.0** résumé and dictation milestone. This is an open testing bench, not a hosted service or production-ready installer. Initial geographic scope: the contiguous 48 United States plus Washington, DC.


## What the prototype includes

- **Chat-first exploration:** streaming replies from a locally installed Ollama model, with visible context limits.
- **A reviewed Person Map:** notes across seven domains covering capabilities, work, environment, life, place, finances, and direction. Model proposals require explicit review; ordinary chat never silently updates the map.
- **Optional résumé intake:** locally parse PDF, DOCX, TXT, or Markdown; inspect and correct the text; select excerpts deliberately. Document claims keep their source references.
- **Optional local dictation:** Granite Speech produces an editable draft. Recording does not send a message or modify the map.
- **Connected possibilities and Search Briefs:** explore how work, place, and everyday life could fit together, with tradeoffs, unknowns, and practical next investigations. These are model-generated hypotheses, not verified job listings.
- **Portability:** inspect and edit saved information, export the workspace as JSON or a brief as Markdown, and delete local sources or reset the active workspace.
- **Manual model handoff:** prepare selected context for an external chat tool and validate its pasted JSON response. This is not automatic ChatGPT/Claude subscription access.

The central boundary is deliberate:

```text
Selected conversation / résumé excerpts / reviewed notes
    → model proposes typed changes
    → schema and evidence validation
    → user review
    → deterministic local save
```

## Engineering focus

This project explores how to turn probabilistic model output into a user-controlled application:

- **State the model does not own.** Pydantic schemas, exact-source evidence checks, revision checks, and explicit acceptance separate suggestions from canonical state.
- **Privacy through architecture.** Private payloads are encrypted with Windows DPAPI before SQLite persistence. Context selection is bounded and inspectable; the app has no telemetry or automatic cloud fallback.
- **Small, inspectable components.** Python, FastAPI, Pydantic, SQLite, and plain HTML/CSS/JavaScript; no frontend build system or required paid backend. Ollama and optional speech inference are separate adapters.
- **Honest uncertainty.** Evidence, provenance, conflicting preferences, and open questions matter more than unexplained match percentages. Schema validity is not proof of reasoning quality.

Useful code entry points: [models](src/jobwolf/models.py), [application rules](src/jobwolf/service.py), [chat context](src/jobwolf/conversation.py), [document intake](src/jobwolf/documents.py), [speech adapter](src/jobwolf/speech.py), and [local HTTP boundary](src/jobwolf/security.py).

## Run locally on Windows

Requirements:

- **Windows** and **Python 3.12+**; Python **3.14** is the reference development/CI version. The current storage adapter requires Windows DPAPI.
- For streaming chat, a running [Ollama](https://ollama.com/) installation and an already-installed local model suited to your hardware. Pip does not install chat models for you.
- A current browser. Chrome and Firefox have been used for local capture checks. Node is only needed for JavaScript development checks, not to run Pip.

From PowerShell in the downloaded/cloned repository:

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -c constraints.txt -e .
.venv\Scripts\python.exe -m jobwolf
```

The app opens a private startup link on `127.0.0.1:8765`. Keep the terminal open; **Ctrl+C** stops it. After installation, `start-bench.cmd` is a convenient launcher. Do not share the startup link or expose the server through a public tunnel.

Choose **Ollama** and one of your installed models. Type and press **Enter**; **Shift+Enter** inserts a newline. The developer's custom `Lyra:latest` preference is not a required or bundled model.

The default requested local context is **128,000 tokens**, not a promise that your model or GPU can support it. Lower it under **Connection & privacy** if necessary. Input checks use a separate conservative byte guard, not an exact tokenizer.

**Optional speech:** run `setup-speech.cmd` once. This explicitly downloads approximately **3.2 GB** of pinned Granite Speech 5.0 2B Q8 and Windows Vulkan runtime assets; allow about **4.5 GB** free for setup. Then use **Dictate → Stop & transcribe → review/edit → Send**. No speech download occurs merely by opening Pip.

See [the prototype guide](docs/PROTOTYPE.md) for model selection, résumé intake, manual handoff, troubleshooting, and recovery. For a portfolio walkthrough without personal data, use [the synthetic demo guide](docs/PORTFOLIO.md).

## Privacy and current limits

- The default workspace remains `%LOCALAPPDATA%\JobWolfBench\workspace.sqlite3`. The historical `jobwolf` package, launchers, storage identifiers, and wire formats are intentionally unchanged.
- DPAPI protects stored payloads, **not the whole SQLite file** or data in memory. It does not protect against malware running as you. JSON/Markdown exports and copied prompts are plaintext; handle them as sensitive documents. Moving a DPAPI database alone is not a portable backup.
- Résumé text is saved only after review; original document binaries are not retained. Dictation sends only audio and a fixed instruction to the local speech runtime; the app does not save raw audio. Deletion cannot erase prior exports or external copies.
- The Ollama adapter is loopback-only. Ollama is a separate application: use local weights and review its own network/cloud settings. Pasting a manual handoff into another tool sends that selected content under the other provider's terms.
- No live web browsing, verified national world-data catalog, automatic affordability comparison, OAuth subscription connector, mobile app, or cross-device sync is implemented. Research/comparison work remains [planned](docs/NEXT_PHASE.md).
- Model quality varies. User review is essential for both personal insights and transcripts. This is not professional career, financial, or legal advice.

Read [SECURITY.md](SECURITY.md) before handling sensitive data or reporting a vulnerability.

## Project background and development

Pip grew out of a personal exploration of how work and life fit together. It is a **human-directed, AI-assisted prototype**: product direction, priorities, and hands-on evaluation come from its creator; AI coding tools have assisted implementation, documentation, and tests. It uses existing models rather than training a foundation model. Initial résumé/dictation trials are usability feedback, not a general accuracy benchmark.

Small, focused contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and proportional testing. The [CI workflow](.github/workflows/ci.yml) is configured for deterministic Windows/Python and mocked JavaScript checks; it does not run a model, browser, or real microphone.

| Read more | Purpose |
|---|---|
| [Prototype guide](docs/PROTOTYPE.md) | Running the current bench, privacy, limits, and module map |
| [Product vision](docs/VISION.md) | The longer-term mission, Person Map, and reasoning model |
| [Next-phase plan](docs/NEXT_PHASE.md) | Implemented milestones and planned research/comparison work |
| [Design brief](Design.md) | User-authored product character and visual direction |
| [Original concept](JOBWOLF_CONCEPT.md) | Founding concept under the JobWolf name |
| [Changelog](CHANGELOG.md) | Milestones and unreleased changes |
| [Dependency inventory](docs/DEPENDENCIES.md) | Runtime, model, and redistribution terms |
| [Portfolio demo](docs/PORTFOLIO.md) / [publishing checklist](docs/PUBLISHING.md) | Safe demonstration and repository preparation |

## License

Project-authored code and documentation are licensed under the **[Apache License 2.0](LICENSE)**. Third-party components, model weights, datasets, and external services retain their own licenses and terms; this repository does not relicense them or grant access to a paid subscription/API. See [the dependency inventory](docs/DEPENDENCIES.md) before redistribution.
