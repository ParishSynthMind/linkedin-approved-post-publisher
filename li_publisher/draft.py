"""A drafted post: body text, hashtags (without the # sign) and which brief facts it used."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import List


@dataclass
class Draft:
    text: str
    hashtags: List[str] = field(default_factory=list)
    facts_used: List[int] = field(default_factory=list)

    def commentary(self) -> str:
        """The exact text a reader sees: body, a blank line, then the hashtags."""
        body = self.text.strip()
        if not self.hashtags:
            return body
        return body + "\n\n" + " ".join("#" + h for h in self.hashtags)

    def content_hash(self) -> str:
        """Hash of what would be published, whitespace-normalised and case-folded,
        so a re-run of the same post is caught even if spacing changed."""
        norm = " ".join(self.commentary().split()).casefold()
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Draft":
        return cls(text=str(data.get("text", "")),
                   hashtags=[str(h) for h in data.get("hashtags", [])],
                   facts_used=[int(i) for i in data.get("facts_used", [])])
