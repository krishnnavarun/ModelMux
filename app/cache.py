"""Semantic cache over Redis (SPEC section 8.5).

An exact-match cache only hits on identical strings. A **semantic** cache hits
when two prompts *mean* the same thing:

    cached:   "What is the capital of Japan?"
    incoming: "Whats the capital city of japan"     -> cosine 0.94, HIT

A hit costs no provider call and no money.

-----------------------------------------------------------------------------
!! THIS IS THE MOST DANGEROUS COMPONENT IN THE PROJECT !!
-----------------------------------------------------------------------------
SPEC section 10 is blunt: "Cache leakage is the real risk in this
architecture." The cache stores prompts and answers. If the similarity
threshold is too loose, one caller's answer is served to another caller whose
prompt merely *resembles* theirs.

That is not a bug to be fixed later -- it is inherent to shared semantic
caching. The mitigations are:

  1. A conservative threshold, justified by measurement (DECISIONS.md D16)
  2. A `bypass_cache` flag on the request, for callers who cannot accept it
  3. Documenting it in the README as a known limitation
  4. A test that DEMONSTRATES a wrong answer at a loose threshold, so the risk
     is visible in the suite rather than described in prose

-----------------------------------------------------------------------------
KEY LAYOUT
-----------------------------------------------------------------------------
    mm:vec:{id}   the embedding, float32 bytes
    mm:ans:{id}   JSON: prompt_hash, response, tier, model, tokens_out, created_at
    mm:index      SORTED SET of live ids, scored by creation timestamp

`mm:index` is a sorted set rather than the plain set SPEC suggests, because
eviction needs "the oldest N". A sorted set answers that in O(log n) via
ZRANGE; an unordered set would need every entry fetched and compared.

-----------------------------------------------------------------------------
WHY A LINEAR SCAN, AND NOT A VECTOR DATABASE
-----------------------------------------------------------------------------
Lookup fetches every stored vector and does one matrix multiply. At the
configured 10,000-entry ceiling that is a 10000x384 matmul -- microseconds of
compute. The cost is the Redis round trip, not the maths.

SPEC section 8.5: "A linear scan is fine at 10k entries and is the honest
simple choice. Do not add a vector database. If scan time exceeds 30ms, say so
and we'll discuss." Measured numbers are in DECISIONS.md D16.
"""

import hashlib
import json
import sys
import time
from dataclasses import dataclass

import numpy as np

from app.classifier import embedding

VEC_PREFIX = "mm:vec:"
ANS_PREFIX = "mm:ans:"
INDEX_KEY = "mm:index"

# float32, not float64: half the bytes over the wire for precision we do not
# need. Cosine similarity to 7 significant figures is far beyond what a 0.92
# threshold can distinguish.
DTYPE = np.float32

# How many above-threshold candidates to check before giving up. The guard
# can reject the closest match, and the runner-up may still be valid.
MAX_CANDIDATES = 3

# Two prompts whose lengths differ by more than this ratio are never a match,
# whatever their similarity. See the truncation collision guard in lookup().
LENGTH_RATIO_MIN = 0.5

VERSION_KEY = "mm:version"

_redis = None
_available = False
_config = None

# In-process mirror of the vectors. See _refresh_mirror().
_mirror_ids: list[str] = []
_mirror_matrix = None      # numpy (n, 384), or None
_mirror_version: int = -1


@dataclass
class CacheHit:
    """A served-from-cache answer, plus why we believed it matched."""

    response: str
    tier: str
    model: str
    tokens_out: int
    similarity: float
    cached_at: str
    entry_id: str


def _wsl_redis_url() -> str | None:
    """Ask WSL for its own IP and build a Redis URL from it.

    **Why this exists.** Redis runs inside WSL2. WSL2 is supposed to forward
    Windows `localhost:6379` into the VM, and it does -- intermittently. When
    the WSL instance idles, the forwarding drops, and a Windows process gets
    ConnectionRefused while `wsl -e redis-cli ping` still answers PONG from
    inside. Measured here: five consecutive attempts refused, then all five
    succeeding immediately after any `wsl` command woke the instance.

    Connecting to WSL's own IP bypasses the forwarding entirely. The address
    changes every time WSL restarts, so it must be discovered, never
    hardcoded.

    Requires Redis to listen beyond WSL's loopback -- see DECISIONS.md D17.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["wsl", "-e", "hostname", "-I"],
            capture_output=True, text=True, timeout=15,
        )
        ip = out.stdout.strip().split()[0] if out.stdout.strip() else None
        return f"redis://{ip}:6379" if ip else None
    except Exception:  # noqa: BLE001 -- discovery is best-effort
        return None


async def _try_connect(url: str):
    """Return a live client for `url`, or None."""
    import redis.asyncio as aioredis

    client = aioredis.from_url(url, decode_responses=False,
                               socket_connect_timeout=5)
    try:
        await client.ping()
        return client
    except Exception:  # noqa: BLE001
        await client.aclose()
        return None


async def init(config) -> None:
    """Connect to Redis. Call once, at startup.

    Tries, in order: MODELMUX_REDIS_URL, the configured URL, then WSL's
    discovered IP. The fallback exists because WSL2 localhost forwarding is
    unreliable -- see _wsl_redis_url().

    A failure here is NOT fatal. The cache is an optimisation; the service must
    still answer requests without it. Every miss simply becomes a provider
    call, which is what a cold cache does anyway.
    """
    global _redis, _available, _config
    _config = config

    # Idempotent, same reasoning as embedding.init(): the lifespan runs per
    # TestClient, and reconnecting Redis for every test is pure overhead.
    if _redis is not None and _available:
        return

    import os

    # Kill switch, same pattern as MODELMUX_DB_PATH. The API tests use it: the
    # mock provider returns identical text for every prompt, so a live cache
    # turns the second request in a test run into a HIT and `mock.calls` stops
    # counting provider calls. That is shared state leaking between tests --
    # the same failure the Day 1 provider fixture had, in a new place.
    if os.environ.get("MODELMUX_CACHE_DISABLED") == "1":
        print("[cache] disabled via MODELMUX_CACHE_DISABLED", file=sys.stderr)
        _available = False
        return

    if not config.cache.get("enabled", True):
        print("[cache] disabled in config", file=sys.stderr)
        _available = False
        return

    candidates = [
        os.environ.get("MODELMUX_REDIS_URL"),
        config.cache.get("url", "redis://localhost:6379"),
    ]

    for url in [c for c in candidates if c]:
        client = await _try_connect(url)
        if client is not None:
            _redis, _available = client, True
            size = await _redis.zcard(INDEX_KEY)
            print(f"[cache] connected to {url}, {size} entries", file=sys.stderr)
            return

    wsl_url = _wsl_redis_url()
    if wsl_url:
        client = await _try_connect(wsl_url)
        if client is not None:
            _redis, _available = client, True
            size = await _redis.zcard(INDEX_KEY)
            print(f"[cache] connected via WSL IP {wsl_url}, {size} entries",
                  file=sys.stderr)
            return

    _redis = None
    _available = False
    print(
        "[cache] UNAVAILABLE -- every request will call a provider.\n"
        "        Redis reachable inside WSL but not from Windows.\n"
        "        Fix: wsl -e sudo sed -i 's/^bind 127.0.0.1/bind 0.0.0.0/' "
        "/etc/redis/redis.conf\n"
        "             wsl -e sudo sed -i 's/^protected-mode yes/protected-mode no/' "
        "/etc/redis/redis.conf\n"
        "             wsl -e sudo service redis-server restart",
        file=sys.stderr,
    )


async def close() -> None:
    if _redis is not None:
        await _redis.aclose()


def available() -> bool:
    """Whether the cache can actually be used.

    Exposed so main.py reports `cache_lookup_ms: None` rather than 0 when the
    cache is down -- None means "not attempted", 0 would claim we looked.
    """
    return _available and embedding.available()


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# THE INVERSION GUARD
#
# Measured finding (eval/tune_cache_threshold.py, DECISIONS.md D16): NO
# similarity threshold is safe. These two score 0.9903 --
#
#     "Convert 32 Fahrenheit to Celsius."
#     "Convert 32 Celsius to Fahrenheit."
#
# -- and have completely different correct answers (0 vs 89.6). At 0.98 the hit
# rate has already collapsed to 7% and that pair STILL hits.
#
# The cause is structural, not a tuning problem: **embeddings encode topic and
# vocabulary, not logical direction.** Negation, antonyms and argument order
# are precisely what they represent worst, because the two sentences share
# nearly every token.
#
# So similarity is necessary but not sufficient. This is a second, cheap,
# purely lexical gate that catches the two failure shapes actually observed.
# It is a PATCH ON A FUNDAMENTAL LIMITATION, not a solution -- see the caveats
# in D16.
# ---------------------------------------------------------------------------

# Terms whose swap reverses meaning while barely moving the embedding.
ANTONYM_PAIRS = [
    ("enable", "disable"), ("encrypt", "decrypt"), ("start", "stop"),
    ("install", "uninstall"), ("commit", "revert"), ("create", "drop"),
    ("add", "remove"), ("open", "close"), ("boiling", "freezing"),
    ("increase", "decrease"), ("lock", "unlock"), ("include", "exclude"),
    ("allow", "deny"), ("connect", "disconnect"), ("attach", "detach"),
    ("import", "export"), ("compress", "decompress"), ("show", "hide"),
    ("adds", "multiplies"), ("first", "second"), ("one", "two"),
    ("before", "after"), ("maximum", "minimum"), ("push", "pull"),
]

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "do", "does", "did", "to",
    "of", "in", "on", "for", "and", "or", "how", "what", "why", "when",
    "where", "who", "i", "me", "my", "can", "you", "it", "this", "that",
}


def _tokens(text: str) -> list[str]:
    return [w.strip(".,?!:;\"'()") .lower() for w in text.split()]


def is_inverted(a: str, b: str) -> bool:
    """True if two prompts look like inversions of each other.

    Two checks, one per observed failure shape:

    1. ANTONYM SWAP -- one prompt says "enable", the other "disable", and
       neither says both. "How do I encrypt a file?" vs "How do I decrypt a
       file?" scored 0.8352.

    2. ARGUMENT ORDER REVERSAL -- both prompts contain the same two
       distinctive words but in opposite order. This is what catches the
       Fahrenheit/Celsius case, where an antonym list never could: both
       prompts contain BOTH units, so only the ordering distinguishes them.
    """
    ta, tb = _tokens(a), _tokens(b)
    sa, sb = set(ta), set(tb)

    # 1. antonym swap
    for left, right in ANTONYM_PAIRS:
        if (left in sa and right in sb and right not in sa and left not in sb):
            return True
        if (right in sa and left in sb and left not in sa and right not in sb):
            return True

    # 2. argument order reversal
    #
    # Only applied when the two prompts use the SAME content words. That
    # restriction is load-bearing: without it the check fires on ordinary
    # rephrasing, where a clause simply moves --
    #
    #   "How do I reverse a string in Python?"
    #   "In Python, how can I reverse a string?"
    #
    # -- which is a legitimate cache hit, and was a false positive until this
    # condition was added. An inversion keeps the vocabulary and swaps the
    # order; a rephrasing changes the vocabulary too.
    content_a = {w for w in sa if w and w not in _STOPWORDS and len(w) > 1}
    content_b = {w for w in sb if w and w not in _STOPWORDS and len(w) > 1}
    if content_a != content_b or len(content_a) < 2:
        return False

    # Look for an EXACT POSITIONAL EXCHANGE: two words that swap places with
    # each other while the rest of the sentence stays put.
    #
    #   "Convert 32 Fahrenheit to Celsius."   fahrenheit@2  celsius@4
    #   "Convert 32 Celsius to Fahrenheit."   celsius@2     fahrenheit@4
    #                                         -> exact exchange, INVERTED
    #
    # A weaker "did any pair change relative order" test was tried first and
    # was too blunt: it also fired on
    #
    #   "How do I reverse a string in Python?"
    #   "In Python, how can I reverse a string?"
    #
    # where "python" merely migrates to the front. That is a rephrasing, not an
    # inversion, and blocking it costs a legitimate cache hit. Requiring the
    # two words to land in each other's exact slots separates the two cleanly.
    shared = sorted(content_a)
    pos_a = {w: ta.index(w) for w in shared}
    pos_b = {w: tb.index(w) for w in shared}

    for i, w1 in enumerate(shared):
        for w2 in shared[i + 1:]:
            if pos_a[w1] == pos_a[w2]:
                continue
            if pos_a[w1] == pos_b[w2] and pos_a[w2] == pos_b[w1]:
                return True

    return False


async def lookup(prompt: str) -> tuple[CacheHit | None, float]:
    """Find a semantically equivalent cached answer.

    Returns (hit or None, lookup_ms). The timing is returned rather than
    logged because it goes into the response and the database row -- SPEC's
    30ms budget is only meaningful if it is measured on every request.
    """
    started = time.perf_counter()

    if not available():
        return None, 0.0

    query = embedding.encode(prompt)
    if query is None:
        return None, (time.perf_counter() - started) * 1000

    try:
        live_ids, matrix = await _refresh_mirror()
        if matrix is None or not live_ids:
            return None, (time.perf_counter() - started) * 1000

        # Both sides are unit vectors, so the dot product IS cosine similarity.
        similarities = matrix @ query

        threshold = _config.cache["similarity_threshold"]

        # Candidates above the threshold, most similar first. More than one is
        # considered because the guard may reject the closest match -- and the
        # runner-up can still be a legitimate hit. Capped at 3: beyond that the
        # extra Redis round trips cost more than the marginal hit is worth.
        ranked = np.argsort(similarities)[::-1][:MAX_CANDIDATES]

        answer = None
        best_score = 0.0
        entry_id = ""
        for idx in ranked:
            score = float(similarities[idx])
            if score < threshold:
                break

            candidate_id = live_ids[int(idx)].decode()
            raw_answer = await _redis.get(ANS_PREFIX + candidate_id)
            if raw_answer is None:
                # Expired between the index read and now. A miss for this
                # candidate, not an error.
                continue

            candidate = json.loads(raw_answer)

            # THE SECOND GATE. Similarity alone is provably insufficient:
            # "Convert 32 Fahrenheit to Celsius" and "Convert 32 Celsius to
            # Fahrenheit" score 0.9903 and have different answers. See
            # DECISIONS.md D16 and is_inverted() above.
            # Truncation collision guard. Vectors are built from the first
            # EMBED_MAX_CHARS only, so two prompts identical in their opening
            # and divergent afterwards embed the same. Comparing full lengths
            # catches that -- cheap, and the alternative is a wrong answer.
            cached_prompt = candidate.get("prompt", "")
            longer = max(len(prompt), len(cached_prompt)) or 1
            shorter = min(len(prompt), len(cached_prompt))
            if shorter / longer < LENGTH_RATIO_MIN:
                print(f"[cache] length guard blocked a {score:.4f} match "
                      f"({len(prompt)} vs {len(cached_prompt)} chars)",
                      file=sys.stderr)
                continue

            if is_inverted(prompt, cached_prompt):
                print(f"[cache] inversion guard blocked a {score:.4f} match",
                      file=sys.stderr)
                continue

            answer, best_score, entry_id = candidate, score, candidate_id
            break

        if answer is None:
            return None, (time.perf_counter() - started) * 1000

        hit = CacheHit(
            response=answer["response"],
            tier=answer["tier"],
            model=answer["model"],
            tokens_out=answer.get("tokens_out", 0),
            similarity=round(best_score, 4),
            cached_at=answer.get("created_at", ""),
            entry_id=entry_id,
        )
        return hit, (time.perf_counter() - started) * 1000

    except Exception as exc:  # noqa: BLE001
        # A broken cache must never fail a request. Report and miss.
        print(f"[cache] lookup failed: {exc}", file=sys.stderr)
        return None, (time.perf_counter() - started) * 1000


async def _refresh_mirror():
    """Return (ids, matrix), refetching from Redis only when it has changed.

    -----------------------------------------------------------------------
    WHY THIS EXISTS -- a measured problem, not a premature optimisation
    -----------------------------------------------------------------------
    The first version fetched every vector from Redis on every lookup. Measured:

        entries   total     embed   redis+matmul
            50   20.7ms    12.1ms         8.6ms
           200   14.2ms     8.9ms         5.4ms
          1000   74.4ms     9.4ms        64.9ms   <-- budget is 30ms

    At the configured 10,000-entry ceiling that extrapolates to ~650ms. The
    cost is the ROUND TRIP -- 1000 vectors is 1.5MB of float32 over the wire --
    not the matmul, which is microseconds even at 10k.

    So the vectors are mirrored in this process and refetched only when Redis
    says they changed. `mm:version` is incremented on every write; a lookup
    reads that one integer (one round trip, ~1ms) and reuses the local matrix
    when it matches.

    THE TRADE-OFF, stated plainly: with several worker processes, one process's
    write is invisible to another until the next version check. That costs
    MISSED HITS, never wrong answers -- a stale mirror can only fail to find
    something, and every hit is still verified against the live answer in
    Redis. Memory is 10k x 384 x 4 bytes = ~15MB.

    SPEC 8.5 forbids a vector database, and this is not one: it is the same
    linear scan, over a local copy.
    """
    global _mirror_ids, _mirror_matrix, _mirror_version

    version = int(await _redis.get(VERSION_KEY) or 0)
    if version == _mirror_version and _mirror_matrix is not None:
        return _mirror_ids, _mirror_matrix

    ids = await _redis.zrange(INDEX_KEY, 0, -1)
    if not ids:
        _mirror_ids, _mirror_matrix, _mirror_version = [], None, version
        return [], None

    raw_vectors = await _redis.mget([VEC_PREFIX + i.decode() for i in ids])

    # TTL expiry removes mm:vec but leaves its id in the index. Filter the
    # holes here and clean them up lazily -- a background sweeper would be more
    # code for a problem that resolves itself on the next lookup.
    live_ids, live_vectors, stale = [], [], []
    for entry_id, raw in zip(ids, raw_vectors):
        if raw is None:
            stale.append(entry_id)
        else:
            live_ids.append(entry_id)
            live_vectors.append(np.frombuffer(raw, dtype=DTYPE))

    if stale:
        await _redis.zrem(INDEX_KEY, *stale)

    _mirror_ids = live_ids
    _mirror_matrix = np.vstack(live_vectors) if live_vectors else None
    _mirror_version = version
    return _mirror_ids, _mirror_matrix


async def store(prompt: str, response: str, tier: str, model: str,
                tokens_out: int) -> None:
    """Cache one successful, non-cached response. Never raises."""
    if not available():
        return

    try:
        vector = embedding.encode(prompt)
        if vector is None:
            return

        entry_id = _prompt_hash(prompt)[:32]
        created_at = time.time()
        ttl = int(_config.cache.get("ttl_seconds", 86400))

        payload = json.dumps({
            # The prompt TEXT is stored here, unlike the request log which
            # keeps only an 80-char preview plus a hash (SPEC 10).
            #
            # Deliberate, and the reasoning matters: the inversion guard has to
            # compare the incoming prompt against the cached one, and a hash
            # cannot be compared for word order. More to the point, this entry
            # ALREADY stores the full answer -- withholding the question while
            # keeping the answer buys no privacy at all. Unlike the request
            # log, cache entries expire (TTL) and are evicted (FIFO), so this
            # is not a permanent transcript. See DECISIONS.md D16.
            "prompt": prompt,
            "prompt_hash": _prompt_hash(prompt),
            "response": response,
            "tier": tier,
            "model": model,
            "tokens_out": tokens_out,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime(created_at)),
        })

        # Pipelined: four commands, one round trip.
        pipe = _redis.pipeline()
        pipe.setex(VEC_PREFIX + entry_id, ttl,
                   vector.astype(DTYPE).tobytes())
        pipe.setex(ANS_PREFIX + entry_id, ttl, payload)
        pipe.zadd(INDEX_KEY, {entry_id: created_at})
        # Bump the version so every process (including this one) knows the
        # mirror is stale.
        pipe.incr(VERSION_KEY)
        await pipe.execute()

        await _evict_if_over_capacity()

    except Exception as exc:  # noqa: BLE001
        print(f"[cache] store failed: {exc}", file=sys.stderr)


async def _evict_if_over_capacity() -> None:
    """Drop the oldest entries once past `max_entries`.

    Oldest-first (FIFO), not least-recently-used. LRU would need a touch on
    every hit, which is an extra write on the fast path. FIFO is the honest
    simple choice at this scale; if the hit rate turns out to be dominated by a
    small hot set, LRU becomes worth its cost and that is a measurement, not a
    guess.
    """
    max_entries = int(_config.cache.get("max_entries", 10_000))
    size = await _redis.zcard(INDEX_KEY)
    if size <= max_entries:
        return

    overflow = size - max_entries
    oldest = await _redis.zrange(INDEX_KEY, 0, overflow - 1)
    if not oldest:
        return

    pipe = _redis.pipeline()
    for entry_id in oldest:
        key = entry_id.decode()
        pipe.delete(VEC_PREFIX + key, ANS_PREFIX + key)
    pipe.zrem(INDEX_KEY, *oldest)
    pipe.incr(VERSION_KEY)
    await pipe.execute()


async def size() -> int:
    """Number of live entries. Used by tests and the eval harness."""
    if not available():
        return 0
    return int(await _redis.zcard(INDEX_KEY))


async def clear() -> None:
    """Remove every entry. Tests only -- never called by the service."""
    if not _available:
        return
    ids = await _redis.zrange(INDEX_KEY, 0, -1)
    pipe = _redis.pipeline()
    for entry_id in ids:
        key = entry_id.decode()
        pipe.delete(VEC_PREFIX + key, ANS_PREFIX + key)
    pipe.delete(INDEX_KEY)
    pipe.incr(VERSION_KEY)
    await pipe.execute()
