# Setup — what is needed to run ModelMux, and what is still outstanding

Two parts: **getting it running** (everything here works today), and **the
short list of things only you can do** at the end.

---

## Part 1 — Running it

### What you need

| Requirement | Status on this machine | Notes |
|---|---|---|
| Python 3.11+ | ✅ 3.13 at `C:\dev\modelmux-venv` | 3.13 is verified fine (D8) |
| Dependencies | ✅ installed | 9 packages, `requirements.txt` |
| Redis | ✅ via WSL2 | no supported native Windows build (D9) |
| `GROQ_API_KEY` | ✅ in `.env` | the only required key |
| Anthropic / Google keys | ❌ **not set** | optional — see Part 2 |

### The virtualenv is deliberately outside the project

`C:\dev\modelmux-venv` — 1.1 GB and 36,030 files once PyTorch arrived. That
does not belong in a OneDrive-synced folder (SPEC 13). Source stays here,
binaries do not.

```powershell
C:\dev\modelmux-venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Start Redis (WSL2)

```powershell
wsl -e sudo service redis-server start
wsl redis-cli PING            # expect: PONG
```

If the cache tests skip with *"Redis unavailable"*, this is why. Note that a
skip reads as success in a `-q` summary — see D35.

### Run the server

```powershell
C:\dev\modelmux-venv\Scripts\python.exe -m uvicorn app.main:app --port 8000 --reload
```

Then `http://localhost:8000/dashboard`, or:

```powershell
curl -X POST http://localhost:8000/v1/chat -H "Content-Type: application/json" `
  -d '{\"prompt\": \"What is the capital of Japan?\"}'
```

### Everything you can run without spending anything

```powershell
# 131 tests, no network, no key
C:\dev\modelmux-venv\Scripts\python.exe -m pytest tests/ -q

# explain one routing decision in full
C:\dev\modelmux-venv\Scripts\python.exe eval/explain.py "Prove there are infinitely many primes."

# classifier accuracy, all 4 modes x both sets, with the overfitting gap
C:\dev\modelmux-venv\Scripts\python.exe eval/run_classifier_eval.py

# the Stage 2 misroute report
C:\dev\modelmux-venv\Scripts\python.exe eval/run_routing_check.py

# cache threshold sweep
C:\dev\modelmux-venv\Scripts\python.exe eval/tune_cache_threshold.py

# score an existing graded spot-check
C:\dev\modelmux-venv\Scripts\python.exe eval/grade_quality.py eval/results/spotcheck-20260921-200552.json
```

### The live evaluation (spends money — about $0.03)

```powershell
wsl redis-cli FLUSHDB     # mock runs poison the cache with canned answers
C:\dev\modelmux-venv\Scripts\python.exe eval/run_eval.py --set holdout.json
```

It **refuses** to run against mock providers without `--simulated`.

### Seeing a 200 with no key at all

```powershell
$env:MODELMUX_MOCK_PROVIDERS="1"
C:\dev\modelmux-venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
$env:MODELMUX_MOCK_PROVIDERS=""      # unset when done, then FLUSHDB
```

---

## Part 2 — What only you can do

Three items. **The first is free and takes five minutes**, the second is
optional and costs money, and the third needs a person rather than a key.

### ① Get a free Google AI Studio key — 5 minutes, $0

**Exactly how:**

1. Go to **https://aistudio.google.com/apikey**
2. Sign in with any Google account
3. Click **"Create API key"**
4. Pick a Google Cloud project when asked, or let it create one — the free
   tier needs **no billing account and no card**
5. Copy the key (it starts `AIza...`)
6. Paste it into `.env`:

```
GOOGLE_API_KEY=AIza...
```

7. **Open a new terminal** — see the failure mode at the bottom of this file
8. Verify it took effect:

```powershell
C:\dev\modelmux-venv\Scripts\python.exe eval/explain.py "Prove there are infinitely many primes."
```

The `provider/model` line should read `google / gemini-3.1-pro-preview`. If
it says `groq`, the key is not being seen.

9. Run the real thing:

```powershell
wsl redis-cli FLUSHDB
C:\dev\modelmux-venv\Scripts\python.exe eval/run_eval.py --set holdout.json
```

**No config change is needed.** `config.yaml` already lists google first
under `tiers.large`.

#### What it fixes

| Without the key | With it |
|---|---|
| large tier = `gpt-oss-120b` = **the mid tier's own model** | `gemini-3.1-pro-preview` |
| ladder **1×** | **27× input / 40× output** |
| cost saving 3-4%, unmeasurable by A/B (±5 pt noise) | signal clears the noise |
| quality **n=8** — 22 of 30 pairs compared a model with itself | **n=30** |

#### Things to know

- **It is a preview model.** `-preview` names get renamed and retired. A 404
  means the model moved, not that your key is bad — check the model list.
  `gemini-3.8-flash` is the stable alternative (narrower ladder, also free).
- **Free tier is rate-limited** per minute and per day. Retry and backoff
  absorb it; a full eval may just run slower.
- **Gemini 3 thinks by default and bills thinking as output.** The adapter
  accounts for that and raises rather than returning an empty answer if the
  budget runs out (D38). `max_tokens_default` was raised to 4000 to give
  reasoning room.
- **Please run the eval 3-5 times.** D36 exists because one run was published
  and did not reproduce.

### ② Anthropic — optional, not free

**A Claude Pro subscription does not include API credits.** Pro covers
claude.ai and Claude Code; the API is separately billed via prepaid credits
at [console.anthropic.com](https://console.anthropic.com/settings/keys).

New API accounts do receive a small free credit grant — worth checking your
balance, since a 32-prompt run costs roughly **$0.30-0.60** at `claude-opus-5`
rates, or about a fifth of that on `claude-sonnet-5`.

To use it: uncomment the `anthropic` provider under `tiers.large` in
`config.yaml` (its prices are already verified) and set `ANTHROPIC_API_KEY`.

### ③ Grade 30 answers by hand — no key needed, ~40 minutes

The quality column currently holds an **LLM-as-judge** grade. D23 asked for
that **and** a human spot-check, reported as **separate columns, never
averaged** — because averaging them hides which one disagreed.

A fresh blind file is exported by every eval run. To grade one:

1. Open `eval/results/spotcheck-<stamp>.json`
2. For each of the 30 items, read the prompt and both answers
3. Set `"better"` to `"A"`, `"B"`, or `"same"`, and optionally a note
4. **Do not open the `_KEY.json` file first** — that is the bias the format exists to remove
5. Then:

```powershell
C:\dev\modelmux-venv\Scripts\python.exe eval/grade_quality.py eval/results/spotcheck-<stamp>.json
```

You are the only available *independent* grader — the existing column was
graded by the same agent that wrote the router, which is stated everywhere it
is quoted but is still a real weakness.

---

## The one failure mode to recognise

If you add a key and still get **401**, check this before anything else:

```powershell
[Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY','User')
[Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY','Machine')
$env:ANTHROPIC_API_KEY
```

`load_dotenv()` does not overwrite a variable already set in the environment,
so a stale one silently wins and `.env` is ignored. The app warns on stderr
when it detects this:

```
!! ANTHROPIC_API_KEY in your environment SHADOWS the value in .env
   environment ends ...XXXX   .env ends ...YYYY
```

**A provider console showing 0 API calls means the key was never sent** — a
rejected key still records an attempt. That distinguishes "bad key" from
"never reached the key" in one glance, and it is how D29 was finally found
after three keys and several weeks.

After clearing a persistent variable, **open a new terminal** — existing
processes keep the copy they were spawned with.

---

## Current state, honestly

| | |
|---|---|
| Tests | **131 passing** |
| Decisions recorded | **37** |
| Stages | **all six complete** |
| Classification | 11-12 ms (budget 20) — *idle process*, see D35 |
| Cache lookup | 11.2 ms at 10k entries (budget 30) |
| Classifier accuracy, held out | 88% heuristic · 72% hybrid with **zero** too-cheap |
| Latency | **2.5× faster** at p50, reproduced across two runs |
| Cost | **3-4%** routing effect; A/B is noise-dominated on this ladder (D36) |
| Quality | **not worse in 8/8** where routing changed the model (D37) |
| **Open** | D35 — an unexplained in-suite test flake, cause narrowed not found |
