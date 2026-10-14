"""Jinja2-based prompt templates.

All LLM prompts live as ``.j2`` files next to this module. Importers call
``render("system_de.j2", **vars)`` instead of inlining strings — that way
prompt edits don't require touching Python, and we can A/B-test by
swapping files at boot.

Autoescape is intentionally off: these templates produce plain text for an
LLM, not HTML. Don't add user-controlled data into a template variable
without thinking about prompt injection — the system prompt's
``<context>`` framing only protects content explicitly placed inside that
block, not other interpolations.
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_ENV: Environment | None = None


def _env() -> Environment:
    global _ENV
    if _ENV is None:
        _ENV = Environment(
            loader=FileSystemLoader(str(Path(__file__).parent)),
            # StrictUndefined: rendering a template with a missing variable
            # raises instead of silently emitting an empty string. Catches
            # typos before they reach the LLM.
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            # trim/lstrip_blocks intentionally OFF: these prompts are plain
            # text where whitespace matters (the LLM sees the literal
            # newlines around <context>). Use explicit {%- -%} in templates
            # if you need to strip.
            trim_blocks=False,
            lstrip_blocks=False,
        )
    return _ENV


def render(template_name: str, **vars) -> str:
    """Render ``template_name`` (relative to this package) with ``vars``."""
    return _env().get_template(template_name).render(**vars)
