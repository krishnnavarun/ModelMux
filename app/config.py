"""Loads and validates config.yaml.

SPEC section 2 makes "configuration over code" non-negotiable, so this is the
only module allowed to know where settings come from. Everything else asks this
module.

Validation happens once, at startup. A malformed config should stop the process
immediately with a clear message -- not surface as a KeyError on the first
request, hours later.
"""

import os
from pathlib import Path

import yaml

# SPEC section 13: use pathlib, never string concatenation, because this
# project runs on Windows. __file__ is app/config.py, so two parents up is the
# project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

TIER_NAMES = ("small", "mid", "large")


class ConfigError(Exception):
    """config.yaml is missing, malformed, or internally inconsistent."""


def _resolve_db_path(raw_path: str) -> Path:
    """Turn the configured database path into a concrete absolute Path.

    Three layers, most specific first:
      1. MODELMUX_DB_PATH environment variable wins outright. Tests use it to
         redirect to a temp directory so they never touch the real database.
      2. ${VAR} placeholders in config.yaml are expanded (YAML itself does no
         expansion), which is how the default reaches LOCALAPPDATA.
      3. A relative path resolves against the project root.

    The default deliberately points outside the project, because the project
    sits in a OneDrive-synced folder and SQLite plus file sync is a corruption
    risk. See DECISIONS.md D7.
    """
    override = os.environ.get("MODELMUX_DB_PATH")
    path = Path(os.path.expandvars(override or raw_path))

    if "$" in str(path):
        raise ConfigError(
            f"database.path contains an unset variable: {path}. "
            "Set it, or use a literal path."
        )

    # SPEC section 13: pathlib, never string concatenation, because Windows.
    return path if path.is_absolute() else PROJECT_ROOT / path


class Config:
    """Parsed configuration.

    Thin wrapper over the raw dict rather than a deep tree of dataclasses.
    The dict is the shape in the YAML file, so what you read in one you can
    find in the other -- worth more here than type safety.
    """

    def __init__(self, raw: dict):
        self.raw = raw
        self.tiers = raw["tiers"]
        self.limits = raw["limits"]
        self.classifier = raw["classifier"]
        self.cache = raw["cache"]
        self.resilience = raw["resilience"]
        self.baseline_tier = raw.get("baseline_tier", "large")
        self.db_path = _resolve_db_path(raw["database"]["path"])

    def provider_for(self, tier: str) -> dict:
        """First configured provider in a tier.

        Tiers hold a *list* of providers from day one (SPEC section 7) so
        within-tier fallback needs no restructuring later. Until Stage 5 there
        is no health tracking, so "first" is the whole selection strategy.
        """
        providers = self.tiers[tier]["providers"]
        if not providers:
            raise ConfigError(f"tier '{tier}' has no providers configured")
        return providers[0]

    def cost_usd(self, tier: str, tokens_in: int, tokens_out: int) -> float:
        """Cost of a call, counting input and output separately.

        Output tokens are billed at a different -- usually much higher -- rate
        than input tokens. On the large tier here that is 5x. Pricing input
        only would understate the real spend badly.
        """
        provider = self.provider_for(tier)
        return (
            tokens_in / 1000 * provider["cost_per_1k_input"]
            + tokens_out / 1000 * provider["cost_per_1k_output"]
        )


def load(path: Path = CONFIG_PATH) -> Config:
    """Read and validate config.yaml, or raise ConfigError."""
    if not path.exists():
        raise ConfigError(f"config.yaml not found at {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must contain a mapping at the top level")

    for key in ("tiers", "limits", "classifier", "cache", "resilience", "database"):
        if key not in raw:
            raise ConfigError(f"config.yaml is missing the '{key}' section")

    for tier in TIER_NAMES:
        if tier not in raw["tiers"]:
            raise ConfigError(f"config.yaml is missing tier '{tier}'")
        providers = raw["tiers"][tier].get("providers") or []
        for provider in providers:
            # Catching a missing price here, at startup, is the difference
            # between a clear error and a silently wrong cost figure in every
            # row of the database.
            for field in ("name", "model", "cost_per_1k_input", "cost_per_1k_output"):
                if field not in provider:
                    raise ConfigError(
                        f"tier '{tier}' provider "
                        f"'{provider.get('name', '?')}' is missing '{field}'"
                    )

    baseline = raw.get("baseline_tier", "large")
    if baseline not in TIER_NAMES:
        raise ConfigError(f"baseline_tier must be one of {TIER_NAMES}, got '{baseline}'")

    return Config(raw)
