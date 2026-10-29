# Pip — Next-phase plan

**Status — v0.2.0 milestone checkpoint:** The initial connected-path prompt/report/card slice, reviewed résumé intake, and Granite-first local dictation are implemented. The user has confirmed the initial résumé-to-note workflow and satisfactory dictation speed/accuracy in hands-on use. Research/comparison tools remain planned; broader model-quality assessment continues incrementally. See [CHANGELOG.md](../CHANGELOG.md).

## 1. Outcome

Move from a functioning conversational bench to a useful personal exploration session:

> Start with a conversation or an existing résumé, develop a reviewed understanding of the person, and produce a few connected paths through work, place, and everyday life—with practical ways to investigate them.

A résumé is an optional starting record, not a definition of the person or the boundary of their possibilities. Typing must not be the only way to participate: optional local speech input now provides an editable draft, with explicit sending and keyboard input preserved.

The first usable milestone combines **richer discoveries and briefs** with **reviewed résumé intake and a source-aware interview**. It does not require a national database, live browsing, a map, a financial engine, or speech recognition to be finished first.

Follow [README.md](../README.md), [Design.md](../Design.md), and [AGENTS.md](../AGENTS.md). Keep the calm field-companion character and the scope of the contiguous 48 states plus Washington, DC.

## 2. Starting point and boundaries

The current prototype already has streaming Ollama chat, explicit Person Map extraction/review, possibility proposals, feedback, a portable Search Brief, encrypted local persistence, and manual external-model handoff. See [PROTOTYPE.md](PROTOTYPE.md) for actual behavior and limits.

It now has optional reviewed document intake and local speech recognition. It does **not** yet have sourced public research records, live browsing, or deterministic affordability comparisons. Its existing JSON workspace import **replaces** a workspace; that is not a résumé importer and must not be repurposed as one.

Preserve these boundaries throughout:

- Chat remains smooth and primary. Advanced extraction, research, and comparison stay in **Notebook & bench tools**; convenient entry points must not make the main screen a control panel.
- Ordinary chat, attaching a document, and dictation do not silently mutate the Person Map. Typed proposals still require validation and deliberate review.
- Keep Python/FastAPI/Pydantic, SQLite, and the simple browser UI. No framework rewrite or new hosted backend is justified.
- Keep the `jobwolf` package, wire identifiers, `start-bench.cmd`, and existing workspace location. Consequential schema changes require explicit migrations, not a reset.
- Keep local inference and manual handoff usable. Hosted integration is independent work, requires an officially supported route, and must visibly disclose selected context leaving the device. No silent cloud fallback.
- Prefer established, permissively licensed dependencies. Check exact package, transitive-dependency, model-weight, and data terms before adopting or distributing them; see [DEPENDENCIES.md](DEPENDENCIES.md).

## 3. What to carry forward from the original exploration

The private original archive is reference material, not an application dependency, public dataset, or test fixture. Its ideas should become general behavior, not personal defaults.

| Lesson | Application in Pip |
|---|---|
| Work, place, community, and daily life interact | Develop connected paths, not only unrelated job titles and towns |
| Preferences have reasons and can be conditional | Preserve supporting stories, conditions, tensions, and corrections behind concise notes |
| Ability is broader than job history | Distinguish paid work, volunteering, completed projects, interests, and aspirations |
| A good headline can hide a bad ordinary week | Discuss time, energy, responsibilities, commuting, and financial breathing room together |
| Discovery should lead to learning | Give each path a low-commitment next investigation and explain what its answer would change |
| Evidence depth can bias comparisons | Keep unexplored alternatives visible; more research about one place does not automatically make it better |

Retain the archive rather than merging the old application. Selected calculation code and evidence-view patterns may be adapted later. Its 25-region research sample is not complete national coverage; incomplete financial scenarios are not established affordability results. Review provenance, licensing, and residual personal material before promoting anything into distributable code or data.

## 4. Milestone A — Richer discoveries and a portable exploration brief

**First slice delivered:** task-specific connected-path instructions, clearer discovery sections, individually presented brief paths, an ordered investigation plan, and matching Markdown export. Existing fields, explicit context/review, and storage format are unchanged; shared prompt instructions were shortened to preserve context headroom. Earlier results are not rewritten. Live-model quality still needs the user's assessment. See [PROTOTYPE.md](PROTOTYPE.md) for the current workflow.

**Strengthens:** possibility discovery and Search Brief/report generation.

**Why more than the current lists:** a career suggestion and a place suggestion do not explain how they could fit together, what daily life might involve, or how to investigate the combination.

### Deliver

Use the existing selected-context discovery and brief actions to develop a small number of distinct paths. A path can connect work, a place or type of place, community participation, training, or a transition strategy. It need not include every category, require moving, or culminate in a job application.

Each path should explain:

1. **The possibility:** what could be worth exploring.
2. **Why it may fit:** relevant reviewed Person Map notes and the reasoning connecting them.
3. **The ordinary week:** likely implications for schedule, energy, household needs, community, and money, with assumptions clearly marked.
4. **Tradeoffs and unknowns:** what may not fit and what we do not know.
5. **A way in:** a practical, low-commitment investigation—not an invented vacancy, welcome, or invitation.
6. **The next discriminating question:** what answer would materially change whether to pursue it.

Evolve the existing Search Brief rather than creating a competing report system. Include a concise understanding of the person, the paths, their important differences, useful search vocabulary, and a short investigation plan. Detailed evidence remains inspectable without overwhelming the narrative.

LLM-generated insight is useful before public research is integrated. Continue labeling it as hypothesis: do not fabricate current employers, events, pay, housing, citations, or having searched the web. Missing evidence should not prohibit exploration, but it must limit factual claims.

### Smallest implementation

Start with improved task instructions and report/card presentation using existing fields where adequate. Add typed fields only where needed to preserve meaning across review, storage, and export. Do not introduce a graph database, ranking engine, or elaborate path editor.

Keep feedback deliberate: what appeals, what does not, and why. Feedback can inform a later reviewed map update; it must not become an automatic preference change.

### Done when

- A user can understand why two or three paths differ and choose a useful next investigation.
- Relevant conditions and uncertainties survive proposal review, saving, and Markdown export.
- No unsupported precision, invented research, or universal homeownership/relocation goal is introduced.
- Existing Person Map extraction remains separate and does not produce discovery output by accident.

## 5. Milestone B — Résumé intake and a source-aware interview

**Implemented in the bench:** optional local PDF/DOCX/TXT/Markdown/paste previews; editable approved text snapshots; explicit line-range selection for chat or source-only proposals; document citations preserved through review/edit/export; source deletion with dependency confirmation. JSON v1 migrates in memory to v2 and persists on the next ordinary save; no reset or SQLite-envelope migration. See [the résumé workflow and limits](PROTOTYPE.md#optional-résumé-upload). Interview quality still needs hands-on evaluation with the user's chosen model.

The implementation retains no original binary, uses pypdf (BSD-3-Clause) and standard-library DOCX/text parsing, and bounds file/text/structure sizes and parser time. It does not provide OCR, a strict process-memory cap, or an OS parsing sandbox. Deletion conservatively clears derived outputs rather than retaining potentially copied source claims as merely stale; conversation and uncited notes remain, as disclosed before confirmation.

**Strengthens:** conversational understanding and accurate Person Map creation.

**Why copy/paste alone is insufficient:** many people already have a résumé in a document format. Requiring them to retype their background adds friction, loses provenance, and risks treating document claims as direct conversational confirmation.

### User experience

1. Offer an optional **Add a résumé** entry point near the conversation, alongside starting without one. It is not an onboarding requirement.
2. Parse locally and show the extracted text before any model use. Let the user correct extraction mistakes and exclude irrelevant or sensitive passages such as contact details.
3. Let the user explicitly use selected material as the interview starting point. Show what will be used and which provider will receive it.
4. Reflect a short, tentative understanding and ask one useful question at a time: what the person enjoyed, what they want to leave behind, what is missing from the résumé, or which direction they want to explore.
5. Offer supported notes for the existing Person Map review flow. The user chooses which to retain or correct.

Do not equate job titles with identity, infer credentials from exposure, treat résumé omissions as lack of ability, or assume past work is desired future work. A tailored résumé can omit entire interests and capabilities. The conversation must be able to broaden beyond it.

### Initial formats and limits

- Ship support for **text-based PDF, DOCX, and plain text/Markdown**, with paste as a fallback.
- Implement the source/review flow with text first, then add PDF and DOCX adapters. The résumé milestone is not complete with paste alone.
- Defer OCR/scanned documents, legacy `.doc`, image résumés, and layout recreation. Explain unsupported, encrypted, or unreadable documents and offer a usable fallback; do not silently ingest empty or garbled text.
- Select maintained permissive parsers during implementation. No package or external document service is selected by this plan.
- Bound upload size, pages, decompressed content, extracted text, and processing time. Treat documents as untrusted: do not execute macros/scripts, follow embedded instructions or links, fetch remote resources, or use user filenames as filesystem paths.

### Source and review model

Introduce a minimal typed document-source record: internal ID, display filename, source kind, import time, document date if known, and a stable extracted-text version with passage locators. The same mechanism can later support user-selected notes or research documents, without implementing a general document-management system now.

The present evidence contract quotes user messages only. Extend it deliberately to distinguish message evidence from document evidence. Never disguise imported text as a user-authored chat message just to satisfy that contract.

Preserve separate concepts:

- **Document claim:** what a particular source/version says.
- **User correction or confirmation:** what the person subsequently states or reviews.
- **Pip inference:** an interpretation, not a quotation or credential.

Acceptance does not rewrite provenance. Correcting extracted text creates a new version rather than silently changing the passage behind an accepted note. Conflicts and duplicate claims should be surfaced for reconciliation; an old document does not override a newer explicit correction, nor does an import timestamp prove the document is current.

Long documents must use bounded, visible passage selection or explicit batches, not silent truncation or stuffing the whole archive into each prompt. Expose omissions and retain links back to source passages. Do not automatically raise model context limits or perform unbounded extraction calls.

### Privacy and lifecycle

- Résumé intake adds selected sources to the current notebook; it does not replace the workspace or automatically load the private archive.
- Retain approved extracted text and provenance in encrypted local storage. Do not retain the original binary by default; disclose retention and clean up parsing temporaries. The user's original file is never modified.
- Contact information is normally unnecessary for discovery and should be excluded from model context unless explicitly relevant. Do not promise automatic redaction is infallible.
- Source text is not automatically included in ordinary chat or every future task. Use selected passages and reviewed notes, with context visibility and limits.
- Include retained source text, versions, and evidence links in portable export/import. Preserve existing messages and IDs through a versioned migration and round-trip checks.
- Before shipping, implement deletion with a clear dependency preview: remove retained source text and copied evidence quotes, and resolve affected notes/proposals without dangling references or presenting missing support as intact. Mark affected derived outputs stale. Explain that external files and earlier exports are not erased.
- No raw document content in routine logs, telemetry, committed examples, or test fixtures.

### Done when

A user can import a supported résumé, inspect/correct the text, have a useful follow-up conversation, review supported notes, and generate the richer brief without manually re-entering their history. Source, uncertainty, corrections, export, and deletion remain understandable. Starting without a résumé remains equally valid.

## 6. Follow-on — Investigated paths and optional comparisons

**Strengthens:** opportunity reasoning and practical investigation after initial discovery.

Begin with explicitly attached research notes or source references, not a comprehensive search service. Keep public observations separate from personal interpretation. Record source, observation/check date, geography, and limitations; distinguish an employer lead from an advertised opening or an actual offer. A link alone is not verification.

Keep these additions demand-driven:

- Side-by-side comparison of a few saved paths in meaningful units and qualitative tradeoffs; no opaque match score.
- Inspectable place/climate views where they answer a user's question, labeled with sampling and date limitations.
- A selectively adapted deterministic budget/ownership calculator, only when the user wants that comparison and can supply the required inputs. Unknowns stay unknown; calculations do not establish job access, financing, or future outcomes.
- Later public-data/search adapters using minimal derived queries, not full private profiles. Review each source's license and refresh requirements before distribution.

Avoid double-counting income, low costs, and the affordability result they jointly determine. Keep financial viability, evidence quality, and personal preference distinct. Maps and calculators remain optional notebook tools, not prerequisites for useful discovery or speech input.

## 7. Speech input — Granite-first local dictation

**Strengthens:** conversational understanding by reducing typing burden and allowing a more natural account of someone's life.

**Current slice:** at the user's direction, start with **Granite Speech 5.0 2B Q8** rather than comparing several backends up front. Optional pinned Windows Vulkan assets, bounded local capture/transcription, cancellation, editable draft insertion, and no automatic submission or map mutation are implemented. The synthetic Windows/Vulkan check and focused Chrome/Firefox capture checks passed. The user's initial real-voice trial was fast and accurate enough to retain Granite as the first backend; assess longer recordings and varied speech through continued use rather than starting a model comparison without a demonstrated problem. See [the prototype speech guide](PROTOTYPE.md#optional-local-dictation-granite).

This is speech-to-text input, not a commitment to a full voice assistant. No text-to-speech or hands-free turn-taking is added. The speech adapter remains separate from conversation reasoning so the backend can change later if evidence warrants it.

### Intended first experience

A visible microphone action starts recording with permission; the user can stop or cancel. Transcribed text becomes an editable draft in the normal composer, and the user sends it deliberately. Recording or transcription alone does not save a chat turn or create Person Map notes. Keyboard input remains available.

Keep capture/transcription separate from conversation reasoning so a transcription backend can change without rewriting chat. No always-on listening, hidden browser speech service, or automatic cloud fallback. Do not retain raw audio by default; disclose any temporary handling and release recording resources on stop, cancel, or failure.

### Focused hands-on evaluation

Try Granite first with short, then longer local dictation. Compare another model only if speed, accuracy, or usability is inadequate. Use synthetic material or explicitly consented recordings for any shared tests, never committed personal audio. Assess:

- Rambling speech, pauses, corrections, names, dates, numbers, and technical vocabulary.
- Missing or invented words, including speech hallucinated during silence; ease of correction matters as much as aggregate accuracy.
- Latency, long-utterance handling, and recovery from microphone or transcription failures.
- Windows support and CPU/GPU memory use on the current AMD 24 GB machine, including contention with an Ollama model.
- Offline operation, any external transmission, runtime and model-weight licenses, installation burden, and redistribution terms.

Prefer a practical local option if quality and usability permit; a future hosted option requires explicit provider selection and consent. Schedule this evaluation after the first résumé-to-brief slice, or earlier if typing is blocking useful personal testing. It need not wait for maps, financial tools, or live search. Hands-free turn-taking, streaming partial transcripts, speaker identification, and spoken replies are separate later decisions.

## 8. Delivery order and proportional validation

| Step | Scope | Completion boundary |
|---|---|---|
| 0 | Ignore the private archive; publish this general plan | Housekeeping and documentation only |
| 1 | Richer paths and brief using existing selected context | Initial prompt/presentation slice implemented; assess usefulness with the chosen model |
| 2 | Typed document sources, reviewed text intake, and lifecycle/migration handling | Implemented; synthetic citation/review/migration/export/deletion checks |
| 3 | PDF/DOCX adapters and résumé-assisted interview | v0.2.0 milestone: initial résumé-to-note workflow confirmed; interview quality remains model-dependent |
| 4 | Granite-first local dictation | v0.2.0 milestone: initial hands-on dictation accepted for speed/accuracy; continue evaluating through use |
| 5 | Research records, comparisons, maps/calculators as needed | Each addition solves a demonstrated investigation problem |

Steps 1–3 form the next core phase. Later steps can change order based on actual use; they are not a request to implement everything at once. Provider/model experiments remain separate from feature work.

Use focused checks proportional to each change:

- Planning/ignore-file edits: verify Git exclusion and documentation links; no model or browser tests.
- Prompt/report changes: targeted task-contract and rendering checks; user judges conversational usefulness and discovery quality.
- Source/schema changes: focused evidence, migration, export/import, and deletion checks using synthetic material.
- Parser changes: a small set of synthetic normal, malformed, oversized, and unsupported documents; confirm failures do not overwrite state or make external requests.
- Speech work: focused recording/transcript lifecycle and installer checks, one explicit compatibility smoke when needed, then user assessment of Granite. No automatic multi-model comparison.

Do not run the entire suite, browser sweeps, or live-model calls after every edit. Broaden testing when an actual cross-cutting migration or security boundary warrants it, explain the reason, and never repurpose the private archive as an evaluation dataset without separate informed consent.

## 9. Not in this phase

No automatic applications or outreach, résumé optimization service, employer-facing scoring, mandatory numerical preference weights, centralized private-profile storage, exhaustive national scraping, automatic import of old personal research, broad UI redesign, or mascot artwork. No new provider login, parser dependency, speech model, public-data redistribution, or project license is approved merely by appearing in this plan.

The measure of progress is a user recognizing themselves in the understanding, finding a path worth investigating, and knowing what to do next—not the number of fields, documents, or tools accumulated.
