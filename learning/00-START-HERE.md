# START HERE — the ModelMux study guide

This folder teaches you **ModelMux**: what it is, how every piece works, every
number in it, and how to defend all of it in an interview.

## How this differs from `learnings/`

You have two folders and they do different jobs.

| Folder | Holds | Read it when |
|---|---|---|
| `learnings/` | **Reference, keyed by technology.** 19 files: Redis, FastAPI, SQLite, embeddings… Each is *what it is → how it works → how we used it → interview questions*. | You need depth on **one technology** |
| `learning/` ← you are here | **The study guide, keyed by how you'll be asked.** Project walkthrough, stages, constraints, decisions, war stories, Q&A, system design. | You are **preparing to be interviewed** |

`learnings/` is the encyclopaedia. `learning/` is the exam revision.

---

## Read in this order

| # | File | What it gives you | Time |
|---|---|---|---|
| **01** | `01-what-the-project-is.md` | The problem, the solution, one request end to end | 20 min |
| **02** | `02-build-stages.md` | All 6 stages, what each proved, what broke | 25 min |
| **03** | `03-architecture.md` | Every file's job, the data flow, the schema | 30 min |
| **04** | `04-tools-and-technologies.md` | Every tool: what, why, alternatives rejected | 45 min |
| **05** | `05-numbers-and-limits.md` | **Every constraint, limit, threshold and price** | 20 min |
| **06** | `06-decisions-defended.md` | The 35 decisions as talking points | 40 min |
| **07** | `07-war-stories.md` | Real bugs in STAR format — the highest-value file | 30 min |
| **08** | `08-interview-qa.md` | Question bank, easy → hard | 60 min |
| **09** | `09-system-design.md` | The whiteboard version, and scaling it | 30 min |
| **10** | `10-build-from-scratch.md` | Rebuild it manually, with the learning goal at each step | reference |
| **11** | `11-cheatsheet.md` | One page to re-read before you walk in | 5 min |

**If you have one hour:** 01, 05, 07, 11.
**If you have one evening:** all of them, skimming 04 and 10.

---

## The three pitches — memorise these

### 30 seconds

> ModelMux is a cost-aware LLM router. It sits between an application and
> several model providers, classifies every prompt by how hard it actually is,
> and sends it to the cheapest tier that can handle it. A semantic cache in
> front means repeated questions cost nothing at all. Every routing decision
> is logged with the signals behind it, so you can always explain why a request
> went where it did.

### 2 minutes

> The problem is that most applications send every request to their best model.
> "What's 15% of 240?" and "refactor this 400-line auth module" cost the same,
> and only one of them needs a frontier model.
>
> ModelMux classifies each prompt in **11-12 milliseconds** with no LLM call —
> that's the key constraint, because calling a model to decide which model to
> call defeats the purpose. It uses hand-built heuristics plus a local
> MiniLM embedding model comparing against labelled examples, and routes to
> small, mid or large.
>
> In front of that is a semantic cache on Redis: it embeds the prompt and
> serves a stored answer for anything similar enough, so rephrasings hit too,
> not just exact matches. Behind it are retry, within-tier fallback and a
> circuit breaker per provider.
>
> Measured on a 32-prompt held-out set: **2.5× faster at the median** and
> **88% classification accuracy** for the heuristic. Every request writes a
> database row including every failure path, which is where the cost and
> latency numbers come from.

### 10 minutes

Walk them through `01-what-the-project-is.md` — the request lifecycle, the
three classifier modes and why the less accurate one shipped, the cache
threshold problem, and one war story from `07`.

---

## The five things that make this project interesting

Lead with these. They are what separate it from a tutorial project.

1. **The shipped classifier is the *less accurate* one, deliberately.** Hybrid
   scores 72% against the heuristic's 88% — and ships, because it eliminates
   every "too cheap" misroute. A wrong cheap answer costs more in trust than a
   wrong expensive one costs in money. (`06`, D15)

2. **The evaluation harness refuses to produce numbers from mock data.** A
   plausible fake in a results table is worse than no number at all. (`06`, D23)

3. **The measured cost saving didn't reproduce, and that is written up as
   the finding.** 6.6% on one run, 1.7% on an identical re-run — the real
   routing effect is 3-4% and the noise is bigger than the signal. The
   *latency* result reproduced. (`06`, D36)

4. **A 401 that lasted weeks was an environment variable shadowing `.env`,**
   found by noticing the provider console said *0 API calls*. (`07`, D29)

5. **Every failure path writes a database row.** Nothing is silently dropped —
   including the case where a model returned HTTP 200 with an empty answer,
   which would otherwise have been cached as a cheap success. (`07`, D32)

---

## The honest limitations — say these before you are asked

Volunteering a limitation reads as confidence. Hiding one that gets found
reads as the opposite.

- **Answer quality has never been graded.** The blind spot-check harness
  exists and nobody has filled one in. Cost savings mean nothing if the cheap
  answers are worse, and that column is empty.
- **Only one provider key ever worked.** The large tier is served by a Groq
  fallback, so the 37.1% figure is a projection, not a measurement.
- **The A/B cost saving is noise-dominated and does not reproduce** — 6.6%
  then 1.7% on identical prompts. Quote the same-token routing effect (3-4%).
  The latency result *does* reproduce.
- **Every accuracy number is scored against hand-labels written by one person**
  — the same person who built the classifier. Treat it as an upper bound.
- **n=32 on the held-out set.** One prompt is three percentage points.
- **The 11-12ms classification figure was measured on an idle process**, and
  the test guarding it is intermittently flaky in the full suite for reasons
  still unexplained.

---

## The one-line version of the whole project's philosophy

> **A number you cannot defend is worse than no number.**

Nearly every decision in `06-decisions-defended.md` is an instance of it.
