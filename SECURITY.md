# Security and privacy

Pip is an experimental, single-user Windows-local application, not a hardened hosted service. No independent security audit or production support commitment is claimed. Security fixes target the current development version; older prototype releases do not have a separate maintenance policy or guaranteed response time.

## Report a vulnerability privately

Do not publish exploit details or personal data in an issue, pull request, discussion, or screenshot.

If this repository's GitHub **Security → Advisories → Report a vulnerability** option is available, use it for a private report. That option must be enabled by the repository owner; this file does not enable it or imply it is already enabled. If unavailable, ask the maintainer for a private reporting channel without posting the vulnerability details. No separate security email is configured in this repository yet.

Include the version/commit, relevant platform versions, impact, and a minimal **synthetic** reproduction. Do not include a real résumé, Person Map, database/export, audio recording, API key, or startup-token link. Test only your own isolated environment and avoid accessing other people's data.

## Intended boundary

- The launcher binds only to `127.0.0.1`. A per-run token authenticates API calls; host/origin checks and restrictive browser headers provide additional controls. Localhost alone is not treated as authentication.
- Keep the startup URL private, leave the server local, and stop it when finished. Do not put it behind a public tunnel, reverse proxy, or shared multi-user deployment.
- Private workspace payloads are encrypted with Windows user-scoped DPAPI before SQLite storage. Database headers, sizes, and revision metadata are not encrypted. There is no plaintext storage fallback.
- DPAPI does not protect a running app from malware or another process with your privileges. Browser/server/model memory, extensions, OS paging/crash dumps, and external backups are outside that protection.
- JSON and Markdown exports, clipboard contents, screenshots, and manually shared prompts can contain private information. Exports are deliberately plaintext for portability. Copying a DPAPI database to another machine is not a reliable recovery plan; see [backup and recovery](docs/PROTOTYPE.md#storage-backup-and-recovery).

## Documents, models, and speech

Résumé previews are local and bounded; the parser child process is **not an OS sandbox or a strict total-memory cap**. Original document binaries are not retained by the app. Review extracted text and remove unnecessary contact details before saving or selecting it for model use.

The app treats documents and model output as untrusted. Typed schemas, evidence validation, deterministic rules, and user acceptance constrain state changes, but do not prove factual accuracy or eliminate every prompt-injection risk. Do not interpret an accepted note as verification of a credential or a model-generated possibility as verified research.

The Ollama adapter contacts a configured loopback service. Ollama's own cloud/network behavior is outside this app's control: choose genuine local weights and review that service's configuration. Manual handoff sends nothing automatically; copying and sending it to another tool is an external disclosure under that tool's terms.

Optional speech setup downloads pinned assets from public hosts and verifies expected sizes/hashes. Dictation itself sends only bounded audio and a fixed instruction to the owned local runtime. The app does not save raw audio or silently send transcripts. Download integrity does not constitute a full supply-chain audit of upstream binaries or models.

Deletion/reset clears the relevant active application records, not prior exports, original source files, provider copies, backups, or guaranteed forensic traces. Review the exact deletion warning before confirming.

## Repository hygiene

Private archives, databases, environment files, and exports are ignored; ignored files can still be force-added, and `.gitignore` cannot erase history. Use synthetic fixtures and disposable workspaces for tests and demos. Review both the staged changes and reachable Git history before publishing. Check author/committer metadata and screenshots as well as file contents.

The CI workflow uses read-only repository permissions and does not require provider credentials, upload workspace artifacts, or run models. It is not a substitute for dependency/advisory review or a security audit. Review [dependency and model terms](docs/DEPENDENCIES.md) before redistribution, and [the publication checklist](docs/PUBLISHING.md) before the first public push.
