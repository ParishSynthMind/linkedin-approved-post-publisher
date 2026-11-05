import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from li_publisher.brief import parse_brief  # noqa: E402

BRIEF_TEXT = """topic: What I learned automating invoice data entry
audience: small business owners
call_to_action: ask which task they would automate first
max_chars: 1100
max_hashtags: 3
facts:
- The firm received around 300 supplier invoices a month.
- Each invoice used to take about 4 minutes to key in by hand.
- A person still approves every invoice.
banned_claims: zero errors
---
Keep it short.
"""

GOOD_TEXT = ("I spent the last few weeks automating invoice entry for a small firm. They got around 300 "
             "supplier invoices a month, and each one took about 4 minutes to key in.\n\n"
             "The lesson surprised me. The hard part was not reading the PDF, it was the review step. "
             "A person still approves every invoice, and that is why the team trusts it.\n\n"
             "Which manual task would you automate first?")


@pytest.fixture
def brief():
    return parse_brief(BRIEF_TEXT, name="invoice")


@pytest.fixture
def brief_file(tmp_path):
    p = tmp_path / "invoice.md"
    p.write_text(BRIEF_TEXT)
    return p


class FakeAnthropic:
    """Stands in for anthropic.Anthropic(); returns queued JSON replies and records calls."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        reply = self.replies.pop(0)
        return SimpleNamespace(stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=500, output_tokens=200),
                               content=[SimpleNamespace(type="text", text=json.dumps(reply))])
