# Changelog

Pip — Personal Insight Pathfinder. Versions describe the local prototype, not a packaged public release. The Python package and storage identifiers retain the `jobwolf` name.

## Unreleased

### Public repository preparation

- Adopt Apache-2.0 for project-authored code and documentation, with package license metadata and separate third-party/model licensing guidance.
- Make the README a concise project overview; preserve the extended product vision and original concept/design documents.
- Add contribution and security guidance, a synthetic-data portfolio demo, and a publication checklist.
- Add a Windows GitHub Actions workflow for deterministic Python and mocked JavaScript checks, without model downloads or live inference.
- Make optional Chrome smoke checks use an explicitly configured existing executable instead of a developer-specific home path; retain disposable profiles and the no-Edge/no-download safeguards.

### Fixed

- Define the missing `Proposal.repaired_document_statement_indices` review metadata so structured proposals no longer fail strict-schema validation. Require distinct, in-range, nonnegative integer indices; keep the field application-owned and default it to an empty list for older workspaces. Evidence validation and explicit acceptance remain unchanged; no workspace reset or migration is required. Focused regression checks passed; see [validation status](docs/PUBLISHING.md#validation-status).

## 0.2.0 — 2026-10-29

### Prototype milestones

- **Reviewed résumé intake:** local PDF/DOCX/TXT/Markdown or pasted-text previews, editable source snapshots, explicit excerpt selection, source-cited Person Map proposals, and source deletion. The initial résumé-to-note workflow is confirmed working.
- **Local speech-to-text:** Granite Speech 5.0 2B Q8 dictation with recording controls, a two-minute limit, cancellation, and an editable composer draft. The user reports satisfactory speed and accuracy in initial hands-on use. This is an initial usability milestone, not a general speech-accuracy benchmark.

### Reliability improvements

- Number document lines explicitly in model context. Recover incorrect locators or whitespace-only quotation differences only when a quotation uniquely matches the selected source; visibly flag repaired citations for review.
- Keep résumé evidence distinct from user speech. Source-only proposals no longer require or invent user-message citations. Ambiguous, paraphrased, or fabricated evidence still fails validation.
- Default local chat/proposals to a requested **128,000-token** window, with smaller selectable windows and a separately labeled conservative byte guard. Align request schemas, preflight checks, context selection, and Ollama options.
- Capture microphone audio at the browser's native rate, then convert locally to Granite's 16 kHz WAV format. This fixes Firefox's immediate recording failure without requiring global blocker or tracking-protection changes.

### Privacy and compatibility

- Speech setup explicitly downloads approximately 3.2 GB of pinned, hash-verified public model/runtime assets. It adds no Python dependencies; licenses and notices are retained separately from private workspace data.
- Dictation sends only audio and a fixed instruction to a short-lived local runtime, never the résumé, conversation, or Person Map. Raw audio is not saved by the app; transcripts remain unsent drafts until the user chooses Send.
- Résumé intake and dictation do not automatically mutate the Person Map. Structured proposals still require deliberate review.
- Preserve the existing `start-bench.cmd`, `jobwolf` package, and `%LOCALAPPDATA%\JobWolfBench\workspace.sqlite3`. Workspace JSON remains v2; older v1 data upgrades on the next ordinary save without a reset.
- Keep the private original archive, local databases, model binaries, and personal documents/audio outside the commit. No project license is selected by this milestone.

See [the prototype guide](docs/PROTOTYPE.md) for setup and limits, [dependency terms](docs/DEPENDENCIES.md) for licensing considerations, and [the next-phase plan](docs/NEXT_PHASE.md) for remaining work.
