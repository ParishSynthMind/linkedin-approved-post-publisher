"""Where the LinkedIn credentials live.

Five values: client_id, client_secret (set once from the developer portal), and access_token,
expires_at, person_urn (written after the OAuth sign-in). LinkedIn access tokens last 60 days and
self-serve apps get no refresh token, so when one expires the owner signs in again (`auth`).

- KeychainTokenStore: macOS Keychain generic-password items (used on the build machine).
- FileTokenStore: environment variables for the app keys plus a 0600 JSON file for the token
  (Linux servers, containers).
Token values are never printed, logged or put in an error message.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional, Protocol

KEYS = ("client_id", "client_secret", "access_token", "expires_at", "person_urn")
WRITABLE = ("access_token", "expires_at", "person_urn")
ENV_NAMES = {k: f"LINKEDIN_{k.upper()}" for k in KEYS}
SAFE_VALUE = re.compile(r"^[A-Za-z0-9._\-~+/=:]+$")


class TokenStoreError(RuntimeError):
    pass


class TokenStore(Protocol):
    def get(self, key: str) -> str: ...

    def save(self, **values: str) -> None: ...


def _check(key: str, value: str) -> None:
    if key not in WRITABLE:
        raise TokenStoreError(f"{key} is not something this tool writes")
    # Anything outside the expected characters is refused rather than escaped, so a value can
    # never break out of the Keychain command.
    if not value or not SAFE_VALUE.match(value):
        raise TokenStoreError(f"refusing to store a {key} with unexpected characters")


class KeychainTokenStore:
    """Generic-password items named <prefix>client-id, <prefix>access-token and so on."""

    def __init__(self, account: str = "neo", prefix: str = "linkedin-", runner=subprocess.run):
        self.account = account
        self.prefix = prefix
        self.run = runner

    def item(self, key: str) -> str:
        return self.prefix + key.replace("_", "-")

    def get(self, key: str) -> str:
        proc = self.run(["security", "find-generic-password", "-a", self.account, "-s", self.item(key), "-w"],
                        capture_output=True, text=True)
        value = (proc.stdout or "").strip()
        if proc.returncode != 0 or not value:
            raise TokenStoreError(f"Keychain item {self.item(key)!r} (account {self.account!r}) not found")
        return value

    def save(self, **values: str) -> None:
        for key, value in values.items():
            _check(key, value)
        for key, value in values.items():
            # `security -i` reads the command from stdin, so the token never shows in the process list.
            cmd = f'add-generic-password -U -a "{self.account}" -s "{self.item(key)}" -w "{value}"\n'
            proc = self.run(["security", "-i"], input=cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise TokenStoreError(f"could not write {key} to the Keychain")


class FileTokenStore:
    """App keys from LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET; the token in a 0600 JSON file."""

    def __init__(self, path: str | Path = ".linkedin_token.json", environ=None):
        self.path = Path(path)
        self.environ = os.environ if environ is None else environ

    def _file(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {}

    def get(self, key: str) -> str:
        value = self._file().get(key) or self.environ.get(ENV_NAMES[key], "")
        if not value:
            raise TokenStoreError(f"{ENV_NAMES[key]} is not set and {self.path} has no {key}")
        return value

    def save(self, **values: str) -> None:
        for key, value in values.items():
            _check(key, value)
        data = self._file()
        data.update(values)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.path)  # atomic: a crash mid-write can't leave half a token


def days_left(store: TokenStore, now: Optional[float] = None) -> float:
    """Days until the stored access token expires (negative if it already has)."""
    try:
        expires_at = float(store.get("expires_at"))
    except (TokenStoreError, ValueError):
        return -1.0
    return (expires_at - (time.time() if now is None else now)) / 86400


def make_store(kind: str = "") -> TokenStore:
    kind = kind or os.environ.get("LINKEDIN_TOKEN_STORE", "keychain")
    if kind == "keychain":
        return KeychainTokenStore(account=os.environ.get("LINKEDIN_KEYCHAIN_ACCOUNT", "neo"))
    if kind == "file":
        return FileTokenStore(os.environ.get("LINKEDIN_TOKEN_FILE", ".linkedin_token.json"))
    raise TokenStoreError("LINKEDIN_TOKEN_STORE must be keychain or file")
