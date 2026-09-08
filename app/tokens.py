"""Token counting.

SPEC section 3 specifies tiktoken. Two things about it matter for us:

1. **It is OpenAI's tokenizer.** Groq, Gemini and Anthropic all tokenize
   differently, so this is an *approximation* for every provider we actually
   call. That is fine for its two jobs -- pre-call size estimation and a
   routing signal -- because both only need the relative ordering of prompts to
   be right. It is NOT fine for billing: real token counts come back from the
   provider in `usage`, and those are what the database records.

2. **The encoder must be loaded once, at startup.** First load downloads a BPE
   file (~6.6s here) and the first encode costs ~48ms. Measured steady state
   afterwards is 0.005-0.05ms, against a 20ms classification budget. Loading it
   per request would blow that budget by itself.
"""

import sys

# cl100k_base is GPT-4's encoding. Any consistent tokenizer works for our
# purposes; what matters is that the same prompt always yields the same count.
ENCODING_NAME = "cl100k_base"

# Rough chars-per-token for English prose, used only if tiktoken is unavailable.
FALLBACK_CHARS_PER_TOKEN = 4

_encoder = None
_using_fallback = False


def init() -> None:
    """Load and warm the encoder. Call once, at startup.

    A failure here is not fatal. tiktoken fetches its encoding file over the
    network on first use, so a machine that is offline the first time would
    otherwise be unable to start the service at all. Routing degrades to a
    character-based estimate instead, and says so loudly.
    """
    global _encoder, _using_fallback
    try:
        import tiktoken

        _encoder = tiktoken.get_encoding(ENCODING_NAME)
        # Warm it: the first encode is ~1000x slower than the rest.
        _encoder.encode("warm")
        _using_fallback = False
    except Exception as exc:  # noqa: BLE001 -- must not prevent startup
        _encoder = None
        _using_fallback = True
        print(
            f"[tokens] tiktoken unavailable ({exc}); "
            f"falling back to chars/{FALLBACK_CHARS_PER_TOKEN} estimate",
            file=sys.stderr,
        )


def count(text: str) -> int:
    """Approximate token count for `text`."""
    if _encoder is not None:
        return len(_encoder.encode(text))
    return max(1, len(text) // FALLBACK_CHARS_PER_TOKEN)


def using_fallback() -> bool:
    """True if counts are character estimates rather than real tokenization.

    Exposed so the eval harness can say so in its output -- a routing accuracy
    figure measured on estimated tokens is not the same claim as one measured
    on real ones.
    """
    return _using_fallback
