# Configuration & Secrets

---

## 1. What it is

**Configuration** is everything that changes between environments or over time
without changing program logic: endpoints, thresholds, prices, limits.

**Secrets** are configuration that must never be seen: API keys, passwords,
tokens.

**YAML** is a human-readable data-serialisation format used for config files.
**Environment variables** are key/value pairs the OS gives a process.
**dotenv** loads a `.env` file into those variables for local development.

---

## 2. How it works

### Why configuration doesn't belong in code

Anything you expect to **tune by experiment** must not be in code. If a threshold
lives in Python, changing it is a code change — review, commit, redeploy. If it
lives in config, it's an edit and a restart.

This is one of the twelve-factor app principles: **strict separation of config
from code.**

### Why secrets go in environment variables

| Approach | Problem |
|---|---|
| Hardcoded in source | ends up in git forever |
| In a config file | same, unless carefully ignored |
| **Environment variable** | **not in the repo; every platform supports it** |
| Secret manager (Vault, AWS SM) | best for production; more setup |

Environment variables are the one mechanism Docker, systemd, Render, Fly,
Kubernetes and GitHub Actions all speak. Your code doesn't change between laptop
and server.

### The `.env` pattern

| File | Contains | In git? |
|---|---|---|
| `.env` | real secrets | **No** — gitignored |
| `.env.example` | the variable *names*, blank values | **Yes** |

`.env.example` is how a new contributor learns which variables exist without
ever seeing a value.

```python
from dotenv import load_dotenv
load_dotenv()          # copies .env into os.environ
os.environ["GROQ_API_KEY"]
```

#### The precedence rule that costs people weeks

**`load_dotenv()` does not overwrite a variable that is already set in the
environment.** `.env` fills in what is *missing*; it never wins a conflict.

```python
load_dotenv()                  # os.environ wins
load_dotenv(override=True)     # .env wins -- almost never what you want
```

This is deliberate and correct. A real deployment injects real variables, and a
stray `.env` file left in an image must not be able to override production
credentials. `.env` is a convenience for local development, not a source of
truth.

**The cost of being right here is that it is silent.** Nothing warns you that
the file you just edited is being ignored. On Windows the trap is worse,
because a variable can be set at three scopes:

| Scope | Lifetime | Set by |
|---|---|---|
| Process | this terminal only | `$env:NAME = "x"` |
| **User** | **permanent, survives reboots** | `[Environment]::SetEnvironmentVariable(..., 'User')` |
| Machine | permanent, all users | same, with `'Machine'` |

A **User**-scope variable set once, months ago, is invisible from inside the
project and outlives every terminal you open.

```powershell
[Environment]::GetEnvironmentVariable('GROQ_API_KEY','User')     # inspect
[Environment]::SetEnvironmentVariable('GROQ_API_KEY',$null,'User')  # remove
```

The Unix equivalent hides in `~/.bashrc`, `~/.zshrc` or `~/.profile` and is
exactly as durable.

### The critical git caveat

**git only ignores files it is not already tracking.** If a secret is ever
committed, adding it to `.gitignore` afterwards does nothing — it remains in
history, and **the key must be revoked**. Rewriting history (`filter-repo`,
BFG) helps only if nobody has cloned or forked.

### YAML gotchas

- Indentation is significant; **tabs are illegal**
- `yes`, `no`, `on`, `off` parse as booleans in YAML 1.1 — quote them
- **No variable expansion.** `${VAR}` is a literal string unless *you* expand it
- Always `yaml.safe_load()`, never `yaml.load()` — the latter can construct
  arbitrary Python objects, which is remote code execution on untrusted input

---

## 3. How we used it in ModelMux

### "Configuration over code" is a non-negotiable

Stage 1 originally had this in Python:

```python
MODEL = "llama-3.1-8b-instant"
COST_PER_1K_INPUT = 0.00005
```

Both moved to `config.yaml`. Prices and model names drift constantly, and the
classifier thresholds and cache similarity threshold will need repeated tuning.

### Validate at startup, not on first use

```python
# app/config.py
for field in ("name", "model", "cost_per_1k_input", "cost_per_1k_output"):
    if field not in provider:
        raise ConfigError(f"tier '{tier}' provider '{...}' is missing '{field}'")
```

**A missing price would otherwise surface as a silently wrong cost in every row
of the database** — a data error rather than a config error, and far harder to
notice. Fail loudly at startup.

There's a test for exactly this:

```python
def test_missing_price_raises_at_load_not_at_request(tmp_path):
    ...
    with pytest.raises(ConfigError, match="cost_per_1k_output"):
        config_module.load(...)
```

### Dated pricing, and honest VERIFY markers

```yaml
# PRICING SNAPSHOT: 2026-09-08
# !! VERIFY BEFORE TRUSTING ANY COST NUMBER !!
tiers:
  small:
    providers:
      - name: groq
        model: llama-3.1-8b-instant   # VERIFY: Groq renames/deprecates often
        cost_per_1k_input: 0.00005
        cost_per_1k_output: 0.00008
```

The spec warns that model names and prices drift. Marking them as unverified is
more useful than a confident-looking wrong number.

### We implement `${VAR}` expansion ourselves

YAML doesn't expand variables, so `config.py` does:

```python
def _resolve_db_path(raw_path: str) -> Path:
    override = os.environ.get("MODELMUX_DB_PATH")     # 1. env wins outright
    path = Path(os.path.expandvars(override or raw_path))   # 2. ${VAR}
    if "$" in str(path):
        raise ConfigError(f"database.path contains an unset variable: {path}")
    return path if path.is_absolute() else PROJECT_ROOT / path   # 3. relative
```

Three layers, most specific first. The `"$" in str(path)` check catches an unset
variable **loudly** — otherwise you'd silently create a directory literally named
`${LOCALAPPDATA}`.

### Never let a key reach a caller or a log

```python
api_key = os.environ.get("GROQ_API_KEY")
if not api_key:
    raise ProviderBadRequest("GROQ_API_KEY is not set")   # name only, no value
```

And upstream error bodies are truncated before being returned:

```python
detail = (detail or response.text)[:200]
```

### The mistake that cost weeks: the environment shadowed `.env`

**Every API key this project ever had returned 401** — three keys, several
weeks. The conclusion each time was "the key is bad".

The Groq console showed the newest key at **0 API Calls, Last Used: Never.**

> A rejected key still records an attempt. **Zero attempts means that key was
> never sent.** The evidence did not suggest a bad key — it ruled the key out
> of the story entirely.

A `GROQ_API_KEY` had been set at Windows **User** scope long before. Every run
loaded `.env`, found the variable present, and kept the dead value.

**Ask what evidence rules out, not what it suggests.** "401" invites you to
look at the key. "0 calls" tells you nothing ever reached the key at all, which
points at the process instead — a much smaller place to search.

### The fix: make shadowing impossible to miss

Deleting the variable fixes today, not the next one. `.env` loading moved into
`config.load_env()`, which compares `.env` against the environment and warns on
stderr whenever they differ:

```
!! GROQ_API_KEY in your environment SHADOWS the value in .env
   environment ends ...PxcK   .env ends ...yfoX
   the environment wins; .env is being ignored for this variable
```

Two design points worth defending:

- **It warns; it never changes behaviour.** Silently switching to `.env` would
  break the deployment case the precedence rule exists to protect.
- **It prints the last four characters, never the value** — enough to tell two
  secrets apart, useless in a log. See the leak below; this project learned
  that rule the hard way.

**It lives in `config.py`, not `main.py`.** It was in `main.py`, and the eval
scripts import `config` but never `main` — so the entire measurement harness
had never loaded environment variables in its life. Moving it fixed a bug
nobody had connected to it. `config.py` already owns where settings come from,
and "where does the environment come from" is the same question.

See `DECISIONS.md` D29, D31.

### The mistake: I printed a secret

While debugging the 401, I ran `od -c .env` to inspect byte encoding. My
redaction pattern failed and most of the key printed to the terminal.

**The right way to validate a secret is to check its *properties*, never dump
its value:**

```python
print('length     :', len(k))       # 56
print('prefix     :', k[:4])        # gsk_
print('quoted     :', k[:1] in ('"', "'"))
print('whitespace :', k != k.strip())
```

That told us the key was well-formed and correctly loaded — so the 401 was the
credential itself, not our parsing — **without the value ever being displayed.**

---

## 4. Interview questions

**Q: How do you manage secrets in an application?**
Never in source or committed config. Environment variables at minimum, injected
by the platform; a secret manager (Vault, AWS Secrets Manager, sealed secrets)
in production, ideally with rotation. Commit a `.env.example` documenting the
names, never the values.

**Q: You put a key in `.env`, the app still says 401. Where do you look?**
First establish whether the key was ever *sent*: most consoles show a call count
or last-used timestamp. Zero calls means the problem is local, not the
credential. The usual cause is that `load_dotenv()` **does not override a
variable already in the environment** — so something set it earlier: a shell
profile, a Docker `-e` flag, a CI secret, or on Windows a permanent User-scope
variable. Compare the two values by their last four characters, never by
printing them.

**Q: Why does `load_dotenv()` let the environment win? That seems unhelpful.**
Because production injects real variables, and a `.env` accidentally baked into
an image must never be able to override them. Development convenience must not
outrank deployment configuration. The right response to the silence is to
*detect and warn* on a conflict, not to flip the precedence.

**Q: A secret got committed to git. What now?**
**Revoke it immediately** — that's the only step that truly matters, because it
may already be cloned, forked, or scraped. Then rotate, then optionally rewrite
history with `git filter-repo`/BFG. Adding it to `.gitignore` afterwards does
nothing; git only ignores untracked files.

**Q: What belongs in config vs code?**
Anything that varies by environment or gets tuned by experiment: endpoints,
credentials, thresholds, limits, prices, feature flags. Business logic stays in
code. *A useful test: if you'd change it to run an experiment, it's config.*

**Q: Why validate config at startup instead of on use?**
Fail fast, with a clear message, before serving traffic. Lazy validation turns a
config typo into a runtime error hours later — or worse, into silently wrong
data. *A missing price in my project would have produced a wrong cost in every
logged row rather than any error at all.*

**Q: `yaml.load` vs `yaml.safe_load`?**
`yaml.load` can instantiate arbitrary Python objects, so untrusted YAML is
remote code execution. `safe_load` restricts to basic types. Always `safe_load`
unless you fully control the input and need custom tags.

**Q: How do you handle config differing across dev/staging/prod?**
Environment variables for anything that differs or is secret; a committed base
config for shared defaults; optional per-environment overlay files. Keep one
schema and validate it identically everywhere, so a missing prod value fails at
deploy rather than at 3am.

**Q: What are the twelve-factor principles you actually apply?**
Config in the environment; explicit declared dependencies (pinned
requirements.txt); stateless processes; logs as event streams to stdout/stderr;
dev/prod parity. *This project follows all five.*

**Q: How would you validate a secret is correctly configured without printing
it?**
Check its properties — length, prefix, absence of quotes or whitespace — and make
a cheap authenticated call to see whether the provider accepts it. *I learned
this the hard way: I dumped `.env` bytes to a terminal while debugging and
exposed a live key.*
