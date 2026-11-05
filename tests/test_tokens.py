import os
import stat
import subprocess
import time

import pytest

from li_publisher.tokens import FileTokenStore, KeychainTokenStore, TokenStoreError, days_left, make_store


class FakeSecurity:
    def __init__(self):
        self.items = {}
        self.calls = []

    def __call__(self, cmd, input=None, capture_output=True, text=True):
        self.calls.append((cmd, input))
        if cmd[1] == "find-generic-password":
            value = self.items.get(cmd[cmd.index("-s") + 1])
            return subprocess.CompletedProcess(cmd, 0 if value else 44, stdout=(value or "") + "\n", stderr="")
        if cmd == ["security", "-i"]:
            parts = input.split('"')
            self.items[parts[3]] = parts[5]
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(cmd)


def test_keychain_store_roundtrip_keeps_token_off_command_line():
    sec = FakeSecurity()
    store = KeychainTokenStore(runner=sec)
    store.save(access_token="AQVabc-123_x", expires_at="1790000000", person_urn="urn:li:person:782bbtaQ")
    assert store.get("access_token") == "AQVabc-123_x"
    assert store.get("person_urn") == "urn:li:person:782bbtaQ"
    for cmd, _ in sec.calls:
        assert "AQVabc-123_x" not in " ".join(cmd)
    assert sec.calls[0][1].startswith('add-generic-password -U -a "neo" -s "linkedin-access-token"')


def test_keychain_missing_item():
    with pytest.raises(TokenStoreError, match="linkedin-client-secret"):
        KeychainTokenStore(runner=FakeSecurity()).get("client_secret")


def test_refuses_injection_and_unwritable_keys(tmp_path):
    store = KeychainTokenStore(runner=FakeSecurity())
    with pytest.raises(TokenStoreError):
        store.save(access_token='abc" ; delete-keychain "x')
    with pytest.raises(TokenStoreError):
        store.save(client_secret="abc")
    with pytest.raises(TokenStoreError):
        FileTokenStore(tmp_path / "t.json").save(access_token="")


def test_file_store_is_private_and_wins_over_env(tmp_path):
    path = tmp_path / "t.json"
    store = FileTokenStore(path, environ={"LINKEDIN_CLIENT_ID": "cid", "LINKEDIN_ACCESS_TOKEN": "old"})
    assert store.get("access_token") == "old"
    store.save(access_token="new.token", expires_at=str(int(time.time()) + 86400 * 10))
    assert store.get("access_token") == "new.token" and store.get("client_id") == "cid"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert 9.9 < days_left(store) <= 10
    with pytest.raises(TokenStoreError, match="LINKEDIN_CLIENT_SECRET"):
        store.get("client_secret")


def test_days_left_without_token(tmp_path):
    assert days_left(FileTokenStore(tmp_path / "none.json", environ={})) < 0


def test_make_store(monkeypatch, tmp_path):
    monkeypatch.setenv("LINKEDIN_TOKEN_FILE", str(tmp_path / "x.json"))
    assert isinstance(make_store("file"), FileTokenStore)
    assert isinstance(make_store("keychain"), KeychainTokenStore)
    with pytest.raises(TokenStoreError):
        make_store("s3")
