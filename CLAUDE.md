# ModelMux

A cost-aware LLM request router. Classifies each incoming prompt by difficulty
and dispatches it to the cheapest model tier that can handle it.

**`specs.md` is the authoritative technical spec.** Read it before writing code.
If something here conflicts with it, the spec wins. If the spec conflicts with a
conversation, the spec still wins unless I say otherwise.

---

## Working style

- I am learning this project, not just shipping it. **Explain what you write.**
- Build one piece at a time and confirm it runs before moving on.
- Prefer simple, explicit code over clever abstractions. If a junior reader
  would need to think twice, rewrite it.
- **Do not add a dependency without asking me first.** The approved list is
  `specs.md` section 3; anything outside it needs a conversation.
- No ORM. Use `sqlite3` from the standard library.
- No vector database. No framework beyond FastAPI.
- Keep routing logic in `router.py` readable and commented — I need to be able
  to defend every decision in it.
- Do not write the dashboard before Stage 6, however tempting.

## When you make a choice I did not specify

Say so explicitly, and record it in `DECISIONS.md` with:
what was decided, the alternatives, why this one, and what it costs.

That file is the point of the project as much as the code is.

## Non-negotiables (specs.md section 2)

- No LLM call in the classification path. Classification is heuristics plus a
  local CPU embedding model, nothing else.
- Classification under 20ms, cache lookup under 30ms.
- Every routing decision explainable — the `signals` object is never removed.
- Nothing silently dropped. Every request writes a database row, including
  every failure path.
- Configuration over code. Tiers, thresholds and provider settings live in
  `config.yaml`.

## Documentation layout

| File | Holds |
|---|---|
| `specs.md` | The authoritative spec. Amendments are made in place, marked. |
| `DECISIONS.md` | Choices not in the spec, with reasoning and costs. |
| `learnings/` | Reference library keyed by **technology**. See `learnings/00-INDEX.md`. |
| `PLAN.md` | Status, blockers, and the day-by-day delivery plan. |
| `README.md` | The outward-facing description. No placeholder metrics by Stage 6. |

### learnings/ structure

Keyed by technology, not by build stage. Every file has the same four parts:
**what it is** → **how it works** → **how we used it here** → **interview
questions with answers**.

`01-project-timeline.md` is the exception: it holds the chronological record of
what was built and every mistake made.

**When you introduce a new technology, tool, or protocol, add or extend the
matching file** — definition, its own workflow, how this project uses it, and
interview questions. **Every action taken gets logged** in
`01-project-timeline.md`: commands run, files written, what they returned, and
mistakes made. The mistakes matter as much as the successes; do not quietly fix
and move on.

---

## Current state (2026-09-08)

**Stage 1 — bare proxy: complete except for one item.**
**Stage 2 — two providers + naive routing: complete.**
**Stage 3 — real classifier: complete.**

Built: `config.yaml` + validation, `Provider` ABC with a four-type error
taxonomy, three adapters (Groq/Google/Anthropic) plus a mock, a provider
registry, `router.py` with the naive token-count rule, tiktoken counting,
SQLite logging on every path, 56 passing tests, and a 50-prompt routing
evaluation set.

**Blocked on:** a valid `GROQ_API_KEY` (current one is well-formed but 401s),
and there are no Google or Anthropic keys at all. **No adapter has ever spoken
to a live server** — all three are verified against synthetic responses only.

The mock provider means that blocks only the live calls; the request path, cost
arithmetic, routing, and logging are all verified without them.

### Stage 3 results (DECISIONS.md D15)

Held-out accuracy (n=32), the only honest column:

| mode | accuracy | too cheap | too expensive |
|---|---|---|---|
| naive_tokens | 31% | 20 | 2 |
| heuristic | **88%** | 4 | 0 |
| embedding | 72% | 0 | 9 |
| **hybrid (shipped)** | 72% | **0** | 9 |

**Hybrid is less accurate than the heuristic and is still the default.** It
eliminates every too-cheap misroute, and SPEC 8.4 rule 3 says a wrong cheap
answer costs more than a wrong expensive one. That is a values decision — one
config line to reverse.

Classification runs at **11-12ms p50** against the 20ms budget.

Three sets, and do not confuse them:
- `eval/prompts.json` — **tuned against**. A training score.
- `eval/labelled.json` — the embedding classifier's reference examples.
- `eval/holdout.json` — **never used for tuning.** Quote this one.

### Run it

**The virtualenv lives OUTSIDE this folder**, at `C:\dev\modelmux-venv`.
It reached 1.1 GB / 36,030 files once PyTorch arrived.

*Correction:* the move was originally justified by 60-80 second cold imports
that I attributed to OneDrive sync. **That diagnosis was wrong** -- OneDrive.exe
was not running, and imports dropped to ~8s on the third run purely from OS file
caching. The move is still right practice (1.1 GB of regenerable binaries do not
belong in a synced folder, and SPEC 13 says so) but it was hygiene, not a
performance fix. Source stays here; binaries do not.

```powershell
C:\dev\modelmux-venv\Scripts\python.exe -m uvicorn app.main:app --port 8000 --reload
C:\dev\modelmux-venv\Scripts\python.exe -m pytest tests/ -q
C:\dev\modelmux-venv\Scripts\python.exe eval/run_routing_check.py      # Stage 2 misroutes
C:\dev\modelmux-venv\Scripts\python.exe eval/run_classifier_eval.py    # Stage 3 accuracy
C:\dev\modelmux-venv\Scripts\python.exe eval/explain.py "your prompt"       # explain one decision
```

**Seeing a 200 without a valid API key.** `MODELMUX_MOCK_PROVIDERS=1` swaps
every adapter for the fake one, so the whole path -- routing, cost, the response
contract, the database row -- runs end to end with canned text. It prints a loud
banner because a server silently returning invented answers would be worse than
one that fails.

```powershell
$env:MODELMUX_MOCK_PROVIDERS="1"
C:\dev\modelmux-venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
$env:MODELMUX_MOCK_PROVIDERS=""     # unset when done
```

Tests marked `stage2_failure` assert the naive router's *wrong* behaviour on
purpose. When Stage 3 lands they should fail — that is the signal it worked.

The database lives at `%LOCALAPPDATA%\ModelMux\modelmux.db`, deliberately
outside this OneDrive-synced folder. See `DECISIONS.md` D7.

### Settled

- **Python 3.13 is fine.** `torch` publishes a `cp313 win_amd64` wheel and
  `sentence-transformers` is pure Python. No venv rebuild. (D8)
- **Redis will come from WSL2** — Ubuntu is already installed, Docker is not.
  Needs `sudo apt install redis-server` before Stage 4. (D9)

### Verify before trusting

- **Groq models and prices: VERIFIED 2026-09-08** against console.groq.com/docs/models.
  Small = `openai/gpt-oss-20b`, mid = `openai/gpt-oss-120b`. The Llama models
  are Enterprise/Contact-Sales and unusable on a developer key. (D14)
- **Large tier (Anthropic): STILL UNVERIFIED** — no key, and the prices are
  estimates never checked against a pricing page. Every `cost_if_large_usd`
  figure and therefore every savings claim depends on them.
- Mid tier is **temporarily on Groq** so the router can be exercised with one
  key; the Google entry is commented in `config.yaml` ready to swap back.
