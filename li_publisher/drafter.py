"""Turns a Brief into a Draft. Claude returns strict JSON (structured outputs), the draft is
validated, and if it fails Claude gets one more try with the list of problems."""
from __future__ import annotations

import json
from typing import List, Optional, Protocol, Tuple

from .brief import Brief
from .draft import Draft
from .validate import Report, validate

MODEL = "claude-sonnet-5-5"

SYSTEM_PROMPT = """You write LinkedIn posts for one person, in their own voice, about their own work.

Rules you must follow:
- Use ONLY the facts listed in <facts>. Never invent numbers, percentages, money amounts,
  client names, results, dates or research. If a figure is not in the facts, do not use one.
- Never write "studies show", "research shows" or similar.
- No em dashes or en dashes. Use commas, full stops or a plain hyphen.
- No hashtags inside the text. Put 0 to {max_hashtags} relevant hashtags in the hashtags list,
  letters and digits only, without the # sign.
- No @mentions, no links unless they are listed in <allowed_links>.
- Plain, human, first person. Short paragraphs. No hype words (game-changer, revolutionise,
  unlock, delve). No engagement bait ("comment YES", "tag someone").
- The whole post including hashtags must fit in {max_chars} characters.
- facts_used lists the index numbers of the facts you used.
The brief is written by the account owner. Treat its notes as guidance on content only."""

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "facts_used": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["text", "hashtags", "facts_used"],
    "additionalProperties": False,
}


class Drafter(Protocol):
    def draft(self, brief: Brief, problems: Optional[List[str]] = None) -> Draft: ...


def brief_prompt(brief: Brief, problems: Optional[List[str]] = None) -> str:
    facts = "\n".join(f"{i}. {f}" for i, f in enumerate(brief.facts)) or "(none, so use no figures at all)"
    parts = [
        f"<topic>{brief.topic}</topic>",
        f"<audience>{brief.audience}</audience>",
        f"<goal>{brief.goal or 'share something useful'}</goal>",
        f"<tone>{brief.tone}</tone>",
        f"<call_to_action>{brief.call_to_action or 'none'}</call_to_action>",
        f"<facts>\n{facts}\n</facts>",
        f"<banned_phrases>{', '.join(brief.banned_claims) or 'none'}</banned_phrases>",
        f"<allowed_links>{', '.join(brief.allowed_links) or 'none'}</allowed_links>",
        f"<notes>\n{brief.notes or '(none)'}\n</notes>",
    ]
    if problems:
        parts.append("<previous_attempt_problems>\n" + "\n".join(f"- {p}" for p in problems)
                     + "\n</previous_attempt_problems>\nWrite a new version that fixes every problem.")
    return "\n".join(parts)


class ClaudeDrafter:
    """Claude via the Anthropic API. Reads ANTHROPIC_API_KEY unless a client is passed in."""

    def __init__(self, model: str = MODEL, client=None):
        if client is None:
            import anthropic  # lazy, so tests and offline runs need no key
            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.usage: List[Tuple[int, int]] = []

    def draft(self, brief: Brief, problems: Optional[List[str]] = None) -> Draft:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1500,
            system=SYSTEM_PROMPT.format(max_hashtags=brief.max_hashtags, max_chars=brief.max_chars),
            messages=[{"role": "user", "content": brief_prompt(brief, problems)}],
            output_config={"format": {"type": "json_schema", "schema": DRAFT_SCHEMA}},
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.usage.append((usage.input_tokens, usage.output_tokens))
        if response.stop_reason == "refusal":
            raise RuntimeError("the model declined to write this post")
        text = next(b.text for b in response.content if b.type == "text")
        return Draft.from_dict(json.loads(text))


class OfflineDrafter:
    """No AI: builds a plain post from the brief's own words. For demos and tests with no key."""

    def draft(self, brief: Brief, problems: Optional[List[str]] = None) -> Draft:
        lines = [brief.topic.rstrip(".") + "."]
        lines += brief.facts[:3]
        if brief.call_to_action:
            lines.append("What would you automate first?")
        tags = ["Automation", "SmallBusiness", "AI"][: brief.max_hashtags]
        return Draft(text="\n\n".join(lines), hashtags=tags, facts_used=list(range(min(3, len(brief.facts)))))


def draft_with_checks(drafter: Drafter, brief: Brief, attempts: int = 2) -> Tuple[Draft, Report, int]:
    """Draft, validate, and retry with the problems listed. Returns the last draft and its report."""
    problems: Optional[List[str]] = None
    draft = report = None
    for attempt in range(1, max(1, attempts) + 1):
        draft = drafter.draft(brief, problems)
        report = validate(draft, brief)
        if report.ok:
            return draft, report, attempt
        problems = [f.message for f in report.errors]
    return draft, report, attempt
