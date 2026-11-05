"""Publishes approved drafts, and only approved drafts, to the signed-in member's own profile.

Before every post: the draft must be `approved`, its content must still match the hash that
was approved, it must pass the checks again, and its hash must not be in the posted log
(duplicate guard). The author is always the member the token belongs to, read live from
/v2/userinfo and compared with the one saved at sign-in, so the tool cannot post anywhere else.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

import requests

from .brief import Brief
from .draft import Draft
from .linkedin import DuplicatePost, LinkedInClient, LinkedInError, post_url
from .queue import ApprovalQueue, now_iso
from .validate import validate


@dataclass
class Outcome:
    item_id: str
    result: str          # published | dry_run | skipped_duplicate | blocked | failed
    detail: str = ""
    urn: Optional[str] = None
    code: int = 0


@dataclass
class PublishRun:
    outcomes: List[Outcome] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return max([o.code or 1 for o in self.outcomes if o.result == "failed"], default=0)


class PostedLog:
    """Append-only JSON lines: id, hash, urn, time, length. No post text and no token."""

    def __init__(self, path: str | Path = "queue/posted-log.jsonl"):
        self.path = Path(path)

    def hashes(self) -> set:
        if not self.path.exists():
            return set()
        out = set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.add(json.loads(line)["content_hash"])
        return out

    def append(self, **entry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": now_iso(), **entry}) + "\n")


def publish_approved(queue: ApprovalQueue, log: PostedLog, load_brief: Callable[[str], Brief],
                     client: Optional[LinkedInClient], saved_person_urn: Optional[str],
                     dry_run: bool = False, only: Optional[str] = None,
                     say: Callable[[str], None] = print) -> PublishRun:
    run = PublishRun()
    items = [i for i in queue.approved() if only is None or i["id"] == only]
    if not items:
        say("Nothing approved to publish.")
        return run

    author = saved_person_urn
    if not dry_run:
        if client is None:
            raise LinkedInError("no LinkedIn client; run `auth` first")
        live_urn = client.person_urn()
        if saved_person_urn and live_urn != saved_person_urn:
            raise LinkedInError("the token belongs to a different member than the one saved at sign-in; "
                                "refusing to post. Run `auth` again as the profile owner.")
        author = live_urn

    posted = log.hashes()
    for item in items:
        draft = Draft.from_dict(item["draft"])
        iid = item["id"]
        if draft.content_hash() != item.get("approved_hash"):
            queue._update(iid, status="pending", approved_hash=None, approved_by=None, approved_at=None)
            run.outcomes.append(Outcome(iid, "blocked", "content changed after approval, sent back to pending"))
            continue
        report = validate(draft, load_brief(item["brief"]))
        if not report.ok:
            queue.mark_failed(iid, "failed checks at publish time: " + "; ".join(f.message for f in report.errors))
            run.outcomes.append(Outcome(iid, "blocked", "failed checks at publish time"))
            continue
        if draft.content_hash() in posted:
            queue._update(iid, status="rejected", error="duplicate of a post already published")
            run.outcomes.append(Outcome(iid, "skipped_duplicate", "same content already in the posted log"))
            continue
        if dry_run:
            body = (client or LinkedInClient("dry-run")).post_body(author or "urn:li:person:DRYRUN",
                                                                   draft.text, draft.hashtags)
            say(f"[dry run] would POST /rest/posts for draft {iid}:\n" + json.dumps(body, indent=2, ensure_ascii=False))
            run.outcomes.append(Outcome(iid, "dry_run", f"{len(draft.commentary())} characters"))
            continue
        try:
            urn = client.create_post(author, draft.text, draft.hashtags)
        except DuplicatePost as exc:
            queue._update(iid, status="rejected", error=str(exc))
            run.outcomes.append(Outcome(iid, "skipped_duplicate", str(exc)))
            continue
        except requests.RequestException:
            # The request may or may not have reached LinkedIn, so it is never retried blindly.
            msg = "network error mid-post; check the profile before approving this draft again"
            queue.mark_failed(iid, msg)
            run.outcomes.append(Outcome(iid, "failed", msg, code=1))
            break
        except LinkedInError as exc:
            queue.mark_failed(iid, str(exc))
            run.outcomes.append(Outcome(iid, "failed", str(exc), code=getattr(exc, "exit_code", 1)))
            if getattr(exc, "exit_code", 1) in (3, 4, 5):   # auth, rate limit, version: stop the batch
                break
            continue
        queue.mark_published(iid, urn)
        log.append(id=iid, content_hash=draft.content_hash(), post_urn=urn, chars=len(draft.commentary()),
                   approved_by=item.get("approved_by"))
        posted.add(draft.content_hash())
        run.outcomes.append(Outcome(iid, "published", post_url(urn), urn))
    for o in run.outcomes:
        say(f"{o.item_id}: {o.result} {o.detail}".rstrip())
    return run
