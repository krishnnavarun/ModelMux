# Async Python & Concurrency

The single most important concept in this project. ModelMux is almost entirely a
**waiting** program.

---

## 1. What it is

**Synchronous** code runs one thing at a time and *blocks* while waiting. A
function that calls an API sits idle for the ~300ms the server takes. One thread,
one request.

**Asynchronous** code lets a single thread juggle many operations. At each
`await`, a function says *"I am waiting on I/O — run something else"*, and
resumes when the result arrives.

**Key distinction:**
- **Concurrency** — many tasks *in progress* at once (async does this)
- **Parallelism** — many tasks *executing* at once (needs multiple cores/processes)

Async gives you concurrency on **one** thread. It makes waiting cheap, not
computation fast.

---

## 2. How it works

### The event loop

```
┌─────────────────────────────────────────┐
│  EVENT LOOP                             │
│                                         │
│  ready queue: [task A, task C]          │
│  waiting on I/O: {task B: socket 7}     │
│                                         │
│  1. take next ready task, run it        │
│  2. it hits `await` on I/O              │
│  3. register the I/O, park the task     │
│  4. goto 1                              │
│  5. when the OS says socket 7 is ready, │
│     move task B back to ready queue     │
└─────────────────────────────────────────┘
```

The loop uses OS primitives (`epoll` on Linux, `IOCP` on Windows) to ask *"which
of these thousands of sockets has data?"* in one call.

### Coroutines

```python
async def fetch(url):        # calling this returns a coroutine object
    r = await client.get(url)  # ...it does NOT run yet
    return r.text
```

- `async def` defines a **coroutine function**
- Calling it returns a **coroutine object** — nothing executes
- It runs only when awaited or scheduled on the loop
- `await` = "suspend me here; resume when this completes"

**Forgetting `await` is the classic bug** — you get a coroutine object instead of
a result, and Python warns `coroutine was never awaited`.

### The rule that bites everyone

**One blocking call inside `async def` freezes the entire event loop.** Not just
that request — *every* concurrent request stalls.

| Never in async code | Use instead |
|---|---|
| `requests.get()` | `httpx.AsyncClient.get()` |
| `time.sleep()` | `await asyncio.sleep()` |
| `open()` / heavy file I/O | `aiofiles`, or `run_in_executor` |
| CPU-heavy loops | `run_in_executor` / a process pool |

For unavoidable blocking work: `await loop.run_in_executor(None, blocking_fn)`
pushes it to a thread pool so the loop keeps turning.

### Running several at once

```python
results = await asyncio.gather(fetch(a), fetch(b), fetch(c))   # all, fail fast
done, pending = await asyncio.wait(tasks, timeout=5)           # more control
```

Three 300ms calls take ~300ms total with `gather`, ~900ms sequentially.

---

## 3. How we used it in ModelMux

### Why async at all

A router forwards requests and waits. Almost 100% of its wall-clock time is
network I/O. Async is the difference between handling a handful of concurrent
requests and hundreds on the same hardware — and it costs nothing extra here
because FastAPI and httpx are both async-native.

### Every provider call yields

```python
# app/providers/groq.py
async def complete(self, prompt: str, model: str, max_tokens: int) -> ProviderResult:
    response = await self.client.post(...)   # <- yields control while waiting
```

### The mock respects it

```python
# app/providers/mock.py
if self.delay_seconds:
    # asyncio.sleep, never time.sleep: the latter would block the whole
    # event loop and stall every other in-flight request.
    await asyncio.sleep(self.delay_seconds)
```

**This detail matters more than it looks.** A mock that simulates latency with
`time.sleep` would misrepresent exactly the behaviour a load test measures — it
would serialise requests that production would overlap.

### Lifespan is an async context manager

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    ...startup...
    async with httpx.AsyncClient(timeout=timeout) as client:
        app.state.http = client
        yield          # <- app runs here
    ...shutdown...
```

Everything before `yield` runs once at startup, everything after once at
shutdown.

### Testing async code

`test_mock_counts_calls` is `async def`, and we never installed an async pytest
plugin. **An async test with no plugin can silently never run and still report
green.** Verified:

```
PASSED tests/test_providers.py::test_mock_counts_calls[asyncio]
```

The `[asyncio]` suffix showed anyio (bundled with starlette) parametrized and
actually executed it.

---

## 4. Interview questions

**Q: Explain async/await to someone who knows threads.**
Threads are preemptive — the OS can switch anywhere, so you need locks. Async is
cooperative — a task yields only at an explicit `await`, so between awaits you
have atomicity for free. One thread, so no GIL contention and no lock overhead,
but one blocking call stops everything.

**Q: Concurrency vs parallelism?**
Concurrency is dealing with many things at once (structure); parallelism is
doing many things at once (execution). Async gives concurrency on one thread.
For parallelism in Python you need multiprocessing, because the GIL prevents
threads executing bytecode simultaneously.

**Q: What actually happens at `await`?**
The coroutine suspends and returns control to the event loop, along with what it
is waiting for. The loop registers that with the OS, runs other ready tasks, and
resumes this coroutine when the OS reports readiness.

**Q: What if you call `time.sleep(5)` inside an async handler?**
The entire event loop blocks for 5 seconds. Every concurrent request stalls, not
just that one. Health checks time out and the process looks dead. Use
`await asyncio.sleep(5)`.

**Q: When is async the *wrong* choice?**
CPU-bound work — async does nothing for it and adds complexity. Also when your
libraries are sync-only, since you'd be wrapping everything in executors. Async
pays off for I/O-bound, high-concurrency workloads. *A router waiting on LLM
APIs is close to the ideal case.*

**Q: How do you run blocking code inside an async app?**
`await loop.run_in_executor(None, blocking_fn, args)` — offloads to a thread
pool so the loop keeps turning. FastAPI does this automatically for `def`
(non-async) route handlers.

**Q: `asyncio.gather` vs `asyncio.wait`?**
`gather` returns results in order and by default cancels/propagates on first
exception. `wait` returns `(done, pending)` sets, supports timeouts and
`return_when`, and doesn't raise — you inspect each task. Use `gather` for "I
need all of these", `wait` when you need partial results or timeouts.

**Q: What's the GIL and does async avoid it?**
The Global Interpreter Lock means only one thread executes Python bytecode at a
time. Async doesn't avoid it — it sidesteps needing to, by using one thread and
never blocking it. For CPU parallelism you still need multiple processes.

**Q: How do you test async code?**
An async plugin — `pytest-asyncio` or `anyio` — since plain pytest can't await a
coroutine. **Verify it actually runs**: an async test collected without a plugin
can report passed without executing. Check for the backend parametrization in
the test ID.
