import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture()
def client():
    # Deliberately not used as a context manager: that would run the startup
    # hooks (cache warmers, cleanup worker) which are not wanted in unit tests.
    return TestClient(main.app)


class _FakeResponse:
    status_code = 201

    def json(self):
        return {"html_url": "https://github.com/example/repo/issues/1"}


class _FakeGitHubClient:
    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeGitHubClient.calls.append(json)
        return _FakeResponse()


@pytest.fixture()
def feedback_env(monkeypatch):
    _FakeGitHubClient.calls = []
    monkeypatch.setattr(main, "GITHUB_FEEDBACK_TOKEN", "test-token")
    monkeypatch.setattr(main, "GITHUB_FEEDBACK_REPO", "example/repo")
    monkeypatch.setattr(main, "_InstrumentedAsyncClient", _FakeGitHubClient)
    monkeypatch.setattr(main, "FEEDBACK_RATE_LIMIT", 2)
    monkeypatch.setattr(main, "FEEDBACK_GLOBAL_RATE_LIMIT", 100)
    main._feedback_hits.clear()
    main._feedback_global_hits.clear()
    yield
    main._feedback_hits.clear()
    main._feedback_global_hits.clear()


def _feedback(client, ip="203.0.113.10", **overrides):
    payload = {"type": "bug", "message": "Something is broken here", "name": "Tester"}
    payload.update(overrides)
    return client.post("/api/feedback", json=payload, headers={"X-Forwarded-For": ip})


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_cache_report_hidden_unless_debug_enabled(client, monkeypatch):
    monkeypatch.setattr(main, "DEBUG_ENDPOINTS_ENABLED", False)
    assert client.get("/api/cache/report").status_code == 404


def test_session_cookie_defaults_to_lax():
    assert main.SEEDR_SESSION_SAMESITE == "lax"


def test_feedback_rejects_invalid_type(client, feedback_env):
    assert _feedback(client, type="spam").status_code == 400


def test_feedback_rejects_short_message(client, feedback_env):
    assert _feedback(client, message="hey").status_code == 400


def test_feedback_requires_review_rating(client, feedback_env):
    assert _feedback(client, type="review").status_code == 400


def test_feedback_not_configured(client, feedback_env, monkeypatch):
    monkeypatch.setattr(main, "GITHUB_FEEDBACK_TOKEN", "")
    assert _feedback(client).status_code == 503


def test_feedback_is_submitted_with_real_newlines(client, feedback_env):
    response = _feedback(client)
    assert response.status_code == 200
    assert response.json()["submitted"] is True
    body = _FakeGitHubClient.calls[0]["body"]
    assert "\n" in body
    assert "\\n" not in body


def test_feedback_does_not_leak_visitor_ip(client, feedback_env):
    _feedback(client, ip="198.51.100.77")
    assert "198.51.100.77" not in _FakeGitHubClient.calls[0]["body"]


def test_feedback_per_client_rate_limit(client, feedback_env):
    assert _feedback(client, ip="203.0.113.1").status_code == 200
    assert _feedback(client, ip="203.0.113.1").status_code == 200
    assert _feedback(client, ip="203.0.113.1").status_code == 429
    # A different client is not affected by the first client's limit.
    assert _feedback(client, ip="203.0.113.2").status_code == 200


def test_feedback_global_rate_limit(client, feedback_env, monkeypatch):
    monkeypatch.setattr(main, "FEEDBACK_GLOBAL_RATE_LIMIT", 1)
    assert _feedback(client, ip="203.0.113.1").status_code == 200
    assert _feedback(client, ip="203.0.113.2").status_code == 429
