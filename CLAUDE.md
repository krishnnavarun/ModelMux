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
| `learning/` | Study guide + interview prep, keyed by how it gets asked. See `learning/00-START-HERE.md`. **Note the singular/plural distinction.** |
| `PLAN.md` | Status, blockers, and the day-by-day delivery plan. |
| `SETUP.md` | How to run it, and the short list of things only the owner can do. |
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

## Current state (2026-09-21)

**All six stages built. The key finally worked** -- Stage 1's last criterion
(a live `200`) closed on 2026-09-21, and cost and latency are measured rather
than projected for the first time.

`131 tests passing`, 37 decisions recorded, 19 learnings files,
12 study-guide files. **All six stages complete; quality is graded.**

### The 401 that lasted weeks was never the key (D29)

A **Windows User-scope** `GROQ_API_KEY` shadowed `.env`. `load_dotenv()` does
not overwrite a variable already in the environment, so every run kept the dead
key. The Groq console's **"0 API Calls"** was the clue: a rejected key still
records an attempt, so zero attempts meant that key was never sent.

`config.load_env()` now warns loudly whenever the environment shadows a
differing `.env` value -- printing the last four characters of each, never the
key.

### First live measurement (D33)

32 held-out prompts, 0 failures, total spend $0.027.

| | ModelMux | baseline | |
|---|---|---|---|
| p50 latency | **2,803 ms** | 6,973 ms | **2.5x faster** |
| cost / 1k, run 1 | $0.4121 | $0.4411 | 6.6% -- **did not reproduce** |
| cost / 1k, run 2 | $0.4181 | $0.4253 | **1.7%** (same prompts) |
| **same-token routing effect** | | | **3-4%** -- quote this one |
| cost / 1k, projected at configured tiers | $11.49 | $18.25 | 37.1% saved |

**Do NOT quote an A/B cost saving from this configuration (D36).** Two runs
of the identical set gave 6.6% and 1.7% -- the routing effect is 3-4% and
run-to-run output-length variance is +/-5 points, so the noise is larger than
the signal. Quote the **same-token** figure (3-4%), or the projection, and say
which ladder.

With one key the large tier falls back to `gpt-oss-120b` (D30) -- the same
model as mid -- so the ladder compresses to 2x and 16 of 32 prompts route
somewhere the baseline would have gone anyway. A router's savings are bounded
by the price spread it is given.

**The latency result DID reproduce** (2.49x then 2.45x at p50) because it
depends on which model answered, not on how many tokens it emitted.

The projection reprices *measured* token counts at the configured rates. It is
arithmetic, not simulation, and it is labelled as projected everywhere.

**D3's bias, finally measured:** the baseline emitted 1.04x the routed tier's
output tokens. Approximately unbiased.

### Two things to know before trusting a number here

- **Quality IS graded now (D37), but the honest n is 8.** The 30-item blind
  spot-check was graded by an LLM-as-judge. 22 of the 30 pairs compared a
  model WITH ITSELF, because with one key `large` falls back to the mid
  tier's model -- those measure nondeterminism, not routing. On the 8 pairs
  where routing actually changed the model: **not worse in 8/8, zero losses.**
  `grade_quality.py` now performs that split itself and labels the meaningful
  subset. The **human** half of D23 is still unfilled; the grader was the same
  agent that wrote the router.
- **The classification budget test is flaky in the full suite (D35, OPEN).**
  11-12ms alone, intermittently ~85ms in-suite against a 20ms budget. Cause
  still unknown, but now genuinely narrowed: sampling noise, the HTTP path,
  test ordering and **CPU contention** are all ruled out. Contention was
  refuted by a correct experiment -- 12 cores saturated gives only 11-15ms.
  **Do not quote 11-12ms without saying it was an idle process.**

### Re-running the measurement

```powershell
redis-cli FLUSHDB     # mock-provider runs poison the real cache with canned answers
C:\dev\modelmux-venv\Scripts\python.exe eval/run_eval.py --set holdout.json
```

It **refuses** to run against mock providers without `--simulated`, because a
plausible fake in a results table is worse than no number at all. That guard
did not stop it publishing figures inflated 40x from real data (D31) -- the
eval priced every call at the tier's *first* provider while Groq answered all
of them. Server and eval now share `config.cost_for_provider()`.

### Still open for the project owner

- **Quality scoring method** (D23). Recommendation: blind human spot-check of
  30 plus LLM-as-judge on the full set, reported as separate columns, never
  averaged. `run_eval.py` already exports the blind spot-check file — gradeable
  today, no key needed.
- ~~Anthropic's large-tier prices are UNVERIFIED~~ — **VERIFIED 2026-09-21**
  (D27). They were `claude-sonnet-5` at Sonnet **4.6** rates: wrong model, wrong
  generation's price. Now `claude-opus-5` at 0.005 / 0.025 per 1K.
- **`ANTHROPIC_API_KEY` -- the single highest-value input left.** One line
  in `.env`; no config change needed, since `config.yaml` already lists
  anthropic first under `tiers.large`. It simultaneously widens the ladder
  from 2x to ~33x (fixing D36's noise problem), takes the quality sample from
  n=8 to n=30 (fixing D37's main limitation), and gives the Anthropic adapter
  its first live call. See `SETUP.md`.
- **A human grader for 30 answers** (~40 min, no key). D23 asked for a human
  spot-check AND LLM-as-judge as separate columns; only the second is filled.
- **D35, still open but narrowed:** the classification budget test's
  in-suite flake. The CPU-contention hypothesis is now **refuted** by a
  correct experiment (separate processes, not GIL-bound threads): under full
  saturation of 12 cores the median reaches only 11 ms unpinned / 15 ms
  pinned, against an in-suite failure of 85 ms. Side finding worth keeping:
  `torch.set_num_threads(1)` costs ~4 ms at the median and makes the tail 3.5x
  better (18.7 ms vs 65.8 ms worst case). Not adopted -- never measured under
  real concurrency.

### Deliberately cut

`GET /v1/stream` (SSE) and the React/Vite/Recharts dashboard — see D24. The
dashboard is one static file at `/dashboard`.

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
- **Large tier (Anthropic): prices VERIFIED 2026-09-21** (D27).
  `claude-opus-5` at 0.005 / 0.025 per 1K. It previously read
  `claude-sonnet-5` at 0.003 / 0.015 — which is Sonnet **4.6** pricing, so it
  was neither the model named nor any current rate.
  **The adapter is still unverified**: there is no Anthropic key, so the large
  tier is served by its Groq fallback (D30) and no Anthropic call has ever been
  made. The *prices* are verified; the *adapter* is not.
- Mid tier is **temporarily on Groq** so the router can be exercised with one
  key; the Google entry is commented in `config.yaml` ready to swap back.
