#!/usr/bin/env python3
"""Codex-side entry point: runs the shared engine that lives next door in skills/codex/scripts/bridge.py.
Default seats here favor Claude (the caller is Codex): ask/task -> fable:max, review -> opus:max, council -> fable:max,opus:max."""
import os
import runpy
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_ENGINES = (_HERE.parents[3] / "skills" / "codex" / "scripts" / "bridge.py",
            _HERE.parents[2] / "codex" / "scripts" / "bridge.py")
ENGINE = Path(os.environ.get("CLAUDE_BRIDGE_ENGINE") or
              next((path for path in _ENGINES if path.is_file()), _ENGINES[0])).resolve()
BUILTIN_SEATS = {"BRIDGE_BUILTIN_DEFAULT_SEAT": "fable:max", "BRIDGE_BUILTIN_REVIEW_SEATS": "opus:max",
                 "BRIDGE_BUILTIN_COUNCIL_SEATS": "fable:max,opus:max"}
os.environ.setdefault("BRIDGE_CALLER", "OpenAI Codex")
# this entry point's built-in seat lists travel in their own variables and never as the user-facing BRIDGE_DEFAULT_SEAT /
# BRIDGE_REVIEW_SEATS / BRIDGE_COUNCIL_SEATS (S-DEF): the engine uses each where the user's variable is unset, names a variable
# in a refusal only when the user set it, and drops these from every seat CLI's environment
os.environ.update(BUILTIN_SEATS)
if not ENGINE.exists():
    sys.exit(f"bridge engine not found at {ENGINE}")
sys.argv[0] = str(ENGINE)
runpy.run_path(str(ENGINE), run_name="__main__")
