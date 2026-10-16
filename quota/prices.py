"""Codex price table and cost estimates.

Rates are USD per 1M tokens (standard tier). Codex ``input_tokens`` already
includes cached tokens, and ``output_tokens`` already includes reasoning, so
the estimate subtracts the cache once and does not add reasoning again.
A reported ``cost_usd`` is never replaced by an estimate.
"""

from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PRICES_PATH = os.path.join(ROOT, "config", "prices.json")
RATE_KEYS = ("input", "cached_input", "cache_write", "output")


def resolve_prices_path(path=None) -> str:
    """Explicit ``--prices`` wins, then ``QUOTA_PRICES``, then ``config/prices.json``."""
    if path:
        return os.path.expanduser(path)
    env = os.environ.get("QUOTA_PRICES")
    if env:
        return os.path.expanduser(env)
    return DEFAULT_PRICES_PATH


def load_prices(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict) or not data["models"]:
        raise ValueError("prices: models")
    default = data.get("default_model")
    if not isinstance(default, str) or default not in data["models"]:
        raise ValueError("prices: default_model")
    for name, rates in data["models"].items():
        if not isinstance(rates, dict):
            raise ValueError(f"prices: {name}")
        for key in RATE_KEYS:
            value = rates.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"prices: {name}.{key}")
    return data


def _num(value) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def rates_for(prices: dict, model) -> tuple[dict, bool]:
    """(rates, used_default). Unknown or missing model uses ``default_model``."""
    models = prices["models"]
    if isinstance(model, str) and model in models:
        return models[model], False
    return models[prices["default_model"]], True


def estimate_codex_cost(prices: dict, model, *, input_tokens=0, cached_input_tokens=0,
                        cache_write_tokens=0, output_tokens=0, reasoning_tokens=0) -> tuple[float, bool]:
    """Return ``(usd, used_default)``.

    ``input_tokens`` includes ``cached_input_tokens``. ``reasoning_tokens`` is
    accepted and ignored: Codex already counts reasoning inside ``output_tokens``.
    """
    del reasoning_tokens
    rates, used_default = rates_for(prices, model)
    uncached = _num(input_tokens) - _num(cached_input_tokens)
    cached = _num(cached_input_tokens)
    usd = (
        uncached * rates["input"]
        + cached * rates["cached_input"]
        + _num(cache_write_tokens) * rates["cache_write"]
        + _num(output_tokens) * rates["output"]
    ) / 1_000_000
    return usd, used_default


def effective_cost(cost_usd, cost_usd_est=None):
    """Reported price when present, otherwise the estimate. ``0`` is a real price."""
    if cost_usd is not None:
        return cost_usd
    return cost_usd_est
