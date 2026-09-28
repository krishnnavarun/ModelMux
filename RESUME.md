# RESUME — you are here

**A fresh Claude Code session should read this file first.** It is the
"where we left off" pointer, not documentation. For the project itself read
`CLAUDE.md`; to run it read `SETUP.md`; for reasoning read `DECISIONS.md`.

**Last updated: 2026-09-28** · last commit `32c022f`

---

## Start the session with

> Read RESUME.md and continue.

---

## ⛔ The one thing blocking everything

**`GROQ_API_KEY` is missing from `.env`.** The file was overwritten with only
`GOOGLE_API_KEY` rather than appended to.

That kills the **small tier, the mid tier, and the large tier's fallback** —
all three are Groq. No live evaluation can run until it is back.

It cannot be recovered: `.env` is gitignored, and the only copy in this
machine's environment is the *dead* key from D29. **Create a new one at
https://console.groq.com/keys**, so `.env` reads:

```
GROQ_API_KEY=gsk_...
GOOGLE_API_KEY=AQ.A...
```

Two lines, not one. Then **open a new terminal** (D29: a live process keeps
the environment it was spawned with).

---

## ✅ What works right now

| | |
|---|---|
| `GOOGLE_API_KEY` | **live and verified.** First real Google calls in the project's history. |
| Large tier | `gemini-3.5-flash`, 0.0015 / 0.009 per 1K, **verified 2026-09-27** |
| Ladder | **30× output** (was 1× when large fell back to the mid tier's model) |
| Tests | **131 passing** with Redis up; 123 + 8 skipped without it |
| Decisions | **38** |

---

## 🔭 The single highest-value next action

**Restore the Groq key, then run the full evaluation.** This has never been
done with a real tier ladder.

```powershell
.\run.ps1 -Eval        # flushes the cache, runs the held-out set
```

Why it matters: every weak number in this project traces to one missing
provider.

- **D36** — the cost A/B was noise-dominated (6.6% then 1.7% on identical
  prompts) because large *was* mid. With a 30× ladder the signal should
  finally clear the noise.
- **D37** — quality graded on n=8, because 22 of 30 pairs compared a model
  with itself. With a real large tier every pair becomes a genuine comparison
  and n goes to 30.

**Run it 3–5 times, not once.** D36 exists precisely because a single run was
published and did not reproduce.

Expect it to be slow: Gemini's free tier measured **19–32s on hard prompts**
and returns intermittent 503s (retry absorbs them).

---

## 🆕 Built in the last session, NOT yet verified by eye

**`dashboard/index.html` was rebuilt** — new colours, and a prompt console
that routes a prompt from the page and replays the seven pipeline stages with
the timings the API actually reported.

**`run.ps1` was added** — one-command launcher.

> ⚠️ **Neither has been looked at in a browser.** The Claude-in-Chrome
> extension was not connected, so layout, spacing and whether the motion
> feels right are all unverified. The JS and PowerShell parse; that is all
> that was checked. **Ask the user what is wrong with it before assuming it
> is fine.**

Best demo path while Groq is missing:

```powershell
.\run.ps1 -Mock      # fake providers, all tiers respond, nothing 401s
```

…then `wsl redis-cli FLUSHDB` afterwards — mock runs poison the real cache,
which contaminated a measurement once already.

---

## 📋 Still open

| Item | Needs | Notes |
|---|---|---|
| Restore `GROQ_API_KEY` | the user | blocks everything live |
| Look at the new UI | the user | never eyeballed |
| Run the eval on the real ladder | the key | fixes D36 **and** D37 |
| **D35** | investigation | classification test flakes in-suite (~85ms vs a 20ms budget). CPU contention **refuted** by experiment. Not yet examined: GC/memory pressure after a full suite, `embedding.init()` re-entry per TestClient, module-level state (the D22 family). |
| Human quality grading | ~40 min, no key | D23 wanted human **and** LLM-as-judge as separate columns; only the second is filled, and the grader was the same agent that built the router |
| A found labelling bug | a decision | The user noticed `"Prove that there are infinitely many prime numbers."` is labelled `large` in `eval/labelled.json`, and argued it is a famous 3-line proof a small model has memorised. Probably right — a "too expensive" misroute. **Test before relabelling**: `labelled.json` is the embedding classifier's training data, so changing it shifts every accuracy figure. |

---

## 🧭 Conventions that are easy to break

- **Never print a secret.** Length and last four characters only. This
  project leaked one once; that is why the rule exists.
- **One static file for the dashboard** — no React, no build step, no
  external references (D24).
- **No new dependencies without asking.** The approved list is `specs.md` §3.
- **Every decision gets recorded in `DECISIONS.md`** with what it cost, and
  every mistake gets logged in `learnings/01-project-timeline.md`. The
  mistakes are the point — do not quietly fix and move on.
- **Do not quote an A/B cost saving from the current configuration** (D36).
  Quote the same-token routing effect, and say which ladder.
- **Do not quote 11–12 ms classification** without saying it was an idle
  process (D35).
