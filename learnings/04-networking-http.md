# Networking — TCP, TLS, HTTP, and httpx

---

## 1. What it is

- **TCP** (Transmission Control Protocol) — reliable, ordered, connection-based
  byte stream between two machines. Guarantees delivery and order; the
  application sees a pipe, not packets.
- **TLS** (Transport Layer Security) — encryption layered on top of TCP. The `s`
  in HTTPS.
- **HTTP** — a request/response text protocol carried over TCP (+TLS).
- **httpx** — a modern Python HTTP client supporting both sync and async, with
  connection pooling.

---

## 2. How it works

### The layers

```
  Your code          "GET /v1/chat"
      │
  HTTP              method, path, headers, body
      │
  TLS               encrypt / decrypt
      │
  TCP               reliable ordered bytes, retransmission
      │
  IP                routing between hosts
```

### Opening a connection is expensive

```
  CLIENT                                SERVER
    │──────── SYN ──────────────────────▶│   ┐
    │◀─────── SYN-ACK ───────────────────│   │ TCP handshake (1 round trip)
    │──────── ACK ──────────────────────▶│   ┘
    │                                     │
    │──────── ClientHello ──────────────▶│   ┐
    │◀─────── ServerHello, cert ─────────│   │ TLS handshake (1-2 round trips)
    │──────── key exchange ─────────────▶│   ┘
    │                                     │
    │──────── HTTP request ─────────────▶│   finally, actual data
```

**Typically 100–200ms before a single byte of your request is sent.** Latency,
not bandwidth, is what makes this hurt — each round trip costs the physical
distance to the server.

### Connection pooling

Keep the connection open after the response and reuse it (HTTP keep-alive). The
second request skips both handshakes entirely.

**A client created per request pays the handshake every time.** A long-lived
client with a pool pays it roughly once per host.

### Timeouts — you need more than one

| Timeout | Bounds | Should be |
|---|---|---|
| **connect** | establishing the TCP+TLS connection | **short** — a host that won't accept in 5s is down |
| **read** | waiting for response data | **long** — the server may be legitimately working |
| **write** | sending the request body | short |
| **pool** | waiting for a free connection from the pool | short |

A single flat timeout cannot express *"give up on an unreachable host quickly,
but be patient with a thinking model."*

**No timeout at all** means a hung server holds your connection forever.

### HTTP status codes — whose fault is it?

| Range | Meaning |
|---|---|
| `2xx` | success |
| `3xx` | redirect |
| **`4xx`** | **the caller did something wrong** |
| **`5xx`** | **the server did, or something it depends on did** |

Ones that matter here:

- `400` Bad Request — malformed caller input
- `401` Unauthorized — bad/missing credentials
- `404` Not Found
- `422` Unprocessable Entity — well-formed but semantically invalid (FastAPI's
  validation default)
- `429` Too Many Requests — rate limited; often carries `Retry-After`
- `500` Internal Server Error — *we* broke
- **`502` Bad Gateway — we are a gateway and our upstream failed**
- `503` Service Unavailable — temporarily down
- `529` — Anthropic's non-standard "overloaded"

---

## 3. How we used it in ModelMux

### One client for the process lifetime

```python
# app/main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = httpx.Timeout(
        config.resilience["request_timeout_seconds"],   # 30s read
        connect=config.resilience["connect_timeout_seconds"],  # 5s connect
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        app.state.http = client
        app.state.providers = build_all(config, client)
        yield
```

**Every adapter shares this one pool.** In a proxy, the saved handshake can be
comparable to the model's own latency — it is the cheapest performance win in
the whole project.

Note the client is **injected** into each provider rather than created inside
it — see `10-design-patterns.md` on dependency injection.

### Why 502 and not 500

```python
except ProviderError as exc:
    return JSONResponse(status_code=502, content={...})
```

ModelMux is *definitionally* a gateway. `500` would mean our code broke; `502`
says our upstream did. When you are reading logs during an outage, that
distinction tells you whose fault it was without opening a trace.

**The mistake I made:** I first mapped `ProviderBadRequest` → `400`. That error
fires when *our* API key is dead — so a 400 told the caller *their* request was
malformed. Caught when the dead key produced
`400 {"error": "groq 401: Invalid API Key"}`, which is self-evidently incoherent.

**The principle: status codes describe whose fault it is, from the caller's
side.**

### Mapping upstream status to a retry decision

```python
# app/providers/groq.py
if status == 429:
    return ProviderRateLimited(..., retry_after_seconds=...)
if status >= 500:
    return ProviderServerError(...)     # transient, retry
return ProviderBadRequest(...)          # 400/401/404 — NEVER retry
```

This mapping *is* the retry policy. Get 401 wrong and Stage 5 retries a dead key
forever; get 500 wrong and it gives up on a provider that would have recovered.

### Never leak an upstream body

```python
detail = (detail or response.text)[:200]
```

Provider error bodies can carry internal detail and are unbounded in length.
Truncate and normalise before the string reaches a caller or a log.

---

## 4. Interview questions

**Q: What happens when you type a URL and hit enter?** *(the classic)*
DNS resolution → TCP handshake (SYN/SYN-ACK/ACK) → TLS handshake (ClientHello,
certificate validation, key exchange) → HTTP request → server processes →
response → render. For an API call, stop at the response.

**Q: Why is connection pooling important?**
Each new HTTPS connection costs a TCP handshake plus a TLS handshake — often
100–200ms before any data flows. Pooling reuses established connections so
subsequent requests skip both. In a proxy that can be as large as the upstream's
own processing time.

**Q: TCP vs UDP?**
TCP is connection-oriented, reliable, ordered, with retransmission and flow
control. UDP is fire-and-forget — no guarantees, but no handshake and lower
latency. HTTP/1 and HTTP/2 use TCP; HTTP/3 uses QUIC over UDP, largely to avoid
TCP head-of-line blocking.

**Q: 400 vs 401 vs 403 vs 422 vs 500 vs 502?**
400 malformed request; 401 not authenticated; 403 authenticated but not
permitted; 422 well-formed but semantically invalid; 500 we broke; 502 we're a
gateway and our upstream broke; 503 temporarily unavailable.

**Q: Your service calls a third-party API that returns 401. What do you return?**
**502, not 401 or 400.** The 401 is between us and the upstream — the caller
authenticated with *us* fine. Returning 4xx would blame them for our
misconfiguration. *This is a mistake I actually made and had to fix.*

**Q: Why separate connect and read timeouts?**
They bound different failures. An unreachable host should fail fast — 5s. A
server doing real work needs patience — 30s. One number forces you to choose
which failure to handle badly.

**Q: How should a client handle 429?**
Respect `Retry-After` if present, otherwise exponential backoff with **jitter**.
Jitter matters: without it, all clients retry in lockstep and produce a
thundering herd that re-triggers the rate limit.

**Q: Which errors are safe to retry?**
Timeouts, 429, and 5xx — transient, so a retry may succeed. **Never retry 4xx
client errors** (except 429) — they fail identically every time, so retrying
wastes time and adds load. *In ModelMux this is encoded in the four-type error
taxonomy: three retryable, one not.*

**Q: What is idempotency and why does it matter for retries?**
An idempotent operation has the same effect applied once or many times. GET/PUT/
DELETE are idempotent; POST generally isn't. Retrying a non-idempotent request
risks duplicate side effects — which is why payment APIs use idempotency keys.

**Q: HTTP/1.1 vs HTTP/2?**
HTTP/1.1 allows one in-flight request per connection (pipelining is broken in
practice), so browsers open several. HTTP/2 multiplexes many streams over one
connection, adds header compression and server push. It still suffers TCP
head-of-line blocking, which HTTP/3 over QUIC addresses.
