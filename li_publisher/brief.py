"""Reads a post brief: a few `key: value` lines, a `facts:` list, then `---` and free notes.

The facts list is the only source of figures, names and claims the post may use.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

LIST_KEYS = {"facts", "banned_claims", "allowed_links"}


@dataclass
class Brief:
    name: str
    topic: str
    audience: str = "people in my network"
    goal: str = ""
    tone: str = "plain, friendly, first person"
    call_to_action: str = ""
    max_chars: int = 1300
    max_hashtags: int = 3
    facts: List[str] = field(default_factory=list)
    banned_claims: List[str] = field(default_factory=list)
    allowed_links: List[str] = field(default_factory=list)
    notes: str = ""


class BriefError(ValueError):
    pass


def parse_brief(text: str, name: str = "brief") -> Brief:
    head, _, notes = text.partition("\n---")
    values: dict = {}
    lists: dict = {k: [] for k in LIST_KEYS}
    current = None
    for raw in head.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and current:
            lists[current].append(line.lstrip()[2:].strip())
            continue
        key, sep, value = line.partition(":")
        key = key.strip().lower()
        if not sep:
            raise BriefError(f"line without a key: {line!r}")
        current = None
        if key in LIST_KEYS:
            if value.strip():
                lists[key].extend(v.strip() for v in value.split(",") if v.strip())
            current = key
        else:
            values[key] = value.strip()
    if not values.get("topic"):
        raise BriefError("a brief needs a topic")
    brief = Brief(name=name, topic=values["topic"], notes=notes.strip(), **lists)
    for key in ("audience", "goal", "tone", "call_to_action"):
        if values.get(key):
            setattr(brief, key, values[key])
    for key in ("max_chars", "max_hashtags"):
        if values.get(key):
            try:
                setattr(brief, key, int(values[key]))
            except ValueError:
                raise BriefError(f"{key} must be a whole number") from None
    if not 200 <= brief.max_chars <= 3000:
        raise BriefError("max_chars must be between 200 and 3000")
    if not 0 <= brief.max_hashtags <= 5:
        raise BriefError("max_hashtags must be between 0 and 5")
    return brief


def load_brief(path: str | Path) -> Brief:
    path = Path(path)
    return parse_brief(path.read_text(encoding="utf-8"), name=path.stem)
