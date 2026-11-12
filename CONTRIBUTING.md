# Contributing to Pip

Pip is a personal, early-stage prototype. Small fixes, clearer documentation, synthetic regression cases, and focused usability improvements are welcome; there is no promise of a release schedule or support response time. Discuss large changes before investing in them.

Read [README.md](README.md), [AGENTS.md](AGENTS.md), and the user-authored [Design.md](Design.md). The longer-term [vision](docs/VISION.md) and [next-phase plan](docs/NEXT_PHASE.md) distinguish product goals from shipped behavior.

## Boundaries worth preserving

- Work for the individual, not employer-side candidate scoring.
- Keep canonical private state local. Chat, document intake, and dictation must not silently mutate the Person Map.
- Treat documents and model output as untrusted data, not instructions. Preserve provenance, uncertainty, explicit context selection, and review.
- Keep a small modular application; prefer existing dependencies and permissive licenses over new infrastructure.
- Do not rename `jobwolf`, wire/storage names, or the existing workspace path without an explicit migration.

## Local setup

Use Windows and Python 3.12+; Python 3.14 is the reference environment. From PowerShell at the repository root:

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -c constraints.txt -e ".[test]"
```

Node 24 is the CI reference for JavaScript checks. No npm install is required. Deterministic tests need neither Ollama nor speech assets; do not download models just to make a contribution.

For manual development, isolate synthetic data from your usual workspace:

```powershell
.venv\Scripts\python.exe -m jobwolf --data-dir private\development --port 8766
```

Do not point tests at a real workspace, browser profile, résumé archive, or microphone recording.

## Test in proportion to the change

Start with the affected module or behavior, not a full browser/model sweep. For example:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p test_documents.py -v
node --check src/jobwolf/web/app.js
```

For speech UI changes use `node --test tests/test_speech_ui.mjs`; for speech setup behavior use the `test_speech_setup.py` module. A prose-only change normally needs link/content review, not inference tests. See [the prototype guide](docs/PROTOTYPE.md#development-and-tests) for other focused checks and opt-in browser/model tools.

When a shared interface changes, or before a release, the broader deterministic checks are:

```powershell
.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py" -v
node --check src/jobwolf/web/app.js
node --check src/jobwolf/web/speech-recorder.js
node --test tests/test_speech_ui.mjs
```

[CI](.github/workflows/ci.yml) runs these on Windows with synthetic fixtures and temporary stores. It has no model downloads, live inference, real microphone, or browser sweep. See the [current validation status](docs/PUBLISHING.md#validation-status); report actual results rather than hiding failures or claiming the whole suite passes.

## Issues and pull requests

Explain the problem, the smallest useful change, and how you checked it. For behavior changes, include an appropriate synthetic regression test; for storage/privacy changes, describe compatibility and data-flow implications. Do not fix unrelated issues in the same patch.

Never attach real conversations, Person Maps, résumé files, workspace exports/databases, audio, provider keys, or startup-token URLs. Reproduce problems with invented content and redact screenshots and diagnostics. Ignoring a file does not remove it from existing commits. Security-sensitive reports belong in the private process described in [SECURITY.md](SECURITY.md), not a public issue.

AI-assisted contributions are welcome. Contributors remain responsible for understanding the change, checking its behavior and licensing, and keeping private data out of prompts and patches. Describe material AI assistance when it helps reviewers understand how the work was checked.

By intentionally submitting a contribution for inclusion, you agree to license that contribution under this project's [Apache-2.0 license](LICENSE), as described in its contribution terms. Only submit material you have the right to contribute; identify third-party sources and retain their required notices. No separate CLA is currently required.
