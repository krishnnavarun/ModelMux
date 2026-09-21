# Testing with pytest

---

## 1. What it is

**pytest** is Python's dominant testing framework. Plain functions named
`test_*`, plain `assert`, with fixtures for setup and a large plugin ecosystem.

**Fixture** — reusable setup/teardown injected into tests by name.
**conftest.py** — where fixtures shared across a directory live.
**Marker** — a label on a test, for selecting or configuring it.

---

## 2. How it works

### Test discovery

Files `test_*.py`, functions `test_*`, classes `Test*` (without `__init__`).

### Fixtures are dependency injection

```python
@pytest.fixture
def db():
    conn = connect()
    yield conn          # test runs here
    conn.close()        # teardown

def test_something(db):   # requested by NAME
    ...
```

Scopes: `function` (default), `class`, `module`, `session`. `autouse=True`
applies without being requested.

### conftest.py and its subtlety

Fixtures in `conftest.py` are available to all tests in that directory and
below, with no import.

**But hooks are different.** pytest honours *initialisation hooks* —
`pytest_sessionstart`, `pytest_sessionfinish`, `pytest_configure`,
`pytest_addoption` — **only from the rootdir conftest.py or installed plugins**.
Define one in a subdirectory conftest and it is collected, looks correct, and
silently never runs.

### Parametrize

```python
@pytest.mark.parametrize("status,expected", [
    (400, ProviderBadRequest),
    (429, ProviderRateLimited),
    (500, ProviderServerError),
])
def test_status_maps(status, expected):
    assert isinstance(translate(status), expected)
```

One function, N independent test cases, each reported separately.

### Test doubles

| Type | What it does |
|---|---|
| **Dummy** | passed but never used |
| **Stub** | returns canned answers |
| **Fake** | working but simplified implementation |
| **Mock** | records calls, asserts on interactions |
| **Spy** | real thing + call recording |

`MockProvider` in this project is really a **fake** — a working implementation
with configurable behaviour — plus call counting.

### The testing pyramid

Many fast unit tests, fewer integration tests, very few end-to-end. Inverted
pyramids are slow and flaky.

---

## 3. How we used it in ModelMux

### Isolation is the whole game

Two things a test must never touch: the real database, and a real provider.

```python
# tests/conftest.py — order is load-bearing
_TEST_DB = Path(os.environ["TEMP"]) / f"modelmux-test-{uuid.uuid4().hex}.db"
os.environ["MODELMUX_DB_PATH"] = str(_TEST_DB)

from app.main import app       # <- imported AFTER the env var is set
```

`main.py` loads config at **import time** (deliberately — a broken config should
stop the process immediately). So by the time `app` is imported, the database
path is fixed. Setting the variable afterwards would be too late, and tests
would quietly write to the real database.

A fresh `uuid4` per run means two runs never share state.

### TestClient must be a context manager

```python
with TestClient(app) as test_client:      # runs the app's lifespan
```

Without `with`, `db.init_db()` never executes and every test fails on a missing
table — you'd be testing a differently-configured app than production.

### The bug that would have spent money

The Stage 1 fixture mocked one provider:

```python
test_client.app.state.providers["groq"] = mock_provider
```

Correct while everything routed to `groq`. **Stage 2 made routing real** — a long
prompt now resolves to `google`, which was still the *real adapter*. A test
posting 200 words attempted a live HTTPS call to Google.

It surfaced as a confusing `KeyError: 'routing'` — but only because there is no
`GOOGLE_API_KEY`. **With valid keys in `.env`, that test would have passed while
making paid API calls on every run.**

```python
for name in list(test_client.app.state.providers):
    test_client.app.state.providers[name] = mock_provider
```

**Two lessons — the second is bigger:**
1. Adding a code path silently widens what your doubles must cover.
2. **Green is not proof of isolation.**

### Asserting the *reason*, not just the outcome

```python
def test_oversized_prompt_returns_400_not_a_crash(client, mock_provider):
    response = client.post("/v1/chat", json={"prompt": "x" * 100_001})
    assert response.status_code == 400
    assert mock_provider.calls == 0        # <- the real assertion
```

The status code alone would pass even if we called the provider first and threw
the answer away. `calls == 0` proves rejection happened **before** spending
money — which is the actual requirement.

### Characterisation tests — pinning known-wrong behaviour

```python
@pytest.mark.stage2_failure
def test_short_hard_prompt_is_misrouted_to_small(config):
    decision = router.select_tier("Prove Fermat's Last Theorem.", config)
    assert decision.tier == "small", "if this fails, Stage 3 fixed it -- update the test"
```

This asserts the **wrong** answer deliberately. When Stage 3's classifier lands
and this fails, **that failure is the signal it worked.** Without it, "did
reasoning markers help?" is answered by squinting at percentages.

The marker is registered so pytest doesn't warn:

```ini
# pytest.ini
[pytest]
markers =
    stage2_failure: pins a known failure of the Stage 2 naive router
```

### Verifying a test isn't vacuous

`test_mock_counts_calls` is `async def` with no async plugin installed. Such a
test can silently never run and still report green. Checked:

```
PASSED tests/test_providers.py::test_mock_counts_calls[asyncio]
```

The `[asyncio]` suffix proved anyio parametrized and executed it.

**When a test passes for a reason you cannot name, find the reason.**

### Cleaning up after yourself

The first attempt used `pytest_sessionfinish` in `tests/conftest.py`. It never
fired — subdirectory conftest, initialisation hook. Eight stray databases had
accumulated in TEMP before anyone looked.

```python
@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_db():
    yield
    for suffix in ("", "-wal", "-shm"):
        Path(str(_TEST_DB) + suffix).unlink(missing_ok=True)
```

---

## 4. Interview questions

**Q: Fixture vs setUp?**
Fixtures are composable, explicitly requested by name, have scopes, and support
teardown via `yield`. `setUp` is per-class and implicit. Fixtures also override
cleanly per directory via conftest.

**Q: What are fixture scopes and when do you use each?**
`function` (default) — fresh per test, safest. `module`/`class` — share
expensive setup within a file. `session` — once per run, for things like loading
an ML model. Broader scope is faster but risks state leaking between tests.

**Q: Mock vs stub vs fake?**
A stub returns canned data. A fake is a working simplified implementation (an
in-memory database). A mock records interactions and asserts on them. *My
`MockProvider` is a fake with call counting — it behaves like a provider rather
than just returning a value.*

**Q: How do you test code that calls a third-party API?**
Inject the client or adapter rather than constructing it inside, then substitute
a fake in tests. Alternatively record/replay (VCR). Keep a small number of real
contract tests run separately from CI's fast suite, so you notice when the
upstream changes.

**Q: Your test suite passes but you're not sure it tests anything. How do you
check?**
Deliberately break the code and confirm tests fail — the cheap version of
mutation testing. Check coverage, but treat it as necessary not sufficient.
*And verify async tests actually execute; without a plugin they can report
passed without running.*

**Q: What's a characterisation test?**
A test that pins current behaviour rather than desired behaviour, so any change
becomes visible. Useful for legacy code before refactoring, and *for
deliberately-wrong baselines you plan to replace — mine assert the naive
router's misroutes so the classifier's arrival shows up as a failure.*

**Q: How do you keep tests from hitting production resources?**
Environment variable overrides for paths and endpoints, set before the app
imports; substituted adapters for anything external; and where possible a
network-blocking plugin so an escaped call errors loudly rather than silently
succeeding. *I had a fixture that only mocked one of three providers — with real
keys it would have spent money on every run.*

**Q: What does `conftest.py` do, and what's the catch?**
It holds fixtures and hooks shared by tests in its directory and below, with no
import needed. The catch: **initialisation hooks like `pytest_sessionfinish`
only work from the rootdir conftest or a plugin.** In a subdirectory they're
collected and silently ignored.

**Q: How do you test error paths?**
Make failures injectable. *My fake provider takes `fail_with="timeout" |
"rate_limited" | "server_error" | "bad_request"`, so retry, fallback and
circuit-breaker behaviour can be tested deterministically instead of waiting for
a real outage.*
