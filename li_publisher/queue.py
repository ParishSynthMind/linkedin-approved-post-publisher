"""The approval queue. Every draft lands here as `pending`; nothing is published unless a person
marks it `approved`. Approval is pinned to the content hash, so a draft edited after approval
drops back to `pending` and has to be approved again.

queue.json is the source of truth, written atomically. review.html is a read-only view of it.
"""
from __future__ import annotations

import html
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .draft import Draft

STATUSES = ("pending", "approved", "rejected", "published", "failed")


class QueueError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ApprovalQueue:
    def __init__(self, path: str | Path = "queue/queue.json"):
        self.path = Path(path)

    # storage
    def load(self) -> List[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8")).get("items", [])

    def save(self, items: List[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": items}, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, item_id: str) -> dict:
        for item in self.load():
            if item["id"] == item_id:
                return item
        raise QueueError(f"no draft with id {item_id!r}")

    def _update(self, item_id: str, **changes) -> dict:
        items = self.load()
        for item in items:
            if item["id"] == item_id:
                item.update(changes)
                item["updated_at"] = now_iso()
                self.save(items)
                return item
        raise QueueError(f"no draft with id {item_id!r}")

    # actions
    def add(self, brief_name: str, draft: Draft, report_list: List[dict], ok: bool) -> dict:
        items = self.load()
        item = {
            "id": secrets.token_hex(4),
            "brief": brief_name,
            "status": "pending" if ok else "failed",
            "draft": draft.to_dict(),
            "commentary": draft.commentary(),
            "content_hash": draft.content_hash(),
            "checks": report_list,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "approved_hash": None,
            "approved_by": None,
            "approved_at": None,
            "post_urn": None,
            "error": None if ok else "failed validation, fix the brief and draft again",
        }
        items.append(item)
        self.save(items)
        return item

    def approve(self, item_id: str, by: str) -> dict:
        item = self.get(item_id)
        if item["status"] != "pending":
            raise QueueError(f"draft {item_id} is {item['status']}, only pending drafts can be approved")
        if any(f["severity"] == "error" for f in item["checks"]):
            raise QueueError(f"draft {item_id} has failed checks and cannot be approved")
        if not by.strip():
            raise QueueError("say who is approving (--by)")
        return self._update(item_id, status="approved", approved_hash=item["content_hash"],
                            approved_by=by.strip(), approved_at=now_iso())

    def reject(self, item_id: str, reason: str = "") -> dict:
        item = self.get(item_id)
        if item["status"] == "published":
            raise QueueError("this draft is already published; delete it on LinkedIn instead")
        return self._update(item_id, status="rejected", error=reason or None)

    def edit(self, item_id: str, draft: Draft, report_list: List[dict], ok: bool) -> dict:
        item = self.get(item_id)
        if item["status"] == "published":
            raise QueueError("this draft is already published")
        return self._update(item_id, draft=draft.to_dict(), commentary=draft.commentary(),
                            content_hash=draft.content_hash(), checks=report_list,
                            status="pending" if ok else "failed",
                            approved_hash=None, approved_by=None, approved_at=None)

    def mark_published(self, item_id: str, post_urn: Optional[str]) -> dict:
        return self._update(item_id, status="published", post_urn=post_urn, published_at=now_iso(), error=None)

    def mark_failed(self, item_id: str, error: str) -> dict:
        return self._update(item_id, status="failed", error=error)

    def approved(self) -> List[dict]:
        return [i for i in self.load() if i["status"] == "approved"]


def render_review_html(items: List[dict], queue_path: str = "queue/queue.json") -> str:
    """A static, read-only review page. Approving is done with the CLI command shown on each card,
    so the page itself can never publish anything."""
    colours = {"pending": "#b26a00", "approved": "#1a7f37", "rejected": "#777", "published": "#0a66c2",
               "failed": "#c62828"}
    cards = []
    order = {s: i for i, s in enumerate(STATUSES)}
    for item in sorted(items, key=lambda i: (order.get(i["status"], 9), i["created_at"])):
        esc = html.escape
        checks = "".join(
            f'<li class="{esc(c["severity"])}">{esc(c["severity"]).upper()}: {esc(c["message"])}</li>'
            for c in item["checks"]) or '<li class="ok">All checks passed.</li>'
        action = ""
        if item["status"] == "pending":
            action = (f'<pre>python -m li_publisher approve {esc(item["id"])} --by "your name"\n'
                      f'python -m li_publisher reject {esc(item["id"])} --reason "..."</pre>')
        elif item["status"] == "approved":
            action = f'<p class="meta">Approved by {esc(item["approved_by"])} at {esc(item["approved_at"])}</p>'
        elif item["status"] == "published":
            action = f'<p class="meta">Published as {esc(item["post_urn"] or "")}</p>'
        cards.append(f"""
<section class="card">
  <div class="top"><span class="badge" style="background:{colours.get(item['status'], '#555')}">
  {esc(item['status'])}</span> <b>{esc(item['id'])}</b> from <i>{esc(item['brief'])}</i>
  <span class="meta">{len(item['commentary'])} characters, created {esc(item['created_at'])}</span></div>
  <div class="post">{esc(item['commentary'])}</div>
  <ul class="checks">{checks}</ul>
  {action}
</section>""")
    body = "".join(cards) or "<p>The queue is empty.</p>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LinkedIn post approval queue</title>
<style>
body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#f3f2ef; margin:0; padding:24px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} .sub {{ color:#555; margin-bottom:20px; }}
.card {{ background:#fff; border-radius:10px; padding:18px 20px; margin:0 auto 18px; max-width:640px;
        box-shadow:0 1px 3px rgba(0,0,0,.15); }}
.top {{ font-size:14px; margin-bottom:12px; }} .meta {{ color:#666; font-size:13px; }}
.badge {{ color:#fff; border-radius:12px; padding:2px 10px; font-size:12px; text-transform:uppercase; }}
.post {{ white-space:pre-wrap; font-size:15px; line-height:1.5; color:#191919; }}
.checks {{ font-size:13px; padding-left:18px; }} .error {{ color:#c62828; }} .warning {{ color:#b26a00; }}
.ok {{ color:#1a7f37; }} pre {{ background:#f6f8fa; padding:10px; border-radius:6px; font-size:12px; }}
</style></head><body>
<h1 style="max-width:640px;margin:0 auto 4px">LinkedIn post approval queue</h1>
<p class="sub" style="max-width:640px;margin:0 auto 20px">Read-only view of {html.escape(queue_path)}.
Nothing here is published until it is approved from the command line.</p>
{body}
</body></html>"""
