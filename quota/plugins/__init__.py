"""Optional CLI attribution plugins. Off unless configured.

A plugin spec is ``module:function``. The function is called as
``fn(task, cwd, config)`` and returns a bot id, or None to skip.

Enable one of:

* ``config/quota.json`` key ``attribution_plugins`` (a list of specs)
* ``QUOTA_ATTRIBUTION_PLUGIN=module:function`` (appended when set)

An empty list and an unset variable leave every run unattributed unless a
launch tag matches. The example plugin is ``quota.plugins.sqlite_tasks:lookup``.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3

from quota.paths import PACKAGE_ROOT


def config_path() -> str:
    raw = os.environ.get("QUOTA_CONFIG")
    if raw:
        return os.path.expanduser(raw)
    return str(PACKAGE_ROOT / "config" / "quota.json")


def load_config(path: str | None = None) -> dict:
    path = path or config_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_one(spec: str):
    mod_name, sep, func = spec.partition(":")
    if not sep or not mod_name or not func:
        raise ValueError("bad plugin spec")
    module = importlib.import_module(mod_name)
    fn = getattr(module, func)
    if not callable(fn):
        raise ValueError("plugin is not callable")
    return fn


def plugin_specs(config: dict | None = None) -> list[str]:
    if config is None:
        config = load_config()
    raw = config.get("attribution_plugins") if isinstance(config, dict) else None
    specs = [item for item in raw if isinstance(item, str) and item.strip()] if isinstance(raw, list) else []
    env = os.environ.get("QUOTA_ATTRIBUTION_PLUGIN")
    if env and env not in specs:
        specs.append(env)
    return specs


def load_plugins(config: dict | None = None) -> list:
    if config is None:
        config = load_config()
    return [_load_one(spec) for spec in plugin_specs(config)]


class Attributor:
    """Ask each plugin, in order, and keep the first bot id."""

    def __init__(self, plugins, config: dict, errors: list):
        self.plugins = list(plugins)
        self.config = config
        self.errors = errors

    def bot_for(self, task, cwd) -> str:
        if not self.plugins:
            return "unattributed"
        for fn in self.plugins:
            try:
                bot = fn(task, cwd, self.config)
            except (OSError, ValueError, sqlite3.Error) as exc:
                self.errors.append(f"plugin:{type(exc).__name__}")
                self.plugins = []
                return "unattributed"
            if isinstance(bot, str) and bot.strip() and bot != "unattributed":
                return bot
        return "unattributed"
