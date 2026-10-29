# Pip — Personal Insight Pathfinder

> **Find Yourself.**

Pip (formerly **JobWolf**) is a **private, local-first guide** mapping **Places, Interests & Possibilities** to help a person find work, places, and life paths that actually fit.

Pip is not primarily a job board, résumé optimizer, recruiter tool, or application bot.

Its purpose is more fundamental:

> **Help a person understand where they may belong, discover possibilities they would not have thought to search for, and turn that understanding into a practical plan.**

A conventional job site begins with a title, keyword, and location. Pip begins with the person.

Historical **JobWolf** references below describe the same project. The `jobwolf` package, launcher, storage, and wire-format names remain unchanged.

## Run the Windows prototype

The chat-first Windows prototype supports streaming Ollama replies (initial preference: `Lyra:latest`). Deliberate structured extraction, a reviewed/editable Person Map, exploratory possibilities, portable insight/Search Brief exports, and manual handoff to an existing chat tool live in the collapsed **Notebook & bench tools** (`bench-tools`). Optional résumé intake supports locally parsed PDF/DOCX/TXT/Markdown or pasted text, editable previews, explicit excerpt selection, and source-cited notes. Scope: the contiguous 48 United States plus Washington, DC.

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -c constraints.txt -e ".[test]"
.venv\Scripts\python.exe -m jobwolf
```

After installation, `start-bench.cmd` launches it too. Local calls request a **128,000-token context window by default**; lower it under **Connection & privacy** if memory use or latency is excessive. Input preflight still uses a separately labeled conservative byte guard, not an exact token counter. Read [the prototype guide](docs/PROTOTYPE.md) for the workflow, privacy/recovery notes, and current limits; see [dependency and licensing choices](docs/DEPENDENCIES.md) before packaging or choosing a project license.

**Optional local dictation:** run `setup-speech.cmd` once to download/verify about **3.2 GB** of Granite Speech 5.0 2B Q8 and the Windows Vulkan runtime. Then use **Dictate → Stop & transcribe → review/edit → Send**. Audio is processed locally, up to two minutes at a time, without supplying your profile or saving raw audio. The speech model unloads after each transcription and is separate from Ollama chat. See [setup, privacy, and latency details](docs/PROTOTYPE.md#optional-local-dictation-granite).

Private payloads are encrypted with Windows DPAPI before SQLite persistence—not whole-file SQLite encryption. JSON and Markdown exports are deliberately plaintext. The app has no live web browsing, verified world-data catalog, telemetry, or automatic cloud fallback; generated possibilities are hypotheses for exploration.

---

## Why JobWolf Exists

People rarely make career decisions in isolation from the rest of life.

A job that pays more may still be worse if it requires an exhausting commute, lives in a place the person dislikes, leaves no energy for life outside work, or makes homeownership impossible. Conversely, a lower-paying opportunity in the right place, environment, and career path may produce a dramatically better life.

Existing job tools are good at answering:

> "Which posted jobs contain these keywords?"

JobWolf is designed to answer:

> **"Given who I am, what I can do, what matters to me, and the life I want, what possibilities should I investigate?"**

The ideal JobWolf result is not necessarily a job listing. It may be:

- a career family the user never considered;
- an overlooked skill that transfers into another field;
- an unusual employer worth watching;
- a city, town, or region where work and life fit unusually well;
- a short training path that unlocks better options;
- a lifestyle or economic tradeoff worth reconsidering;
- a useful search strategy to carry into traditional job sites;
- or a specific current job worth pursuing.

---

## Core Product Principle

### JobWolf works for one side only: the individual.

JobWolf is not an HR product and is not intended to rank, filter, score, or reject candidates for employers.

The private Person Map exists solely to help its owner make better decisions. It must never quietly become an employer-facing candidate profile.

---

## Privacy Model

JobWolf is **local-first by design**.

The user's personal profile should live on the user's own machine by default.

```text
USER'S DEVICE
│
├── Person Map
├── Conversation history
├── Preferences and anecdotes
├── Résumés and personal files
├── Search briefs
├── Opportunity evaluations
├── Feedback
└── Reports and exports
        │
        │ only when needed and explicitly configured
        ▼
   AI provider chosen by the user
   ├── local Ollama
   ├── local OpenAI-compatible server
   ├── user's chosen API provider
   └── future compatible providers
```

JobWolf should not require a centralized personal-profile database.

The preferred privacy guarantee is stronger than "trust us with your data":

> **JobWolf should not possess the user's private life profile in the first place.**

External AI is optional. If the user selects an API provider, JobWolf sends only what is necessary for the requested operation, under the terms of that provider.

Public/world data may be hosted separately because it describes places, occupations, industries, employers, costs, training paths, and other non-private subjects rather than the user.

```text
PRIVATE PERSON MAP
        +
PUBLIC WORLD MAP
        =
PERSONAL OPPORTUNITY MAP
```

---

## The Person Map

JobWolf organizes what it learns into seven broad domains.

| Domain | User-facing idea | Core question |
|---|---|---|
| Background & Capabilities | **What You Bring** | What can this person actually do? |
| Work Itself | **The Work** | What do they want to spend working time doing? |
| Work Environment | **How You Work** | Under what conditions do they work best? |
| Life & Lifestyle | **Your Life** | What does work need to coexist with? |
| Place & Home | **Your Place** | What kind of physical life and location fit? |
| Money & Security | **The Numbers** | What must work economically? |
| Direction & Growth | **Where You're Going** | What future are we trying to move toward? |

The human-facing structure looks like a tree:

```text
Person
└── Domain
    └── Dimension
        └── Facet
            └── Statement
```

Underneath, it behaves more like a graph because preferences interact across domains.

Examples:

- accept lower pay for a four-day workweek;
- tolerate a longer commute if it can be biked;
- accept colder winters for dramatically cheaper housing;
- take a temporary role because it creates a valuable future path.

These relationships and tradeoffs are first-class information.

---

## Statements, Not Just Fields

The smallest meaningful unit is a human-readable statement with structured metadata.

Example:

```yaml
statement: "Prefers solving unfamiliar practical problems."
domain: Work Itself
dimension: Novelty & Variety
facet: Novel Problems
importance: high
flexibility: preference
confidence: confirmed
source: user conversation
time_horizon: current_and_long_term
```

Important metadata includes:

- **importance** — how much this matters;
- **flexibility** — hard requirement, strong preference, preference, nice-to-have, neutral;
- **confidence** — confirmed, strongly inferred, tentative, unknown, conflicting;
- **source** — explicit statement, inference, résumé, feedback, calculation, etc.;
- **time horizon** — now, near-term, long-term, aspiration;
- **evidence** — why JobWolf believes the statement;
- **relationships** — tradeoffs, dependencies, conflicts, and supporting facts.

Where useful, a facet may distinguish:

- current state;
- ideal state;
- acceptable range;
- hard limit.

The structured map is the source of truth. Embeddings may help retrieval, but embeddings are not the profile.

---

## Conversational Onboarding

Onboarding should feel less like completing a career questionnaire and more like talking with a thoughtful friend who is genuinely trying to understand the person.

The conversation should:

1. invite the user to ramble rather than forcing predefined job titles;
2. extract useful structured information from natural language;
3. notice important anecdotes, informal experience, and nontraditional skills;
4. periodically reflect its understanding back to the user;
5. distinguish what the user explicitly said from what JobWolf inferred;
6. identify contradictions and unresolved tradeoffs;
7. ask the next question based on how much the answer could improve the search;
8. stop asking once enough is known to begin producing value.

A useful conceptual question-priority function is:

```text
Question Value
≈ Uncertainty × Importance × Decision Leverage × Current Relevance
```

Conversation naturalness should influence *when* a valuable question is asked.

There is no need for a fake "profile 63% complete" meter. Human beings are not forms to finish.

---

## Opportunity, Not Job Listing

The atomic unit of JobWolf is an **Opportunity**.

An opportunity may be:

- a specific job;
- an occupation;
- a career family;
- an industry;
- an employer;
- a location;
- a training or credential path;
- a career transition;
- an economic/lifestyle strategy;
- or another actionable possibility.

Every opportunity is evaluated against the same seven-domain structure used by the Person Map.

The result should explain:

- where it aligns;
- where it conflicts;
- what is unknown;
- how confident JobWolf is;
- why JobWolf surfaced it;
- and what questions or next actions would reduce uncertainty.

Avoid false precision such as an unexplained "91% match."

---

## The Search Brief

Once JobWolf understands enough, it compiles the Person Map into an actionable Search Brief.

A Search Brief may contain:

- hard gates;
- strong positive signals;
- negative signals;
- economic requirements;
- place requirements;
- exploratory career directions;
- useful search vocabulary;
- industries and employer types to investigate;
- locations worth exploring;
- unresolved questions;
- promising training paths;
- conventional job-search queries the user can take elsewhere.

The Search Brief is intentionally portable. JobWolf should make other tools more useful rather than trap users inside its ecosystem.

---

## MVP (Minimum Viable Product)

The MVP should prove four things:

1. **Understand** — Can JobWolf understand someone unusually well through conversation?
2. **Structure** — Can it create an accurate, editable Person Map with evidence, confidence, tradeoffs, and unknowns?
3. **Discover** — Can it derive genuinely non-obvious career, place, lifestyle, and transition possibilities from that map?
4. **Plan** — Can it turn those discoveries into a useful Search Brief and human-readable report?

### MVP success metric

The most important early metric is not the number of jobs indexed or applications submitted.

It is **Discovery Rate**:

> The percentage of recommendations that users find genuinely worth investigating and probably would not have discovered themselves.

The desired user reaction is:

> **"I never would have thought to look there, but that actually makes sense for me."**

If a user moves to a small town a starts their own business instead of getting the same job they had but at a different corporation, that should also be considered a success.

---

## MVP Technical Direction

Keep the first implementation intentionally basic.

```text
Frontend / local app
        │
        ▼
JobWolf application layer
        │
        ├── Conversation engine
        ├── Person Map engine
        ├── Question selector
        ├── Opportunity reasoning
        ├── Search Brief compiler
        ├── Report generator
        └── AI provider adapter
        │
        ▼
Encrypted local SQLite database
        │
        ├── optional local semantic index
        ├── user files
        └── portable exports
```

Recommended early technologies:

- Python for core logic;
- FastAPI if a local HTTP API is useful;
- SQLite as the canonical private store;
- Pydantic or equivalent for schema validation;
- a model-provider abstraction supporting local and BYO AI;
- Ollama/OpenAI-compatible local inference first;
- a simple web UI or desktop shell;
- OS keychain/credential storage for API secrets;
- optional public-data adapters added incrementally.

### Critical AI design rule

The LLM should not directly own or mutate the database.

```text
User conversation
      ↓
LLM proposes structured changes
      ↓
Schema validation
      ↓
Deterministic JobWolf rules
      ↓
Database transaction
```

The probabilistic model proposes. The application validates and decides.

---

## Explicit Non-Goals for the MVP

Do not build these merely because they are adjacent to employment software:

- employer or recruiter accounts;
- candidate ranking for employers;
- applicant tracking systems;
- automated candidate rejection;
- résumé spam / mass application tooling;
- a LinkedIn-style social network;
- a giant centralized user-profile database;
- proprietary dependency on one AI provider;
- a custom foundation model;
- comprehensive scraping of every job board;
- unnecessary microservices, Kubernetes, or distributed infrastructure.

A résumé builder, cover-letter tool, application tracker, browser extension, native mobile app, and other conveniences may become useful later, but none are core to proving JobWolf's value.

---

## Data Ownership and Portability

The user should be able to inspect, edit, export, back up, and delete their own JobWolf data.

Useful exports may include:

```text
jobwolf-profile.json
jobwolf-profile.md
search-brief.md
career-map.md
location-shortlist.csv
```

A user's JobWolf should be movable between machines and usable with other AI systems or search services.

Lock-in is not the product.

Understanding is the product.

---

## Product Character

JobWolf should feel curious, practical, nonjudgmental, exploratory, and a little adventurous.

It should be comfortable saying:

- "I don't know yet."
- "That seems important; tell me more."
- "These two things you want may conflict. Which one bends first?"
- "This is unusual, but I think it's worth investigating."
- "I found a place/path/employer you probably would not have searched for yourself."

The long-term ambition is larger than job hunting:

> **Help people find places in the world where their abilities, work, finances, surroundings, and desired life fit together for the benefit of all.**

There is something intentionally hopeful in that idea. Technology should help people navigate the world with more agency, not turn them into better-optimized rows in somebody else's hiring database.

---

## Status

**v0.2.0 — Reviewed résumé intake and local speech-to-text milestones.** See [the changelog](CHANGELOG.md) for this checkpoint. This remains a Windows-local chat-first prototype with open-bench tools, not a packaged public product.

The first implementation covers conversational experiments, an editable persistent Person Map, explicit review of model proposals, exploratory possibilities, feedback, and a portable Search Brief. Its small local architecture is intended to test self-insight before expanding research integrations or public-product packaging.

The [next-phase plan](docs/NEXT_PHASE.md) now includes connected-path discovery and optional reviewed résumé intake: local previews, saved text snapshots, selected excerpts for chat or Person Map proposals, citations, and deletion. Workspace JSON v1 upgrades to v2 on the next save without a reset; the database path is unchanged. The initial résumé-to-note workflow is confirmed working, and the user reports that Granite dictation is fast and accurate in initial hands-on use. Research/comparison tools remain planned. This milestone is not a general accuracy benchmark; model insight and transcripts still require user review.

Licensing and long-term commercial packaging remain open design decisions.
