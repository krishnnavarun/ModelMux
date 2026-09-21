# Redis & Semantic Caching

---

## 1. What it is

**Redis** is an in-memory key-value data store. Everything lives in RAM, which
makes it very fast and means it is not where you keep data you cannot lose.

**Caching** is storing the result of expensive work so the next identical
request skips it.

**Semantic caching** relaxes "identical" to "means the same thing" — matching by
embedding similarity rather than exact string equality.

---

## 2. How it works

### Redis data types we use

| Type | Command | Used for |
|---|---|---|
| String | `SET` / `GET` / `SETEX` | the embedding bytes, the answer JSON |
| Sorted set | `ZADD` / `ZRANGE` / `ZREM` | the index, scored by timestamp |
| Counter | `INCR` | a version number for cache invalidation |

A **sorted set** keeps members ordered by a numeric score. `ZRANGE key 0 N`
returns the N lowest-scored members in O(log n) — which is exactly "the N oldest
entries", the question eviction needs to answer.

### TTL

`SETEX key seconds value` sets a value that deletes itself. Redis handles
expiry; you never write a cleanup job.

**The gotcha:** if the index and the values expire independently, the index
keeps ids whose values are gone. You either sweep them in the background or
tolerate the holes and clean them lazily on read.

### Pipelining

Each Redis command is a network round trip. A pipeline sends several at once:

```python
pipe = redis.pipeline()
pipe.setex(vec_key, ttl, vector_bytes)
pipe.setex(ans_key, ttl, payload)
pipe.zadd(index_key, {entry_id: now})
await pipe.execute()          # one round trip, not three
```

### Eviction policies

| Policy | Evicts | Cost |
|---|---|---|
| **FIFO** | oldest inserted | free — insertion order is already known |
| **LRU** | least recently used | a write on every read, to record the touch |
| **LFU** | least frequently used | a counter per entry |

LRU usually gets a better hit rate, and costs a write on the read path.

### Caching vocabulary

- **Hit / miss** — found / not found
- **Hit rate** — hits ÷ total lookups. The headline number.
- **Cold start** — an empty cache; everything misses
- **Invalidation** — removing entries that are no longer correct
- **Stampede** — many concurrent misses for the same key all doing the work

---

## 3. How we used it in ModelMux

### The key layout

```
mm:vec:{id}   the embedding, float32 bytes
mm:ans:{id}   JSON: prompt, response, tier, model, tokens_out, created_at
mm:index      SORTED SET of live ids, scored by creation time
mm:version    counter, incremented on every write
```

`float32` rather than `float64` halves the bytes for precision no cosine
threshold could distinguish.

### The finding that shaped everything: no threshold is safe

The measurement (`eval/tune_cache_threshold.py`) against 15 equivalent and 20
dangerous prompt pairs:

| threshold | hit rate | false hits |
|---|---|---|
| 0.90 | 67% | 2 |
| 0.92 | 47% | 1 |
| **0.98** | **7%** | **1** |

Even at 0.98 — where the cache has stopped being worth having — this pair still
hits:

```
0.9903  "Convert 32 Fahrenheit to Celsius."
     vs "Convert 32 Celsius to Fahrenheit."
```

Opposite operations. Answers 0 and 89.6.

> **Embeddings encode topic and vocabulary, not logical direction.** Negation,
> antonyms and argument order are exactly what they represent worst, because
> the two sentences share nearly every token. This is not a tuning problem, and
> no single cosine threshold can fix it.

### The fix: a second, lexical gate

`cache.is_inverted()` runs *after* the similarity check:

1. **Antonym swap** — one prompt says "encrypt", the other "decrypt".
2. **Exact positional exchange** — two words land in each other's slots while
   the rest of the sentence stays put.

Check 2 is what catches Fahrenheit/Celsius, where an antonym list never could:
both prompts contain *both* units, so only ordering distinguishes them.

**The first version of check 2 was too blunt.** "Did any pair change relative
order" also blocked:

```
"How do I reverse a string in Python?"
"In Python, how can I reverse a string?"
```

where "python" merely migrates to the front. That is a rephrasing — a
legitimate hit. Requiring an **exact** exchange separated the two cleanly.

With the guard, thresholds from 0.84 upward have zero false hits.

### Choosing 0.88 rather than the lowest safe 0.84

The highest dangerous pair surviving the guard scores 0.8156.

- 0.84 → margin +0.024, hit rate 80%
- **0.88 → margin +0.064, hit rate 67%**

Taking the lowest value with zero false hits *on my own 20 pairs* would be
fitting a constant to the test set — the same mistake as tuning classifier
weights on the evaluation set. 0.88 keeps margin for pairs I did not imagine.

**Measured end to end: 80% hit rate, 0% false hits.**

### The scan that did not scale

SPEC said a linear scan is fine at 10k entries, and to say so if it exceeded
30ms. It did:

| entries | total | embed | redis+matmul |
|---|---|---|---|
| 200 | 14.2ms | 8.9 | 5.4 |
| **1000** | **74.4ms** | 9.4 | **64.9** |

At the 10,000 ceiling that extrapolates to ~650ms.

**The cost was the round trip, not the maths.** 1000 vectors is 1.5MB of
float32 over the wire; the matmul is microseconds even at 10k.

**Fix: mirror the vectors in process, refetch only when `mm:version` changes.**

| entries | before | after |
|---|---|---|
| 1,000 | 74.4ms | 10.0ms |
| 10,000 | ~650ms | **11.2ms** |

Flat with size, because the remaining cost is the embedding plus a 1ms version
check.

**The trade-off, named:** across worker processes, one process's write is
invisible to another until its next version check. That costs **missed hits,
never wrong answers** — a stale mirror can only fail to find something, and
every hit is still verified against the live answer in Redis.

### FIFO, not LRU

Eviction drops the oldest inserted. LRU would need a write on every read — an
extra round trip on the fast path. If the hit rate later turns out to be
dominated by a small hot set, LRU earns its cost; that is a measurement, not a
guess.

### Redis on Windows: WSL2 localhost forwarding is unreliable

Redis runs inside WSL2, which is supposed to forward Windows `localhost:6379`
into the VM. It does — **intermittently**. Measured: five consecutive
ConnectionRefused, then five successes immediately after any `wsl` command woke
the instance, while `wsl -e redis-cli ping` answered PONG from inside
throughout.

Two mitigations:

1. Redis binds `0.0.0.0` inside WSL, so its own IP is reachable
2. `cache.init()` falls back to discovering that IP via `wsl -e hostname -I`

The IP changes on every WSL restart, so it must be discovered, never hardcoded.
**The fallback has fired in real runs**, not just in theory.

---

## 4. Interview questions

**Q: When would you use Redis over a relational database?**
When you need low-latency access to data you can afford to lose or rebuild:
caches, sessions, rate-limit counters, queues, leaderboards. Redis is in-memory
first, with optional persistence. It is not a system of record.

**Q: What Redis data structure would you use for "the N oldest entries"?**
A sorted set scored by timestamp — `ZRANGE key 0 N` answers it in O(log n). An
unordered set would require fetching everything and sorting client-side.

**Q: How do you handle cache invalidation?**
TTL for time-bounded staleness, explicit deletion on write for correctness, and
a version/generation counter when many entries invalidate at once. *I use TTL
plus a version counter — the counter is what tells each process its local mirror
is stale.*

**Q: What is a cache stampede and how do you prevent it?**
Many concurrent misses for the same key all doing the expensive work at once,
often right after a popular entry expires. Prevented with a lock so one caller
recomputes while others wait, or by refreshing slightly before expiry.

**Q: Explain semantic caching and its main risk.**
Match by embedding similarity rather than exact string equality, so paraphrases
hit. The main risk is **serving a wrong answer**: two prompts can be similar
and mean different things. Worse, it is a cross-user leak — one caller's answer
reaching another. It needs a conservative threshold, a bypass flag, and
documentation as a known limitation.

**Q: How do you pick a similarity threshold?**
By measurement, on labelled pairs: ones that should match and ones that must
not. Sweep, and pick from the safe region with margin. *In my case the
distributions overlapped so badly that no threshold was safe at all — the top
dangerous pair scored 0.99 — which is why a second lexical check was needed.*

**Q: Your cache serves a wrong answer. How do you find out before a user does?**
A test that demonstrates it. *SPEC required one: at a deliberately loose 0.75,
asking about the tallest mountain in Asia returns Kilimanjaro. It makes the
risk executable rather than a paragraph nobody reads.*

**Q: LRU vs FIFO eviction?**
LRU keeps what is actually being used and usually wins on hit rate, at the cost
of a write on every read to record the touch. FIFO is free because insertion
order is already known. Choose FIFO until you have evidence of a hot set.

**Q: Your linear scan is too slow. You are not allowed a vector database. What
do you do?**
Find out where the time goes first. *Mine was the network round trip — 1.5MB of
vectors per lookup — not the matmul, which was microseconds. Mirroring the
vectors in process and invalidating on a version counter took 10k entries from
~650ms to 11ms, with no new dependency and the same linear scan.*

**Q: What does caching cost you, beyond memory?**
Staleness, invalidation complexity, and a second source of truth that can
disagree with the first. For semantic caching specifically it also costs
**correctness risk** — a class of bug that returns a confident wrong answer
rather than an error, which is much harder to notice.
