"""LinkedIn's official APIs only: OAuth 2.0 (three-legged), /v2/userinfo, and the Posts API.

Built against the docs on learn.microsoft.com/linkedin (Share on LinkedIn, Posts API,
Authorization Code Flow, Sign In with LinkedIn using OpenID Connect, Little Text Format).
"""
from __future__ import annotations

import http.server
import re
import secrets
import time
import urllib.parse
from typing import Callable, Optional

import requests

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
API_BASE = "https://api.linkedin.com"
# openid + profile come from "Sign In with LinkedIn using OpenID Connect" (needed for the member id),
# w_member_social from "Share on LinkedIn" (needed to post as that member).
SCOPES = ("openid", "profile", "w_member_social")
DEFAULT_VERSION = "202610"   # Linkedin-Version header, YYYYMM; LinkedIn has no unversioned calls
RESERVED = set("|{}@[]()<>#\\*_~")
PERSON_URN = re.compile(r"^urn:li:person:[A-Za-z0-9_-]+$")
RETRY_STATUSES = {429, 500, 502, 503, 504}


class LinkedInError(RuntimeError):
    exit_code = 1


class AuthError(LinkedInError):
    """401: token missing, expired or revoked. Fix: run `auth` again."""
    exit_code = 3


class PermissionDenied(LinkedInError):
    """403: the app lacks the product or scope for this call."""
    exit_code = 3


class RateLimited(LinkedInError):
    """429 still coming back after the retries. LinkedIn's limits reset at midnight UTC."""
    exit_code = 4


class VersionError(LinkedInError):
    """400 VERSION_MISSING or 426 NONEXISTENT_VERSION: set LINKEDIN_VERSION to an active YYYYMM."""
    exit_code = 5


class DuplicatePost(LinkedInError):
    """LinkedIn refused the post as a duplicate of one already on the profile."""
    exit_code = 6


class OAuthError(LinkedInError):
    exit_code = 3


def _error_text(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or "")[:200]
    msg = body.get("message") or body.get("error_description") or body.get("error") or ""
    code = body.get("code") or body.get("serviceErrorCode") or ""
    return f"{code} {msg}".strip()[:300]


# ---------- OAuth 2.0, three-legged ----------

def new_state() -> str:
    return secrets.token_urlsafe(24)


def authorization_url(client_id: str, redirect_uri: str, state: str, scopes=SCOPES) -> str:
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": " ".join(scopes),
    }, quote_via=urllib.parse.quote)
    return f"{AUTH_URL}?{query}"


def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str,
                  session: Optional[requests.Session] = None, token_url: str = TOKEN_URL) -> dict:
    """Swap the one-time code (valid 30 minutes) for an access token. Returns LinkedIn's JSON:
    access_token, expires_in (60 days today), scope, and id_token when openid was granted."""
    s = session or requests.Session()
    resp = s.post(token_url, data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    if resp.status_code != 200:
        raise OAuthError(f"token exchange failed ({resp.status_code}): {_error_text(resp)}")
    data = resp.json()
    if not data.get("access_token") or not data.get("expires_in"):
        raise OAuthError("token exchange answered 200 but without access_token/expires_in")
    granted = set((data.get("scope") or "").replace(",", " ").split())
    if granted and "w_member_social" not in granted:
        raise OAuthError("LinkedIn did not grant w_member_social; add the 'Share on LinkedIn' product to the app")
    return data


def wait_for_callback(redirect_uri: str, expected_state: str, timeout: float = 300,
                      ready: Optional[Callable[[], None]] = None) -> str:
    """Run a one-shot local web server on the redirect URI's port and return the `code`.
    A wrong `state` is answered with 401 and rejected, as LinkedIn's docs require (CSRF guard)."""
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.hostname not in ("localhost", "127.0.0.1"):
        raise OAuthError("the built-in callback server only listens on localhost")
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server naming)
            url = urllib.parse.urlparse(self.path)
            if url.path != parsed.path:
                self.send_response(404)
                self.end_headers()
                return
            q = dict(urllib.parse.parse_qsl(url.query))
            if q.get("state") != expected_state:
                result["error"] = "state did not match, possible forged request; nothing was saved"
                status, text = 401, "State mismatch. Sign-in rejected."
            elif "error" in q:
                result["error"] = f"LinkedIn returned {q['error']}: {q.get('error_description', '')}"
                status, text = 400, "LinkedIn sign-in was cancelled or refused. You can close this tab."
            elif q.get("code"):
                result["code"] = q["code"]
                status, text = 200, "Signed in. You can close this tab and go back to the terminal."
            else:
                result["error"] = "callback had no code"
                status, text = 400, "No code received."
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(text.encode())

        def log_message(self, *args):  # keep the code out of the terminal
            pass

    server = http.server.HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    server.timeout = 1
    deadline = time.monotonic() + timeout
    if ready:
        ready()
    try:
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "code" in result:
        return result["code"]
    raise OAuthError(result.get("error", "timed out waiting for the LinkedIn sign-in"))


# ---------- Little Text Format ----------

def escape_little_text(text: str) -> str:
    """Backslash-escape every reserved character, as the Little Text Format requires."""
    return "".join("\\" + ch if ch in RESERVED else ch for ch in text)


def to_little_text(body: str, hashtags) -> str:
    out = escape_little_text(body.strip())
    if hashtags:
        out += "\n\n" + " ".join("{hashtag|\\#|%s}" % h for h in hashtags)
    return out


# ---------- REST client ----------

class LinkedInClient:
    def __init__(self, access_token: str, version: str = DEFAULT_VERSION, api: str = "posts",
                 session: Optional[requests.Session] = None, base: str = API_BASE,
                 max_retries: int = 3, sleep: Callable[[float], None] = time.sleep):
        if not re.fullmatch(r"\d{6}", version):
            raise VersionError("LINKEDIN_VERSION must be YYYYMM, for example 202610")
        if api not in ("posts", "ugcposts"):
            raise ValueError("api must be 'posts' or 'ugcposts'")
        self._token = access_token
        self.version = version
        self.api = api
        self.s = session or requests.Session()
        self.base = base.rstrip("/")
        self.max_retries = max_retries
        self.sleep = sleep

    def headers(self, versioned: bool = True) -> dict:
        h = {"Authorization": f"Bearer {self._token}", "X-Restli-Protocol-Version": "2.0.0"}
        if versioned:
            h["Linkedin-Version"] = self.version
        return h

    def _request(self, method: str, path: str, versioned: bool = True, **kw) -> requests.Response:
        for attempt in range(self.max_retries + 1):
            resp = self.s.request(method, self.base + path, headers=self.headers(versioned), timeout=30, **kw)
            if resp.status_code in RETRY_STATUSES or (resp.status_code == 409 and "duplicate" not in resp.text.lower()):
                if attempt < self.max_retries:
                    self.sleep(self._wait(resp, attempt))
                    continue
                if resp.status_code == 429:
                    raise RateLimited("LinkedIn is still rate limiting after retries; limits reset at midnight UTC")
                raise LinkedInError(f"LinkedIn kept failing ({resp.status_code}): {_error_text(resp)}")
            self._raise_for(resp)
            return resp
        raise LinkedInError("unreachable")

    @staticmethod
    def _wait(resp: requests.Response, attempt: int) -> float:
        # LinkedIn does not document Retry-After, but honour it if it is ever sent.
        try:
            return min(60.0, float(resp.headers.get("Retry-After", "")))
        except ValueError:
            return min(60.0, 2.0 ** (attempt + 1))

    @staticmethod
    def _raise_for(resp: requests.Response) -> None:
        if resp.status_code < 400:
            return
        detail = _error_text(resp)
        if resp.status_code == 401:
            raise AuthError(f"LinkedIn said 401 ({detail}); the token is missing, expired or revoked, run `auth`")
        if resp.status_code == 403:
            raise PermissionDenied(f"LinkedIn said 403 ({detail}); check the app has the product and scope")
        if resp.status_code == 426 or "VERSION_MISSING" in detail or "NONEXISTENT_VERSION" in detail:
            raise VersionError(f"LinkedIn rejected the API version ({detail}); set LINKEDIN_VERSION to an active month")
        if resp.status_code in (409, 422) and "duplicate" in detail.lower():
            raise DuplicatePost(f"LinkedIn refused the post as a duplicate ({detail})")
        raise LinkedInError(f"LinkedIn said {resp.status_code}: {detail}")

    def userinfo(self) -> dict:
        return self._request("GET", "/v2/userinfo", versioned=False).json()

    def person_urn(self) -> str:
        sub = self.userinfo().get("sub")
        if not sub:
            raise AuthError("userinfo returned no member id; the token needs the openid and profile scopes")
        urn = f"urn:li:person:{sub}"
        if not PERSON_URN.match(urn):
            raise LinkedInError("userinfo returned an unexpected member id")
        return urn

    def post_body(self, author_urn: str, body: str, hashtags) -> dict:
        if not PERSON_URN.match(author_urn):
            raise LinkedInError("this tool only posts as a member (urn:li:person), never as a company page")
        if self.api == "ugcposts":
            text = body.strip() + ("\n\n" + " ".join("#" + h for h in hashtags) if hashtags else "")
            return {
                "author": author_urn,
                "lifecycleState": "PUBLISHED",
                "specificContent": {"com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text}, "shareMediaCategory": "NONE"}},
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
            }
        return {
            "author": author_urn,
            "commentary": to_little_text(body, hashtags),
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                             "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }

    def create_post(self, author_urn: str, body: str, hashtags) -> str:
        """Publish a text post. Returns the new post's URN from the x-restli-id header."""
        payload = self.post_body(author_urn, body, hashtags)
        if self.api == "ugcposts":
            resp = self._request("POST", "/v2/ugcPosts", versioned=False, json=payload)
        else:
            resp = self._request("POST", "/rest/posts", json=payload)
        if resp.status_code != 201:
            raise LinkedInError(f"expected 201 Created, got {resp.status_code}")
        urn = resp.headers.get("x-restli-id") or resp.headers.get("X-RestLi-Id") or ""
        if not urn:
            raise LinkedInError("LinkedIn answered 201 but sent no x-restli-id; check the profile by hand")
        return urn


def post_url(urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{urn}/"
