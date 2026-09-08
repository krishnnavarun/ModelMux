"""Provider registry.

One place that maps a config `name` onto an adapter class. `main.py` builds
whatever config.yaml asks for and never mentions a specific provider -- which
is what let Stage 2 add two adapters without touching the endpoint at all.
"""

import os
import sys

import httpx

from app.providers.anthropic import AnthropicProvider
from app.providers.base import Provider
from app.providers.google import GoogleProvider
from app.providers.groq import GroqProvider
from app.providers.mock import MockProvider

REGISTRY: dict[str, type[Provider]] = {
    "groq": GroqProvider,
    "google": GoogleProvider,
    "anthropic": AnthropicProvider,
    "mock": MockProvider,
}


def build_all(config, client: httpx.AsyncClient) -> dict[str, Provider]:
    """Instantiate every provider named anywhere in the tier config.

    Built once at startup and reused, so each adapter shares the one connection
    pool. An unknown name fails here rather than on the first request that
    happens to route to it.

    **MODELMUX_MOCK_PROVIDERS=1** replaces every adapter with the fake one.
    This is a development affordance, not a feature: it lets the full request
    path -- routing, cost arithmetic, the response contract, the database row
    -- be seen end to end without a valid API key and without spending money.

    It announces itself loudly on stderr, because a server that silently
    returns invented text would be far worse than one that fails.
    """
    if os.environ.get("MODELMUX_MOCK_PROVIDERS") == "1":
        print(
            "\n" + "!" * 70 +
            "\n!! MODELMUX_MOCK_PROVIDERS=1 -- ALL PROVIDERS ARE FAKE."
            "\n!! Responses are canned text. No model is called. Costs and"
            "\n!! token counts are simulated. Never set this in production."
            "\n" + "!" * 70 + "\n",
            file=sys.stderr,
        )
        names = {
            entry["name"]
            for tier in config.tiers.values()
            for entry in (tier.get("providers") or [])
        }
        return {
            name: MockProvider(
                text=f"[mock answer from the '{name}' adapter]",
                tokens_in=42, tokens_out=17,
            )
            for name in names
        }

    providers: dict[str, Provider] = {}

    for tier_name, tier in config.tiers.items():
        for entry in tier.get("providers") or []:
            name = entry["name"]
            if name in providers:
                continue
            if name not in REGISTRY:
                raise ValueError(
                    f"tier '{tier_name}' names unknown provider '{name}'. "
                    f"Known: {sorted(REGISTRY)}"
                )
            provider_class = REGISTRY[name]
            # MockProvider takes no client -- it makes no network calls.
            providers[name] = (
                provider_class() if provider_class is MockProvider
                else provider_class(client)
            )

    return providers
