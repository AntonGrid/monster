"""The bridge to ``ENRG-AI``: one copy of the arithmetic, not two.

Why this file exists
====================

The Kuramoto algebra and the resolution gate are already written, tested and
checked against their references inside ``ENRG-AI``: ``oscillator.py`` matches
``entrain/julia/kuramoto/basic.jl`` to the last bit, and ``power.py`` computes
the smallest edge a sample can resolve.  Copying either into this package would
create a second source of truth — the kind that drifts quietly until two files
disagree about what ``R`` means.

So this module imports them from disk, lazily and with an honest error.  It is
also the only place in the monster that knows where ``ENRG-AI`` lives.

The path is found in this order:

1. ``MONSTER_ENRG_AI`` (an operator override — a container, a second checkout);
2. ``../ENRG-AI`` and ``../../ENRG-AI`` relative to this file (the workspace
   layout ``entrain-core-workspace/{ENRG-AI,monster}``).

If neither holds an ``agent/`` package, the import raises
:class:`MonsterImportError` with the paths it tried.  A missing dependency is
then a sentence about the filesystem, not an ``ImportError: agent``.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import ModuleType

__all__ = ["ENV_ROOT", "MonsterImportError", "agent_root", "import_agent", "oscillator", "power"]

#: Environment variable an operator sets to point the monster at another checkout.
ENV_ROOT = "MONSTER_ENRG_AI"


class MonsterImportError(ModuleNotFoundError):
    """``ENRG-AI`` was not found where it was expected to be."""


def agent_root() -> Path:
    """Return the directory that contains ``agent/`` — or explain why not."""
    here = Path(__file__).resolve()
    candidates: list[Path] = []
    configured = os.environ.get(ENV_ROOT, "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    # ``monster/monster/enrg.py`` -> parents[1] is the repository root
    # (``.../monster``), parents[2] is the workspace (``.../entrain-core-workspace``).
    candidates.append(here.parents[1].parent / "ENRG-AI")
    candidates.append(here.parents[2] / "ENRG-AI")
    for candidate in candidates:
        if (candidate / "agent" / "__init__.py").is_file():
            return candidate.resolve()
    tried = ", ".join(str(path) for path in candidates)
    raise MonsterImportError(
        f"ENRG-AI not found: no 'agent/__init__.py' under {tried}. "
        f"Set {ENV_ROOT}=/path/to/ENRG-AI or place the checkout beside 'monster/'."
    )


def import_agent(name: str) -> ModuleType:
    """Import a module from ``ENRG-AI`` by its dotted name (``agent.physics.oscillator``)."""
    root = agent_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return importlib.import_module(name)


def oscillator() -> ModuleType:
    """``agent.physics.oscillator`` — the ported Kuramoto arithmetic."""
    return import_agent("agent.physics.oscillator")


def power() -> ModuleType:
    """``agent.backtest.power`` — the resolution gate (edge vs sample size)."""
    return import_agent("agent.backtest.power")
