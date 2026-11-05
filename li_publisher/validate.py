"""Checks a Draft before it can enter the approval queue, and again right before publishing.

Errors block. Warnings are shown to the reviewer but do not block. The strictest check is
the figures check: every number in the post must appear in the brief's facts, because an
invented statistic under someone's own name is the most damaging thing an AI draft can do.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

from .brief import Brief
from .draft import Draft

LINKEDIN_MAX_CHARS = 3000   # LinkedIn's post box limit; the API docs do not publish a number
MIN_CHARS = 80
HASHTAG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,29}$")
DASHES = {"\u2014": "em dash", "\u2013": "en dash", "\u2015": "horizontal bar", "--": "double hyphen"}
DEFAULT_BANNED = [
    "guaranteed", "guarantee", "risk-free", "risk free", "#1", "number one", "world-class",
    "best in the world", "as an ai", "language model", "10x", "secret formula",
]
RESEARCH_PHRASES = [
    "studies show", "study shows", "research shows", "research suggests", "according to a study",
    "according to research", "a recent survey", "surveys show", "experts agree", "scientists say",
    "statistics show", "data shows", "data show",
]
BAIT_PHRASES = ["comment yes", "like if", "tag someone", "tag a friend", "share if", "type yes", "agree?"]
AI_TELLS = ["delve", "in today's fast-paced", "game-changer", "game changer", "unlock the power",
            "revolutionize", "revolutionise", "in the ever-evolving", "let's dive in", "buckle up"]
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
MENTION_RE = re.compile(r"(?<![\w.])@\w")
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


@dataclass
class Finding:
    severity: str  # "error" | "warning"
    code: str
    message: str


@dataclass
class Report:
    findings: List[Finding] = field(default_factory=list)

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, severity: str, code: str, message: str) -> None:
        self.findings.append(Finding(severity, code, message))

    def as_text(self) -> str:
        if not self.findings:
            return "All checks passed."
        return "\n".join(f"[{f.severity.upper()}] {f.code}: {f.message}" for f in self.findings)

    def to_list(self) -> List[dict]:
        return [f.__dict__ for f in self.findings]


def _norm_number(n: str) -> str:
    return n.replace(",", "")


def validate(draft: Draft, brief: Brief) -> Report:
    r = Report()
    text = draft.text.strip()
    full = draft.commentary()
    lower = text.casefold()

    # length
    if len(text) < MIN_CHARS:
        r.add("error", "too_short", f"post body is {len(text)} characters, minimum is {MIN_CHARS}")
    limit = min(brief.max_chars, LINKEDIN_MAX_CHARS)
    if len(full) > limit:
        r.add("error", "too_long", f"post is {len(full)} characters with hashtags, limit is {limit}")

    # dashes
    for mark, label in DASHES.items():
        if mark in full:
            r.add("error", "dash", f"contains an {label}; use a comma, full stop or plain hyphen")

    # hashtags
    if len(draft.hashtags) > brief.max_hashtags:
        r.add("error", "hashtag_spam", f"{len(draft.hashtags)} hashtags, the brief allows {brief.max_hashtags}")
    seen = set()
    for tag in draft.hashtags:
        if not HASHTAG_RE.match(tag):
            r.add("error", "hashtag_format", f"hashtag {tag!r} must be letters and digits only, 2 to 30 long")
        if tag.casefold() in seen:
            r.add("error", "hashtag_duplicate", f"hashtag {tag!r} appears twice")
        seen.add(tag.casefold())
    if re.search(r"(?<!\w)#\w", text):
        r.add("error", "hashtag_in_body", "hashtags belong in the hashtag list, not inside the post body")

    # invented figures and claims
    fact_text = " ".join(brief.facts)
    fact_numbers = {_norm_number(n) for n in NUMBER_RE.findall(fact_text)}
    for n in NUMBER_RE.findall(full):
        if _norm_number(n) not in fact_numbers:
            r.add("error", "unverified_figure", f"the figure {n!r} is not in the brief's facts")
    for phrase in RESEARCH_PHRASES:
        if phrase in lower:
            r.add("error", "unsourced_research", f"{phrase!r} claims research the brief does not supply")
    for phrase in DEFAULT_BANNED + [b.casefold() for b in brief.banned_claims]:
        if phrase.casefold() in full.casefold():
            r.add("error", "banned_claim", f"uses the banned phrase {phrase!r}")
    for i in draft.facts_used:
        if not 0 <= i < len(brief.facts):
            r.add("error", "unknown_fact", f"facts_used refers to fact {i}, the brief has {len(brief.facts)}")

    # links and mentions
    allowed = [a.rstrip("/").casefold() for a in brief.allowed_links]
    for url in URL_RE.findall(text):
        if url.rstrip("/.,").casefold() not in allowed:
            r.add("error", "unapproved_link", f"link {url!r} is not in the brief's allowed_links")
    if MENTION_RE.search(text):
        r.add("error", "mention", "the post @mentions someone; tag people by hand on LinkedIn instead")

    # style (warnings)
    for phrase in BAIT_PHRASES:
        if phrase in lower:
            r.add("warning", "engagement_bait", f"{phrase!r} reads as engagement bait")
    for phrase in AI_TELLS:
        if phrase in lower:
            r.add("warning", "ai_phrasing", f"{phrase!r} sounds machine-written")
    if len(EMOJI_RE.findall(text)) > 3:
        r.add("warning", "emoji", "more than 3 emoji")
    if brief.call_to_action and "?" not in text:
        r.add("warning", "no_question", "the brief asks for a call to action but the post asks nothing")
    return r
