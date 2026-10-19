"""Seedr sessions live in an encrypted cookie, so they survive backend restarts.

The in-memory ``_seedr_sessions`` dict is only a cache. These tests simulate a
restart by clearing it and checking that the browser's cookie is enough to
restore the connection, the CSRF token and a disconnect.
"""

import json
import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import main

PAT = "pat_" + "a1b2c3d4e5" * 4


@pytest.fixture()
def client(monkeypatch):
    async def accept_any_pat(token):
        return None

    monkeypatch.setattr(main, "_validate_seedr_pat", accept_any_pat)
    main._seedr_sessions.clear()
    test_client = TestClient(main.app)
    csrf = test_client.get("/api/seedr/session").json()["csrfToken"]
    test_client.headers["X-Torrent-Studio-CSRF"] = csrf
    yield test_client
    main._seedr_sessions.clear()


def _connect(client):
    response = client.post("/api/seedr/connect/pat", json={"pat": PAT})
    assert response.status_code == 200
    assert response.json()["connected"] is True


def _restart_backend():
    # A restart empties process memory; only the browser's cookie remains.
    main._seedr_sessions.clear()


def test_connected_session_survives_backend_restart(client):
    _connect(client)
    assert client.get("/api/seedr/session").json()["connected"] is True

    _restart_backend()

    after = client.get("/api/seedr/session").json()
    assert after["connected"] is True
    assert after["csrfToken"] == client.headers["X-Torrent-Studio-CSRF"]


def test_csrf_token_still_valid_after_restart(client):
    _connect(client)
    _restart_backend()
    response = client.post("/api/seedr/connect/disconnect")
    assert response.status_code == 200


def test_disconnect_survives_backend_restart(client):
    _connect(client)
    assert client.post("/api/seedr/connect/disconnect").status_code == 200
    _restart_backend()
    assert client.get("/api/seedr/session").json()["connected"] is False


def test_cookie_is_httponly_and_hides_the_token(client):
    _connect(client)
    response = client.get("/api/seedr/session")
    set_cookie = response.headers["set-cookie"]
    assert "httponly" in set_cookie.lower()
    assert PAT not in set_cookie


def test_cookie_round_trip():
    session = main._seedr_empty_session()
    session["access_token"] = PAT
    session["refresh_token"] = "refresh-token-value"
    session["auth_mode"] = "pat"

    cookie = main._seedr_session_cookie_value("session-1", session)
    restored = main._seedr_restore_session(cookie)

    assert restored is not None
    session_id, restored_session = restored
    assert session_id == "session-1"
    assert restored_session["csrf_token"] == session["csrf_token"]
    assert restored_session["access_token"] == main.normalize_seedr_token(PAT)
    assert restored_session["refresh_token"] == "refresh-token-value"
    assert restored_session["auth_mode"] == "pat"


def test_tampered_cookie_is_rejected():
    cookie = main._seedr_session_cookie_value("session-1", main._seedr_empty_session())
    flipped = "A" if cookie[-5] != "A" else "B"
    assert main._seedr_restore_session(cookie[:-5] + flipped + cookie[-4:]) is None


def test_cookie_made_with_another_secret_is_rejected():
    payload = {"v": 1, "sid": "session-1", "csrf": "csrf", "last": time.time()}
    foreign = Fernet(Fernet.generate_key()).encrypt(json.dumps(payload).encode("utf-8"))
    assert main._seedr_restore_session("v1." + foreign.decode("ascii")) is None


def test_expired_session_is_rejected(monkeypatch):
    monkeypatch.setattr(main, "SEEDR_SESSION_TTL_SECONDS", 10)
    payload = {"v": 1, "sid": "session-1", "csrf": "csrf", "last": time.time() - 100}
    stale = main._seedr_fernet.encrypt(json.dumps(payload).encode("utf-8"))
    assert main._seedr_restore_session("v1." + stale.decode("ascii")) is None


def test_garbage_cookie_is_ignored(client):
    client.cookies.set(main.SEEDR_SESSION_COOKIE, "v1.not-a-valid-token")
    response = client.get("/api/seedr/session")
    assert response.status_code == 200
    assert response.json()["connected"] is False
