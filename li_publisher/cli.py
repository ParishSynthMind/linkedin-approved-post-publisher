"""Command line: auth, whoami, draft, review, approve, reject, edit, list, publish."""
from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser
from pathlib import Path
from typing import List, Optional

from .brief import BriefError, load_brief
from .draft import Draft
from .drafter import ClaudeDrafter, OfflineDrafter, draft_with_checks
from .linkedin import (DEFAULT_VERSION, LinkedInClient, LinkedInError, authorization_url, exchange_code,
                       new_state, wait_for_callback)
from .publisher import PostedLog, publish_approved
from .queue import ApprovalQueue, QueueError, render_review_html
from .tokens import TokenStoreError, days_left, make_store
from .validate import validate

DEFAULT_REDIRECT = "http://localhost:8765/callback"


def _queue_dir() -> Path:
    return Path(os.environ.get("QUEUE_DIR", "queue"))


def _queue() -> ApprovalQueue:
    return ApprovalQueue(_queue_dir() / "queue.json")


def _write_review(queue: ApprovalQueue) -> Path:
    out = _queue_dir() / "review.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_review_html(queue.load(), str(queue.path)), encoding="utf-8")
    return out


def _client(store) -> LinkedInClient:
    return LinkedInClient(store.get("access_token"),
                          version=os.environ.get("LINKEDIN_VERSION", DEFAULT_VERSION),
                          api=os.environ.get("LINKEDIN_API", "posts"))


def cmd_auth(args) -> int:
    store = make_store(args.store)
    client_id, client_secret = store.get("client_id"), store.get("client_secret")
    redirect = os.environ.get("LINKEDIN_REDIRECT_URI", DEFAULT_REDIRECT)
    state = new_state()
    url = authorization_url(client_id, redirect, state)
    print("Open this page, sign in as the profile owner and press Allow:\n" + url)
    code = wait_for_callback(redirect, state, timeout=args.timeout,
                             ready=None if args.no_browser else (lambda: webbrowser.open(url)))
    token = exchange_code(code, client_id, client_secret, redirect)
    expires_at = str(int(time.time()) + int(token["expires_in"]))
    client = LinkedInClient(token["access_token"], version=os.environ.get("LINKEDIN_VERSION", DEFAULT_VERSION))
    urn = client.person_urn()
    store.save(access_token=token["access_token"], expires_at=expires_at, person_urn=urn)
    print(f"Signed in. Posting as {urn}. Token valid for {int(token['expires_in']) // 86400} days "
          f"(scopes: {token.get('scope', 'not reported')}).")
    return 0


def cmd_whoami(args) -> int:
    store = make_store(args.store)
    left = days_left(store)
    print(f"Saved member: {store.get('person_urn')}")
    print(f"Token: {'expired' if left < 0 else f'{left:.0f} days left'}")
    if args.live:
        print(f"LinkedIn says this token belongs to: {_client(store).person_urn()}")
    return 0 if left >= 0 else 3


def cmd_draft(args) -> int:
    brief = load_brief(args.brief)
    drafter = OfflineDrafter() if args.offline else ClaudeDrafter(model=args.model)
    draft, report, attempts = draft_with_checks(drafter, brief, attempts=args.attempts)
    queue = _queue()
    item = queue.add(str(args.brief), draft, report.to_list(), report.ok)
    print(f"Draft {item['id']} ({len(draft.commentary())} characters, {attempts} attempt(s)): {item['status']}")
    print(report.as_text())
    if getattr(drafter, "usage", None):
        tin = sum(u[0] for u in drafter.usage)
        tout = sum(u[1] for u in drafter.usage)
        print(f"Claude tokens: {tin} in, {tout} out")
    print(f"Review page: {_write_review(queue)}")
    return 0 if report.ok else 2


def cmd_review(args) -> int:
    out = _write_review(_queue())
    print(f"Review page: {out}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


def cmd_list(args) -> int:
    for item in _queue().load():
        first = item["commentary"].splitlines()[0][:70] if item["commentary"] else ""
        print(f"{item['id']}  {item['status']:<9}  {len(item['commentary']):>4} chars  {first}")
    return 0


def cmd_approve(args) -> int:
    item = _queue().approve(args.id, args.by)
    _write_review(_queue())
    print(f"Approved {item['id']} (hash {item['approved_hash']}). It will publish on the next `publish` run.")
    return 0


def cmd_reject(args) -> int:
    _queue().reject(args.id, args.reason)
    _write_review(_queue())
    print(f"Rejected {args.id}.")
    return 0


def cmd_edit(args) -> int:
    queue = _queue()
    item = queue.get(args.id)
    old = Draft.from_dict(item["draft"])
    text = Path(args.text_file).read_text(encoding="utf-8") if args.text_file else old.text
    tags = [t.strip().lstrip("#") for t in args.hashtags.split(",") if t.strip()] if args.hashtags is not None \
        else old.hashtags
    draft = Draft(text=text.strip(), hashtags=tags, facts_used=old.facts_used)
    report = validate(draft, load_brief(item["brief"]))
    queue.edit(args.id, draft, report.to_list(), report.ok)
    _write_review(queue)
    print(f"Edited {args.id}; it is back to {'pending' if report.ok else 'failed'} and needs approving again.")
    print(report.as_text())
    return 0 if report.ok else 2


def cmd_publish(args) -> int:
    store = make_store(args.store)
    queue = _queue()
    try:
        saved_urn = store.get("person_urn")
    except TokenStoreError:
        saved_urn = None
    client = None
    if not args.dry_run:
        if days_left(store) < 0:
            print("The LinkedIn token has expired (they last 60 days). Run `auth` again.", file=sys.stderr)
            return 3
        client = _client(store)
    run = publish_approved(queue, PostedLog(_queue_dir() / "posted-log.jsonl"), load_brief, client, saved_urn,
                           dry_run=args.dry_run, only=args.id)
    _write_review(queue)
    return run.exit_code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="li_publisher", description=__doc__)
    p.add_argument("--store", default="", help="keychain (default) or file")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth", help="sign in with LinkedIn (OAuth 2.0) and save the token")
    a.add_argument("--no-browser", action="store_true")
    a.add_argument("--timeout", type=float, default=300)
    a.set_defaults(func=cmd_auth)

    w = sub.add_parser("whoami", help="show the saved member and token expiry")
    w.add_argument("--live", action="store_true", help="also ask LinkedIn who the token belongs to")
    w.set_defaults(func=cmd_whoami)

    d = sub.add_parser("draft", help="draft a post from a brief into the approval queue")
    d.add_argument("brief")
    d.add_argument("--offline", action="store_true", help="no AI, template draft")
    d.add_argument("--attempts", type=int, default=2)
    d.add_argument("--model", default="claude-sonnet-5-5")
    d.set_defaults(func=cmd_draft)

    r = sub.add_parser("review", help="rebuild queue/review.html")
    r.add_argument("--open", action="store_true")
    r.set_defaults(func=cmd_review)

    sub.add_parser("list", help="list the queue").set_defaults(func=cmd_list)

    ap = sub.add_parser("approve", help="approve a pending draft")
    ap.add_argument("id")
    ap.add_argument("--by", required=True)
    ap.set_defaults(func=cmd_approve)

    rj = sub.add_parser("reject", help="reject a draft")
    rj.add_argument("id")
    rj.add_argument("--reason", default="")
    rj.set_defaults(func=cmd_reject)

    e = sub.add_parser("edit", help="replace a draft's text or hashtags (needs approving again)")
    e.add_argument("id")
    e.add_argument("--text-file")
    e.add_argument("--hashtags", help="comma separated, without #")
    e.set_defaults(func=cmd_edit)

    pb = sub.add_parser("publish", help="publish every approved draft to your own profile")
    pb.add_argument("--dry-run", action="store_true", help="show the exact request, send nothing")
    pb.add_argument("--id", help="publish only this draft")
    pb.set_defaults(func=cmd_publish)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (BriefError, QueueError, TokenStoreError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except LinkedInError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 1)
