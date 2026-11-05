import json
import threading
import time
import urllib.request

import pytest
import requests
import responses

from li_publisher import cli
from li_publisher.draft import Draft
from li_publisher.brief import load_brief
from li_publisher.drafter import ClaudeDrafter
from li_publisher.linkedin import TOKEN_URL, LinkedInError
from li_publisher.publisher import PostedLog, publish_approved
from li_publisher.queue import ApprovalQueue
from li_publisher.validate import validate

from .conftest import BRIEF_TEXT, GOOD_TEXT

API = "https://api.linkedin.com"
ME = "urn:li:person:782bbtaQ"


@pytest.fixture
def env(tmp_path, monkeypatch, brief_file):
    monkeypatch.setenv("QUEUE_DIR", str(tmp_path / "queue"))
    monkeypatch.setenv("LINKEDIN_TOKEN_STORE", "file")
    monkeypatch.setenv("LINKEDIN_TOKEN_FILE", str(tmp_path / "tok.json"))
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", "cid")
    monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("LINKEDIN_REDIRECT_URI", "http://localhost:8798/callback")
    (tmp_path / "tok.json").write_text(json.dumps({"access_token": "AQVtoken", "person_urn": ME,
                                                   "expires_at": str(int(time.time()) + 86400 * 30)}))
    return tmp_path


def add_draft(tmp_path, brief_file, text=GOOD_TEXT, approve=True):
    q = ApprovalQueue(tmp_path / "queue" / "queue.json")
    d = Draft(text, ["Automation"])
    r = validate(d, load_brief(brief_file))
    item = q.add(str(brief_file), d, r.to_list(), r.ok)
    if approve:
        q.approve(item["id"], by="Owner")
    return q, item


def mock_userinfo(sub="782bbtaQ"):
    responses.get(API + "/v2/userinfo", json={"sub": sub, "name": "Test Owner"})


@responses.activate
def test_pending_drafts_are_never_published(env, brief_file):
    add_draft(env, brief_file, approve=False)
    assert cli.main(["publish"]) == 0
    assert not any(c.request.url.endswith("/rest/posts") for c in responses.calls)


@responses.activate
def test_publish_approved_then_duplicate_guard(env, brief_file, capsys):
    mock_userinfo()
    responses.post(API + "/rest/posts", status=201, headers={"x-restli-id": "urn:li:share:111"})
    q, item = add_draft(env, brief_file)
    assert cli.main(["publish"]) == 0
    assert q.get(item["id"])["status"] == "published" and q.get(item["id"])["post_urn"] == "urn:li:share:111"
    log = (env / "queue" / "posted-log.jsonl").read_text()
    assert "urn:li:share:111" in log and "AQVtoken" not in log and "invoice" not in log.lower()
    assert json.loads(responses.calls[1].request.body)["author"] == ME

    # same text again, approved again: blocked locally, no second POST
    q2, item2 = add_draft(env, brief_file)
    assert cli.main(["publish"]) == 0
    assert q2.get(item2["id"])["status"] == "rejected"
    assert sum(c.request.url.endswith("/rest/posts") for c in responses.calls) == 1
    assert (env / "queue" / "review.html").exists()


@responses.activate
def test_refuses_when_token_belongs_to_someone_else(env, brief_file):
    mock_userinfo(sub="someoneElse")
    q, item = add_draft(env, brief_file)
    assert cli.main(["publish"]) == 1
    assert q.get(item["id"])["status"] == "approved"
    assert not any(c.request.url.endswith("/rest/posts") for c in responses.calls)


@responses.activate
def test_edit_after_approval_goes_back_to_pending(env, brief_file):
    mock_userinfo()
    q, item = add_draft(env, brief_file)
    items = q.load()
    items[0]["draft"]["text"] = GOOD_TEXT + " Sneaky edit."
    q.save(items)
    assert cli.main(["publish"]) == 0
    assert q.get(item["id"])["status"] == "pending"
    assert not any(c.request.url.endswith("/rest/posts") for c in responses.calls)


@responses.activate
def test_401_marks_failed_and_exit_3(env, brief_file):
    mock_userinfo()
    responses.post(API + "/rest/posts", status=401, json={"message": "The token has been revoked", "status": 401})
    q, item = add_draft(env, brief_file)
    assert cli.main(["publish"]) == 3
    assert q.get(item["id"])["status"] == "failed"


@responses.activate
def test_network_error_mid_post_is_not_retried(env, brief_file):
    mock_userinfo()
    responses.post(API + "/rest/posts", body=requests.ConnectionError("reset"))
    q, item = add_draft(env, brief_file)
    assert cli.main(["publish"]) == 1
    assert "check the profile" in q.get(item["id"])["error"]
    assert sum(c.request.url.endswith("/rest/posts") for c in responses.calls) == 1


@responses.activate
def test_linkedin_duplicate_response_marks_rejected(env, brief_file):
    mock_userinfo()
    responses.post(API + "/rest/posts", status=422, json={"message": "Content is a duplicate of urn:li:share:9"})
    q, item = add_draft(env, brief_file)
    assert cli.main(["publish"]) == 0
    assert q.get(item["id"])["status"] == "rejected"


@responses.activate
def test_dry_run_sends_nothing(env, brief_file, capsys):
    add_draft(env, brief_file)
    assert cli.main(["publish", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "[dry run] would POST /rest/posts" in out and '"author": "urn:li:person:782bbtaQ"' in out
    assert "AQVtoken" not in out
    assert len(responses.calls) == 0


def test_expired_token_stops_before_any_call(env, brief_file):
    (env / "tok.json").write_text(json.dumps({"access_token": "AQVtoken", "person_urn": ME, "expires_at": "1000"}))
    add_draft(env, brief_file)
    assert cli.main(["publish"]) == 3


def test_failed_checks_at_publish_time(env, brief_file, tmp_path):
    q, item = add_draft(env, brief_file)
    brief_file.write_text(BRIEF_TEXT.replace("300", "250"))   # the fact changed after approval
    run = publish_approved(q, PostedLog(tmp_path / "log.jsonl"), load_brief,
                           None, ME, dry_run=True, say=lambda s: None)
    assert run.outcomes[0].result == "blocked" and q.get(item["id"])["status"] == "failed"


def test_live_publish_without_client_raises(env, brief_file, tmp_path):
    q, _ = add_draft(env, brief_file)
    with pytest.raises(LinkedInError):
        publish_approved(q, PostedLog(tmp_path / "l.jsonl"), lambda p: None, None, ME, say=lambda s: None)


def test_cli_draft_offline_approve_reject_list_edit(env, brief_file, capsys, tmp_path):
    assert cli.main(["draft", str(brief_file), "--offline"]) == 0
    q = ApprovalQueue(env / "queue" / "queue.json")
    iid = q.load()[0]["id"]
    assert cli.main(["list"]) == 0 and iid in capsys.readouterr().out
    new_text = tmp_path / "t.txt"
    new_text.write_text(GOOD_TEXT)
    assert cli.main(["edit", iid, "--text-file", str(new_text), "--hashtags", "#Automation, Ops"]) == 0
    assert q.get(iid)["draft"]["hashtags"] == ["Automation", "Ops"]
    assert cli.main(["approve", iid, "--by", "Owner"]) == 0
    assert cli.main(["approve", iid, "--by", "Owner"]) == 2      # already approved
    assert cli.main(["reject", iid, "--reason", "changed my mind"]) == 0
    assert q.get(iid)["status"] == "rejected"
    assert cli.main(["review"]) == 0
    bad = tmp_path / "bad.txt"
    bad.write_text(GOOD_TEXT + " Revenue up 40%.")
    assert cli.main(["edit", iid, "--text-file", str(bad)]) == 2


def test_cli_draft_with_claude(env, brief_file, monkeypatch, capsys):
    from .conftest import FakeAnthropic
    fake = FakeAnthropic([{"text": GOOD_TEXT, "hashtags": ["Automation"], "facts_used": [0]}])
    monkeypatch.setattr(cli, "ClaudeDrafter", lambda model: ClaudeDrafter(model=model, client=fake))
    assert cli.main(["draft", str(brief_file)]) == 0
    assert "Claude tokens: 500 in, 200 out" in capsys.readouterr().out


@responses.activate
def test_whoami(env, capsys):
    mock_userinfo()
    assert cli.main(["whoami", "--live"]) == 0
    out = capsys.readouterr().out
    assert ME in out and "days left" in out and "AQVtoken" not in out


@responses.activate
def test_auth_full_flow(env, monkeypatch, capsys):
    responses.add_passthru("http://localhost:8798")
    responses.post(TOKEN_URL, json={"access_token": "AQVfresh", "expires_in": 5184000,
                                    "scope": "openid,profile,w_member_social"})
    mock_userinfo()
    monkeypatch.setattr(cli, "new_state", lambda: "fixedstate")

    def browser():
        time.sleep(0.4)
        urllib.request.urlopen("http://localhost:8798/callback?code=AQTQ&state=fixedstate", timeout=5)
    t = threading.Thread(target=browser)
    t.start()
    assert cli.main(["auth", "--no-browser", "--timeout", "10"]) == 0
    t.join(5)
    out = capsys.readouterr().out
    assert "Posting as urn:li:person:782bbtaQ" in out and "60 days" in out
    assert "AQVfresh" not in out and "csecret" not in out
    saved = json.loads((env / "tok.json").read_text())
    assert saved["access_token"] == "AQVfresh" and saved["person_urn"] == ME
