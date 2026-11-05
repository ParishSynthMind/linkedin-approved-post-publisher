"""LinkedIn HTTP is mocked with `responses`, using the request and response shapes from
LinkedIn's docs (Authorization Code Flow, /v2/userinfo, Posts API, error handling)."""
import json
import threading
import time
import urllib.parse
import urllib.request

import pytest
import requests
import responses

from li_publisher.linkedin import (AUTH_URL, TOKEN_URL, AuthError, DuplicatePost, LinkedInClient, LinkedInError,
                                   OAuthError, PermissionDenied, RateLimited, VersionError, authorization_url,
                                   escape_little_text, exchange_code, to_little_text, wait_for_callback)

TOKEN = "AQV" + "x" * 40
API = "https://api.linkedin.com"


def client(**kw):
    sleeps = []
    c = LinkedInClient(TOKEN, sleep=sleeps.append, **kw)
    return c, sleeps


def test_authorization_url_has_documented_params():
    url = authorization_url("cid", "http://localhost:8765/callback", "st4te")
    base, _, query = url.partition("?")
    q = dict(urllib.parse.parse_qsl(query))
    assert base == AUTH_URL == "https://www.linkedin.com/oauth/v2/authorization"
    assert q == {"response_type": "code", "client_id": "cid", "redirect_uri": "http://localhost:8765/callback",
                 "state": "st4te", "scope": "openid profile w_member_social"}
    assert "scope=openid%20profile%20w_member_social" in query   # space-delimited, URL-encoded


@responses.activate
def test_exchange_code_form_post():
    responses.post(TOKEN_URL, json={"access_token": TOKEN, "expires_in": 5184000,
                                    "scope": "email,openid,profile,w_member_social", "id_token": "x.y.z"})
    data = exchange_code("AQTQ", "cid", "secret", "http://localhost:8765/callback")
    assert data["expires_in"] == 5184000
    req = responses.calls[0].request
    assert req.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert dict(urllib.parse.parse_qsl(req.body)) == {
        "grant_type": "authorization_code", "code": "AQTQ", "client_id": "cid", "client_secret": "secret",
        "redirect_uri": "http://localhost:8765/callback"}


@responses.activate
def test_exchange_code_errors_never_echo_secret():
    responses.post(TOKEN_URL, status=400, json={
        "error": "invalid_redirect_uri",
        "error_description": "Unable to retrieve access token: appid/redirect uri/code verifier does not match"})
    with pytest.raises(OAuthError) as e:
        exchange_code("AQTQ", "cid", "supersecret", "http://localhost:8765/callback")
    assert "supersecret" not in str(e.value) and "redirect uri" in str(e.value)


@responses.activate
def test_exchange_code_missing_share_scope():
    responses.post(TOKEN_URL, json={"access_token": TOKEN, "expires_in": 5184000, "scope": "openid,profile"})
    with pytest.raises(OAuthError, match="Share on LinkedIn"):
        exchange_code("c", "cid", "s", "http://localhost:8765/callback")


def _hit(url):
    try:
        return urllib.request.urlopen(url, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


def _callback(query, expected_state="good"):
    redirect = "http://localhost:8799/callback"
    out = {}

    def run():
        try:
            out["code"] = wait_for_callback(redirect, expected_state, timeout=10)
        except OAuthError as e:
            out["error"] = str(e)
    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.3)
    status = _hit(redirect + "?" + query)
    t.join(5)
    return status, out


def test_callback_server_accepts_matching_state():
    status, out = _callback("code=AQTQ123&state=good")
    assert status == 200 and out == {"code": "AQTQ123"}


def test_callback_server_rejects_wrong_state_with_401():
    status, out = _callback("code=AQTQ123&state=forged")
    assert status == 401 and "state did not match" in out["error"]


def test_callback_server_user_cancelled():
    status, out = _callback("error=user_cancelled_authorize&error_description=The+user+cancelled&state=good")
    assert status == 400 and "user_cancelled_authorize" in out["error"]


def test_callback_server_refuses_non_local_redirect():
    with pytest.raises(OAuthError):
        wait_for_callback("https://example.com/callback", "s", timeout=1)


def test_little_text_escaping_and_hashtags():
    assert escape_little_text("a (b) [c] @d #e *f* _g_ ~h~ {i} <j> |k| \\l") == \
        "a \\(b\\) \\[c\\] \\@d \\#e \\*f\\* \\_g\\_ \\~h\\~ \\{i\\} \\<j\\> \\|k\\| \\\\l"
    assert to_little_text("Hi (all)", ["AI", "Ops"]) == "Hi \\(all\\)\n\n{hashtag|\\#|AI} {hashtag|\\#|Ops}"


@responses.activate
def test_userinfo_gives_person_urn_with_bearer_only():
    responses.get(API + "/v2/userinfo", json={"sub": "782bbtaQ", "name": "John Doe", "given_name": "John",
                                              "family_name": "Doe", "locale": "en-US"})
    c, _ = client()
    assert c.person_urn() == "urn:li:person:782bbtaQ"
    h = responses.calls[0].request.headers
    assert h["Authorization"] == f"Bearer {TOKEN}" and "Linkedin-Version" not in h


@responses.activate
def test_create_post_matches_posts_api():
    responses.post(API + "/rest/posts", status=201, headers={"x-restli-id": "urn:li:share:6844785523593134080"})
    c, _ = client()
    urn = c.create_post("urn:li:person:782bbtaQ", "Hello (world)", ["Automation"])
    assert urn == "urn:li:share:6844785523593134080"
    req = responses.calls[0].request
    assert req.headers["Linkedin-Version"] == "202610"
    assert req.headers["X-Restli-Protocol-Version"] == "2.0.0"
    assert req.headers["Content-Type"] == "application/json"
    assert json.loads(req.body) == {
        "author": "urn:li:person:782bbtaQ",
        "commentary": "Hello \\(world\\)\n\n{hashtag|\\#|Automation}",
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }


@responses.activate
def test_legacy_ugcposts_body():
    responses.post(API + "/v2/ugcPosts", status=201, headers={"X-RestLi-Id": "urn:li:share:1"})
    c, _ = client(api="ugcposts")
    assert c.create_post("urn:li:person:abc", "Hello", ["AI"]) == "urn:li:share:1"
    body = json.loads(responses.calls[0].request.body)
    assert body["specificContent"]["com.linkedin.ugc.ShareContent"] == {
        "shareCommentary": {"text": "Hello\n\n#AI"}, "shareMediaCategory": "NONE"}
    assert body["visibility"] == {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}


def test_refuses_company_page_author():
    c, _ = client()
    with pytest.raises(LinkedInError, match="only posts as a member"):
        c.post_body("urn:li:organization:5515715", "x", [])


def test_bad_version_rejected_locally():
    with pytest.raises(VersionError):
        LinkedInClient(TOKEN, version="2026-10")


@responses.activate
def test_401_is_auth_error_and_token_not_in_message():
    responses.post(API + "/rest/posts", status=401,
                   json={"message": "Expired access token", "serviceErrorCode": 65601, "status": 401})
    c, _ = client()
    with pytest.raises(AuthError) as e:
        c.create_post("urn:li:person:abc", "x", [])
    assert TOKEN not in str(e.value) and "Expired access token" in str(e.value)


@responses.activate
def test_403_permission():
    responses.post(API + "/rest/posts", status=403, json={"message": "Not enough permissions", "status": 403})
    with pytest.raises(PermissionDenied):
        client()[0].create_post("urn:li:person:abc", "x", [])


@responses.activate
def test_429_retries_then_succeeds():
    responses.post(API + "/rest/posts", status=429,
                   json={"message": "Resource level throttle limit for calls to this resource is reached.",
                         "status": 429})
    responses.post(API + "/rest/posts", status=429, headers={"Retry-After": "7"}, json={"status": 429})
    responses.post(API + "/rest/posts", status=201, headers={"x-restli-id": "urn:li:share:2"})
    c, sleeps = client()
    assert c.create_post("urn:li:person:abc", "x", []) == "urn:li:share:2"
    assert sleeps == [2.0, 7.0]


@responses.activate
def test_429_gives_up_with_rate_limited():
    for _ in range(4):
        responses.post(API + "/rest/posts", status=429, json={"status": 429})
    c, sleeps = client()
    with pytest.raises(RateLimited):
        c.create_post("urn:li:person:abc", "x", [])
    assert len(sleeps) == 3 and len(responses.calls) == 4


@responses.activate
def test_500_retried_and_409_write_conflict_retried():
    responses.post(API + "/rest/posts", status=500, json={"status": 500})
    responses.post(API + "/rest/posts", status=409, json={"code": "CONFLICT", "message": "A write conflict occurred."})
    responses.post(API + "/rest/posts", status=201, headers={"x-restli-id": "urn:li:share:3"})
    assert client()[0].create_post("urn:li:person:abc", "x", []) == "urn:li:share:3"


@responses.activate
def test_duplicate_rejection():
    responses.post(API + "/rest/posts", status=422,
                   json={"message": "Content is a duplicate of urn:li:share:99", "status": 422})
    with pytest.raises(DuplicatePost):
        client()[0].create_post("urn:li:person:abc", "x", [])


@responses.activate
def test_version_errors():
    responses.post(API + "/rest/posts", status=426,
                   json={"status": 426, "code": "NONEXISTENT_VERSION",
                         "message": "Requested version 202401 is not active"})
    with pytest.raises(VersionError):
        client()[0].create_post("urn:li:person:abc", "x", [])
    responses.post(API + "/rest/posts", status=400,
                   json={"status": 400, "code": "VERSION_MISSING", "message": "A version must be present."})
    with pytest.raises(VersionError):
        client()[0].create_post("urn:li:person:abc", "x", [])


@responses.activate
def test_201_without_id_header_is_an_error():
    responses.post(API + "/rest/posts", status=201)
    with pytest.raises(LinkedInError, match="x-restli-id"):
        client()[0].create_post("urn:li:person:abc", "x", [])


@responses.activate
def test_network_error_propagates():
    responses.post(API + "/rest/posts", body=requests.ConnectionError("down"))
    with pytest.raises(requests.ConnectionError):
        client()[0].create_post("urn:li:person:abc", "x", [])
