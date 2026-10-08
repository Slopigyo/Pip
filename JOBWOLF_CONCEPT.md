# JobWolf
## A private map toward work, place, and a life that fits

Most job-search products start by asking what job title you want and where you want to search.

That is often the wrong starting point.

A person may know they are unhappy without knowing what occupation would suit them. They may have useful skills that do not fit neatly into a résumé. They may be willing to relocate but have no idea where their income would go furthest. They may care more about autonomy, a short commute, a workshop, time outdoors, or the ability to eventually own a home than they care about maximizing salary.

JobWolf starts with the person instead.

Its purpose is to learn enough about someone's abilities, preferences, constraints, ambitions, surroundings, finances, and desired way of life to help answer a larger question:

> **Where might this person actually belong?**

A job may be part of the answer. It is not necessarily the whole answer.

---

# The central idea

JobWolf is a private personal decision system for exploring possibilities.

Through an adaptive conversation, it gradually builds a structured **Person Map**: an editable model of what the person can do, how they like to work, what kind of life they want, what matters financially, what places suit them, and where they may want to go next.

The conversation should feel less like filling out a questionnaire and more like talking with a thoughtful friend who has known you for years.

The user can ramble.

JobWolf listens for useful threads.

It may notice that someone repeatedly enjoys fixing unfamiliar problems but gets bored once a process becomes repetitive. It may recognize that a hobby project demonstrates real technical skill even though it never appeared on a résumé. It may realize that a person's salary goal is less important than their desire for a four-day week or a realistic path to homeownership.

And when JobWolf is unsure, it asks.

Not because "question 28" is next, but because the answer could materially change what it recommends.

---

# The seven-part Person Map

Everything JobWolf learns is organized beneath seven large, understandable ideas.

### What You Bring
Your experience, skills, practical abilities, knowledge, projects, credentials, strengths, and transferable capabilities.

### The Work
The kinds of activities, problems, subject matter, physical/mental balance, variety, ownership, creativity, and purpose that make work satisfying or miserable.

### How You Work
Autonomy, team size, management style, company culture, workplace setting, schedule, pace, travel, human interaction, and other conditions surrounding the job itself.

### Your Life
The time and energy work must leave for relationships, pets, hobbies, projects, recreation, community, travel, and ordinary life.

### Your Place
Climate, landscape, housing, commute, infrastructure, neighborhood or town character, outdoor access, and willingness to relocate.

### The Numbers
Minimum viable income, comfortable income, cost of living, housing economics, savings potential, benefits, stability, and long-term ownership or financial goals.

### Where You're Going
Immediate needs, long-term direction, learning, retraining, career experiments, entrepreneurship, meaning, advancement, and the kind of future the person wants to create.

JobWolf can drill down from these broad categories into fine detail, but the person should always be able to zoom back out and understand the whole picture.

---

# People are not checklists

JobWolf should not reduce a human being to a collection of disconnected preferences.

A preference can depend on another preference.

Someone may happily accept lower pay for a four-day week. They may tolerate a longer commute if it can be done by bicycle. They may accept colder winters if doing so makes land and housing dramatically more attainable. They may take an imperfect job because it creates an unusually good path toward the future they want.

JobWolf therefore treats tradeoffs, relationships, uncertainty, and even contradictions as useful information.

If someone says they want to work only 25 hours per week, buy a house soon, live in an expensive region, and do not care much about salary, JobWolf should not quietly average those preferences into nonsense.

It should say, in effect:

> These things may pull in different directions. Which one bends first if we cannot satisfy all of them?

That may be a more useful career question than asking for another job title.

---

# Discovery is the product

JobWolf is not meant to return hundreds of matching listings.

Its best moment is much smaller:

> **"I never would have thought to look there, but that actually makes sense for me."**

That possibility could be a particular job, but it could just as easily be:

- a new career family;
- an unexpected industry;
- a small employer the user had never heard of;
- a town or region where the economics and lifestyle suddenly work;
- a short certification that unlocks an entire path;
- a way to combine skills that previously seemed unrelated;
- a bridge job that creates a better future two years later;
- or a new way of framing what the user is actually seeking.

JobWolf's unit of discovery is therefore not the **job listing**. It is the **Opportunity**.

---

# The Search Brief

Once JobWolf understands enough, it creates an actionable Search Brief.

The brief can describe:

- what kinds of work seem promising;
- what work environments to seek or avoid;
- realistic economic boundaries;
- places worth investigating;
- career families and adjacent occupations;
- unusual employer types;
- useful search terms;
- promising education or certification paths;
- unanswered questions;
- and specific opportunities worth exploring.

The person does not have to stay inside JobWolf afterward.

They can carry the brief to Indeed, Google, LinkedIn, a local job board, another AI, a career counselor, or direct employer websites.

That is intentional.

JobWolf is most valuable upstream of the conventional job search: it helps answer **what should I even be looking for, and where?**

---

# Privacy is part of the product

For JobWolf to work well, a person may reveal more than they would ever put on a résumé.

They may discuss money, frustrations, dreams, unusual life circumstances, failures, insecurities, family obligations, health-related practical needs, or simply things they do not want stored in a company's cloud database.

The best privacy policy is therefore architectural.

> **The private JobWolf profile should live on the user's own machine.**

The user owns it.

JobWolf does not need a centralized copy.

A local AI model can process everything without the profile leaving the device. If the user prefers a commercial AI API, they can choose one and use it under that provider's terms.

JobWolf should clearly show which AI is active and send only the information necessary for a particular operation.

This design allows the project to say something stronger than "we promise to be responsible with your profile":

> **We do not need to possess your profile.**

Public information about the world can remain separate: places, occupations, industries, employers, training resources, housing costs, and similar data can be updated without uploading the person.

The local application combines private knowledge of the person with public knowledge of the world.

---

# One side only

JobWolf serves the person looking for a better path.

It should not become a tool for employers to score or filter people.

The same intimate Person Map that may help someone understand why a certain environment makes them miserable would be inappropriate as an employer-side personality score.

Trying to serve both sides would compromise the trust required for the product to work well.

JobWolf can research employers. Employers are not JobWolf's customer in the matching relationship.

---

# A deliberately modest MVP

The first version does not need to compete with Indeed.

It does not need a mobile app, recruiter dashboard, social network, giant job index, proprietary foundation model, or massive backend.

It needs to prove four things:

### 1. Understand
Can someone have a natural, rambling conversation with JobWolf and feel accurately understood?

### 2. Structure
Can JobWolf convert that conversation into an accurate and editable Person Map while preserving uncertainty, evidence, and nuance?

### 3. Discover
Can it produce career, place, lifestyle, training, or employer possibilities that are genuinely useful and non-obvious?

### 4. Plan
Can it turn those discoveries into a practical Search Brief that helps the person take the next step?

That is enough to test whether the central idea is real.

---

# What the software might look like

The first technical implementation can remain surprisingly small.

The user's private data can live in an encrypted local SQLite database. A Python application can manage the Person Map, conversation logic, opportunity reasoning, and report generation. AI can run through a replaceable provider layer so the user may choose a local model through Ollama, another OpenAI-compatible local server, or an external API.

The language model should not be allowed to arbitrarily rewrite the database. It proposes structured changes; deterministic JobWolf code validates those proposals and commits them.

That produces an important separation:

> **The AI interprets. JobWolf decides what becomes persistent truth.**

The foundation model is therefore replaceable plumbing rather than the product itself.

JobWolf's distinctive value lives in its Person Map, question-selection logic, tradeoff model, discovery methods, world knowledge, opportunity reasoning, and the quality of the resulting plan.

---

# A possible long-term ecosystem

The architecture leaves room for several compatible forms without violating the privacy philosophy.

**JobWolf Core** could be a local application, potentially open source, that owns the Person Map and reasoning workflow.

**Bring Your Own AI** could allow technical users to run entirely locally or provide their own API credentials.

**JobWolf Atlas** could someday provide an optional continually updated source of public information about places, occupations, employers, economics, training paths, and related opportunities without ever receiving the private Person Map.

**JobWolf Pro** could package convenience, richer research, better reports, automation, or professionally maintained public-data integrations.

A future specialized local model might improve JobWolf's reasoning, but training a proprietary foundation model is not necessary to prove or build the product.

The business can sell useful software and knowledge about the world without selling knowledge about its users.

---

# Why the idea feels different

The modern internet is extremely good at optimizing people for systems: optimize the résumé for the ATS, optimize the profile for the recruiter, optimize the commute around the employer, optimize the person for the market.

JobWolf starts from the opposite direction.

It asks what the technology can learn about the world in order to help the person find a better fit within it.

There is something a little solarpunk about that: local ownership, interoperable tools, individual agency, practical technology, public knowledge, and a focus on building a satisfying life rather than maximizing engagement with a platform.

The ambition is not simply:

> **Find a job.**

It is:

> **Find a part of the world where who you are, what you can contribute, how you want to live, and what is realistically attainable line up unusually well.**

If JobWolf can help even a small number of people do that, it is already more valuable than another search box full of listings.

---

# Elevator Pitches

## 10-second / anyone

**JobWolf is a private AI guide that gets to know you, then helps you discover kinds of work and places to live that actually fit the life you want—not just jobs matching a résumé.**

## 20-second / friend

**Most job sites make you decide what job title and city to search before they help you. JobWolf does the opposite. You talk to it about what you're good at, what you like and hate, how you want to live, money, location, all of it. Then it helps uncover careers, places, employers, and paths you probably wouldn't have thought to look for yourself.**

## 30-second / product

**JobWolf is a local-first personal opportunity engine. It builds a private map of your skills, preferences, constraints, finances, lifestyle, and goals, then compares that against possibilities in the real world. A result might be a job, but it could also be a career direction, a place to move, an employer to watch, or a short training path. The goal is not to show you more listings. It's to help you figure out what is actually worth searching for.**

## Privacy-focused

**JobWolf needs to know a lot about you to be useful, so the profile stays yours. It lives on your computer, not in an online database. You can run the AI locally or choose your own API provider. JobWolf can update its knowledge of the outside world without collecting a centralized dossier on the people using it.**

## Technical

**JobWolf is a local-first decision-support system built around a structured Person Map rather than an embedding-only profile. An LLM conducts adaptive conversation and proposes typed profile updates; deterministic application logic validates and persists them locally. The same ontology drives question selection, opportunity discovery, fit reasoning, and report generation, while model providers and public-data sources remain replaceable adapters.**

## Investor / business

**Job search is downstream of a larger unsolved problem: most people don't know which careers, employers, or places would actually produce a good life for them. JobWolf builds a private model of the individual and uses it to discover those possibilities. Instead of monetizing intimate user profiles or selling candidate intelligence to employers, the business can monetize software, premium research, and public world-data services while the sensitive profile remains local.**

## The ambitious version

**JobWolf is trying to answer a question bigger than "What job should I apply for?" It asks: "Where in the world could someone like me build a life?" Then it helps you work backward from that answer into careers, locations, employers, skills, and practical next steps.**
