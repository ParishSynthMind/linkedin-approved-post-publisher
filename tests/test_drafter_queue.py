import json

import pytest

from li_publisher.draft import Draft
from li_publisher.drafter import DRAFT_SCHEMA, ClaudeDrafter, OfflineDrafter, draft_with_checks
from li_publisher.queue import ApprovalQueue, QueueError, render_review_html
from li_publisher.validate import validate

from .conftest import GOOD_TEXT, FakeAnthropic


def test_claude_drafter_uses_schema_and_model(brief):
    fake = FakeAnthropic([{"text": GOOD_TEXT, "hashtags": ["Automation"], "facts_used": [0, 1]}])
    draft, report, attempts = draft_with_checks(ClaudeDrafter(client=fake), brief)
    assert report.ok and attempts == 1
    call = fake.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["output_config"]["format"]["schema"] == DRAFT_SCHEMA
    assert "300 supplier invoices" in call["messages"][0]["content"]


def test_retry_sends_problems_back(brief):
    bad = {"text": GOOD_TEXT + " Errors fell 90%.", "hashtags": [], "facts_used": []}
    good = {"text": GOOD_TEXT, "hashtags": [], "facts_used": []}
    fake = FakeAnthropic([bad, good])
    drafter = ClaudeDrafter(client=fake)
    draft, report, attempts = draft_with_checks(drafter, brief, attempts=2)
    assert report.ok and attempts == 2
    assert "previous_attempt_problems" in fake.calls[1]["messages"][0]["content"]
    assert "'90'" in fake.calls[1]["messages"][0]["content"]
    assert drafter.usage == [(500, 200), (500, 200)]


def test_still_failing_after_attempts(brief):
    bad = {"text": GOOD_TEXT + " Errors fell 90%.", "hashtags": [], "facts_used": []}
    _, report, attempts = draft_with_checks(ClaudeDrafter(client=FakeAnthropic([bad, bad])), brief, attempts=2)
    assert not report.ok and attempts == 2


def test_refusal_raises(brief):
    fake = FakeAnthropic([{}])
    fake.create = lambda **kw: type("R", (), {"stop_reason": "refusal", "usage": None, "content": []})()
    with pytest.raises(RuntimeError):
        ClaudeDrafter(client=fake).draft(brief)


def test_offline_drafter_passes_checks(brief):
    d = OfflineDrafter().draft(brief)
    assert validate(d, brief).ok, validate(d, brief).as_text()


def _queue_with_item(tmp_path, brief, text=GOOD_TEXT):
    q = ApprovalQueue(tmp_path / "queue.json")
    d = Draft(text, ["Automation"])
    r = validate(d, brief)
    return q, q.add("invoice.md", d, r.to_list(), r.ok)


def test_new_drafts_are_pending_never_approved(tmp_path, brief):
    q, item = _queue_with_item(tmp_path, brief)
    assert item["status"] == "pending" and item["approved_hash"] is None
    assert q.approved() == []
    saved = json.loads((tmp_path / "queue.json").read_text())
    assert saved["items"][0]["id"] == item["id"]


def test_approve_pins_hash_and_edit_resets(tmp_path, brief):
    q, item = _queue_with_item(tmp_path, brief)
    with pytest.raises(QueueError):
        q.approve(item["id"], by="  ")
    a = q.approve(item["id"], by="Owner")
    assert a["approved_hash"] == item["content_hash"] and q.approved()
    with pytest.raises(QueueError):
        q.approve(item["id"], by="Owner")       # already approved
    d = Draft(GOOD_TEXT.replace("surprised me", "was simple"), ["Automation"])
    r = validate(d, brief)
    e = q.edit(item["id"], d, r.to_list(), r.ok)
    assert e["status"] == "pending" and e["approved_hash"] is None and q.approved() == []


def test_failed_draft_cannot_be_approved(tmp_path, brief):
    q, item = _queue_with_item(tmp_path, brief, text=GOOD_TEXT + " Up 50%.")
    assert item["status"] == "failed"
    with pytest.raises(QueueError):
        q.approve(item["id"], by="Owner")


def test_reject_and_unknown_id(tmp_path, brief):
    q, item = _queue_with_item(tmp_path, brief)
    assert q.reject(item["id"], "off topic")["status"] == "rejected"
    with pytest.raises(QueueError):
        q.get("nope")
    q.mark_published(item["id"], "urn:li:share:1")
    with pytest.raises(QueueError):
        q.reject(item["id"])


def test_review_html_escapes_and_shows_commands(tmp_path, brief):
    q, item = _queue_with_item(tmp_path, brief, text=GOOD_TEXT + " <script>alert(1)</script>")
    page = render_review_html(q.load())
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    q2, item2 = _queue_with_item(tmp_path / "b", brief)
    page2 = render_review_html(q2.load())
    assert f"approve {item2['id']}" in page2 and "Nothing here is published" in page2
