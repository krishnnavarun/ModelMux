# FastAPI, ASGI & Uvicorn

---

## 1. What it is

- **FastAPI** — a Python web framework for building APIs, built on Starlette
  (routing/HTTP) and Pydantic (validation). Async-native.
- **ASGI** (Asynchronous Server Gateway Interface) — the standard contract
  between an async Python web app and the server running it.
- **Uvicorn** — an ASGI server. It owns the socket, speaks HTTP, and calls your
  app.
- **Starlette** — the lightweight ASGI toolkit FastAPI is built on.

---

## 2. How it works

### WSGI vs ASGI

**WSGI** (Flask, Django classic) is synchronous — one callable, one request,
blocking:

```python
def app(environ, start_response): ...
```

**ASGI** is async and supports long-lived connections (WebSockets, SSE):

```python
async def app(scope, receive, send): ...
```

- `scope` — metadata about the connection (type, path, headers)
- `receive` — await incoming events
- `send` — emit outgoing events

FastAPI wraps all of this so you write functions and decorators instead.

### Who does what

```
  Client
    │  HTTP over TCP
    ▼
  Uvicorn        owns the socket, parses HTTP, runs the event loop
    │  ASGI (scope, receive, send)
    ▼
  Starlette      routing, middleware, request/response objects
    │
    ▼
  FastAPI        validation, serialisation, dependency injection, OpenAPI docs
    │
    ▼
  Your handler
```

`uvicorn app.main:app` means: import module `app.main`, take the object named
`app`. That is why running from the project root matters — Python must be able
to find the `app` package.

### Request lifecycle

```
1. Uvicorn accepts the connection, parses the HTTP request
2. Builds an ASGI scope, calls the app
3. Starlette matches the route
4. FastAPI resolves dependencies and validates the body against the model
   └─ invalid? -> 422, handler NEVER runs
5. Your handler executes
6. Return value serialised to JSON
7. BackgroundTasks run AFTER the response is sent
```

Step 4 is why "validate at the edge" works — by the time your code runs, input
is already known-good.

### Lifespan

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup: runs once
    yield
    # shutdown: runs once

app = FastAPI(lifespan=lifespan)
```

The place for anything expensive that should be created once: connection pools,
ML models, database schema setup. `app.state` is the scratch space to hang them
on.

### Dependency injection

```python
async def chat(request: ChatRequest, background: BackgroundTasks):
```

FastAPI inspects the type annotations and supplies each argument. A Pydantic
model → parsed body. A known type like `BackgroundTasks` → framework-provided.
`Depends(fn)` → whatever `fn` returns, with caching and override support (very
useful in tests).

---

## 3. How we used it in ModelMux

### Lifespan owns the expensive things

```python
# app/main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(config.db_path)
    tokens.init()          # loads + warms the tokenizer once

    async with httpx.AsyncClient(timeout=timeout) as client:
        app.state.http = client
        app.state.providers = build_all(config, client)
        yield
```

Three things created exactly once: the database schema, the warmed tokenizer,
and the HTTP connection pool. Doing any per-request would be a serious
regression — the tokenizer alone costs ~6.6s to load and 48ms on first encode.

### Config loaded at import, deliberately

```python
config = config_module.load()   # module level, not inside lifespan
```

A broken `config.yaml` stops the process **immediately** with a clear message,
rather than surfacing as a `KeyError` on the first request hours later.

*This has a testing consequence:* `MODELMUX_DB_PATH` must be set **before**
`app.main` is imported, which is why `conftest.py` sets it at the very top.

### Background tasks for logging

```python
background.add_task(db.log_request, record)
```

Runs **after** the response is sent, so the caller never waits on disk I/O.

**The honest trade-off:** a background task runs after the response, so a
process killed at the wrong instant loses that row. That is a real gap against
"nothing is silently dropped" — accepted because the alternative is making every
caller wait on a write.

### The 422 we cannot avoid

FastAPI's validation runs *before* our handler, so a Pydantic constraint failure
returns **422** and our code never sees it — meaning it could never write a
database row. That is why prompt length is checked *in the handler* against
config, not as `Field(max_length=...)`. → DECISIONS.md D1.

### TestClient runs the lifespan

```python
with TestClient(app) as test_client:   # context manager form runs lifespan
```

Without the `with`, `db.init_db()` never executes and every test fails on a
missing table — testing a differently-configured app than production.

---

## 4. Interview questions

**Q: WSGI vs ASGI?**
WSGI is the synchronous standard — one callable per request, blocking, no
long-lived connections. ASGI is async and event-based (`scope`, `receive`,
`send`), supporting WebSockets and SSE. ASGI is a superset in capability; ASGI
servers can run WSGI apps via an adapter.

**Q: Why is FastAPI fast?**
Mostly because it's ASGI/async — high concurrency for I/O-bound work — plus
Pydantic v2's validation core is written in Rust. It's not faster at CPU work;
it's better at *waiting*.

**Q: What does `uvicorn app.main:app` mean?**
Import the module `app.main`, take the attribute named `app` from it, and serve
that ASGI application.

**Q: What is lifespan for?**
Startup/shutdown hooks. Anything expensive that should exist once per process —
connection pools, ML models, schema creation — goes there rather than per
request. Shutdown is where you close them cleanly.

**Q: How does FastAPI's dependency injection work?**
It reads type annotations on handler parameters. Pydantic models come from the
request body; known framework types are supplied directly; `Depends(fn)` calls
`fn` and injects its result, with per-request caching and test-time overrides.

**Q: Where would you put a database connection pool?**
In lifespan, stored on `app.state`. Creating one per request destroys the point
of pooling; creating one at module import can bind to an event loop that doesn't
exist yet.

**Q: What's the risk of BackgroundTasks?**
They run in-process after the response. If the process dies between response and
task, the work is lost, and there's no retry or durability. Fine for
best-effort logging; wrong for anything that must not be lost — that needs a
real queue.

**Q: How do you test a FastAPI app without a live server?**
`TestClient`, which drives the ASGI app in-process. **Use the context-manager
form** so the lifespan actually runs — otherwise startup work like schema
creation never happens and you're testing a different app.

**Q: How would you scale a FastAPI service?**
Vertically first — async already handles high I/O concurrency on one process.
Then multiple worker processes (`--workers`, or gunicorn+uvicorn workers) to use
more cores. Then horizontally behind a load balancer. Watch for per-process
state that silently stops being global — *our circuit breaker will have exactly
this limitation.*
