"""Per-household configuration: the things Grocy cannot tell you.

Locations, categories and units are read live from the instance, so they need
no configuration and cannot drift. What Grocy does not know is the *advice* —
what each location is for, how long things keep when the package date is
missing, how products are named. That advice is what makes an agent's guesses
good, and it is different in every kitchen, so it lives in a config file rather
than in the code.

Everything here is optional. With no config file the server still works; it
just describes the instance without opinions about it.

    GROCY_MCP_CONFIG=/path/to/pantry.toml

See examples/pantry.toml for an annotated file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ENV_VAR = "GROCY_MCP_CONFIG"


@dataclass
class PantryConfig:
    """Advice about one pantry, layered on top of what Grocy reports."""

    # A sentence describing the household, shown to the agent up front.
    summary: str = ""

    # Location name -> what it is for. Names, not ids: ids change in a restore.
    location_notes: dict[str, str] = field(default_factory=dict)

    # Preferred display order. Anything not listed sorts after, alphabetically
    # for categories and by id for locations.
    category_order: list[str] = field(default_factory=list)
    location_order: list[str] = field(default_factory=list)

    # Free prose appended to get_conventions, in this order.
    unit_guidance: str = ""
    expiry_guidance: str = ""
    naming_guidance: str = ""

    # Days ahead that expiring_soon looks by default.
    soon_days: int = 7

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "PantryConfig":
        """Read a config file, or return the empty (advice-free) default.

        `path` wins over $GROCY_MCP_CONFIG. A path that is set but missing is
        an error — silently ignoring it would leave the agent quietly working
        without the household's conventions, which looks like the server
        working fine right up until it files something on the wrong shelf.
        """
        raw = path or os.environ.get(ENV_VAR)
        if not raw:
            return cls()
        p = Path(raw).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"{ENV_VAR} points at {p}, which does not exist.")
        with p.open("rb") as fh:
            data = tomllib.load(fh)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "PantryConfig":
        pantry = data.get("pantry", data) or {}
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(pantry) - known
        if unknown:
            raise ValueError(
                "Unknown config keys: "
                + ", ".join(sorted(unknown))
                + ". Known keys: "
                + ", ".join(sorted(known))
                + "."
            )
        kwargs = {k: v for k, v in pantry.items() if k in known}
        for key in ("location_notes",):
            if key in kwargs and not isinstance(kwargs[key], dict):
                raise ValueError(f"{key} must be a table of name = \"note\" pairs.")
        for key in ("category_order", "location_order"):
            if key in kwargs and not isinstance(kwargs[key], list):
                raise ValueError(f"{key} must be a list of names.")
        return cls(**kwargs)


_config: PantryConfig | None = None


def configure(config: PantryConfig) -> PantryConfig:
    global _config
    _config = config
    return _config


def get_config() -> PantryConfig:
    global _config
    if _config is None:
        _config = PantryConfig.load()
    return _config
