"""Versioned prompts (D-015).

The system prompt is a template file. Its version is its name plus a hash of the template *and*
the tool definitions, because tool descriptions are prompt text too. Every LLM call and agent turn
records this version, so an eval result always points at the exact prompt that produced it.
"""

import hashlib
import json
from functools import cache
from pathlib import Path

from app.agent.tools import tool_specs

SYSTEM_PROMPT_NAME = "system_v2"
_DIR = Path(__file__).resolve().parent


@cache
def system_template(name: str = SYSTEM_PROMPT_NAME) -> str:
    return (_DIR / f"{name}.txt").read_text(encoding="utf-8")


@cache
def prompt_version(name: str = SYSTEM_PROMPT_NAME) -> str:
    material = system_template(name) + json.dumps(tool_specs(), sort_keys=True)
    return f"{name}+{hashlib.sha256(material.encode()).hexdigest()[:8]}"
