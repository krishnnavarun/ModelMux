"""Config loading, validation, and cost arithmetic (SPEC section 9, unit)."""

import pytest
import yaml

from app import config as config_module
from app.config import Config, ConfigError


def _valid_raw(tmp_path):
    return {
        "tiers": {
            "small": {"providers": [{"name": "mock", "model": "m",
                                     "cost_per_1k_input": 0.001,
                                     "cost_per_1k_output": 0.002}]},
            "mid": {"providers": [{"name": "mock", "model": "m",
                                   "cost_per_1k_input": 0.01,
                                   "cost_per_1k_output": 0.02}]},
            "large": {"providers": [{"name": "mock", "model": "m",
                                     "cost_per_1k_input": 0.1,
                                     "cost_per_1k_output": 0.5}]},
        },
        "baseline_tier": "large",
        "classifier": {}, "cache": {}, "resilience": {}, "limits": {},
        "database": {"path": str(tmp_path / "x.db")},
    }


def _write(tmp_path, raw):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_loads_valid_config(tmp_path):
    config = config_module.load(_write(tmp_path, _valid_raw(tmp_path)))
    assert config.baseline_tier == "large"
    assert config.provider_for("small")["name"] == "mock"


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        config_module.load(tmp_path / "nope.yaml")


def test_missing_tier_raises(tmp_path):
    raw = _valid_raw(tmp_path)
    del raw["tiers"]["mid"]
    with pytest.raises(ConfigError, match="missing tier 'mid'"):
        config_module.load(_write(tmp_path, raw))


def test_missing_price_raises_at_load_not_at_request(tmp_path):
    """A missing price must fail loudly at startup.

    Otherwise it surfaces as a silently wrong cost in every database row --
    a data error rather than a config error, and far harder to notice.
    """
    raw = _valid_raw(tmp_path)
    del raw["tiers"]["large"]["providers"][0]["cost_per_1k_output"]
    with pytest.raises(ConfigError, match="cost_per_1k_output"):
        config_module.load(_write(tmp_path, raw))


def test_bad_baseline_tier_raises(tmp_path):
    raw = _valid_raw(tmp_path)
    raw["baseline_tier"] = "enormous"
    with pytest.raises(ConfigError, match="baseline_tier"):
        config_module.load(_write(tmp_path, raw))


def test_empty_provider_list_raises(tmp_path):
    raw = _valid_raw(tmp_path)
    raw["tiers"]["small"]["providers"] = []
    config = config_module.load(_write(tmp_path, raw))
    with pytest.raises(ConfigError, match="no providers"):
        config.provider_for("small")


# --- cost arithmetic -------------------------------------------------------

def test_cost_counts_input_and_output_separately(tmp_path):
    """The bug this guards against would have invalidated every savings figure.

    1000 in at 0.001 + 1000 out at 0.002 = 0.003. Pricing input alone gives
    0.001 -- and the size of that error differs per tier, which skews exactly
    the comparison the project exists to make.
    """
    config = Config(_valid_raw(tmp_path))
    assert config.cost_usd("small", 1000, 1000) == pytest.approx(0.003)
    assert config.cost_usd("small", 1000, 0) == pytest.approx(0.001)


def test_baseline_tier_is_more_expensive(tmp_path):
    """Sanity check on the config itself: routing down must actually save."""
    config = Config(_valid_raw(tmp_path))
    small = config.cost_usd("small", 1000, 1000)
    large = config.cost_usd("large", 1000, 1000)
    assert large > small
