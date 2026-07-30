import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main


@pytest.fixture(autouse=True)
def reset_direct_auth(monkeypatch):
    monkeypatch.setattr(main, "BASIC_AUTH_USERNAME", "seth")
    monkeypatch.setattr(main, "BASIC_AUTH_PASSWORD", "correct-horse")
    main._direct_sessions.clear()
    main._login_failures.clear()
    main._login_lockouts.clear()
    yield
    main._direct_sessions.clear()
    main._login_failures.clear()
    main._login_lockouts.clear()


@pytest.fixture
def client():
    test_client = TestClient(main.app, base_url="https://replicarr.test")
    yield test_client
    test_client.close()


def test_direct_page_redirects_to_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "./login"
    assert "WWW-Authenticate" not in response.headers


def test_login_page_has_password_manager_fields(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert 'autocomplete="username"' in response.text
    assert 'autocomplete="current-password"' in response.text
    assert 'method="post"' in response.text


def test_successful_login_sets_secure_session_and_allows_direct_access(client):
    response = client.post(
        "/login",
        data={"username": "seth", "password": "correct-horse"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "./"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "Max-Age=604800" in cookie

    assert client.get("/").status_code == 200
    assert client.get("/api/auth/session").json() == {"direct": True}


def test_invalid_login_is_generic_and_does_not_create_session(client):
    response = client.post(
        "/login",
        data={"username": "seth", "password": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "./login?error=invalid"
    assert not main._direct_sessions


def test_plain_http_login_is_refused():
    http_client = TestClient(main.app, base_url="http://replicarr.test")
    try:
        response = http_client.post(
            "/login",
            data={"username": "seth", "password": "correct-horse"},
            follow_redirects=False,
        )
    finally:
        http_client.close()
    assert response.status_code == 303
    assert response.headers["location"] == "./login?error=https"
    assert not main._direct_sessions


def test_repeated_failures_trigger_lockout(client, monkeypatch):
    monkeypatch.setattr(main, "LOGIN_MAX_FAILURES", 2)
    for expected in ("invalid", "locked"):
        response = client.post(
            "/login",
            data={"username": "seth", "password": "wrong"},
            follow_redirects=False,
        )
        assert response.headers["location"] == f"./login?error={expected}"

    correct = client.post(
        "/login",
        data={"username": "seth", "password": "correct-horse"},
        follow_redirects=False,
    )
    assert correct.headers["location"] == "./login?error=locked"


def test_logout_invalidates_session(client):
    client.post("/login", data={"username": "seth", "password": "correct-horse"})
    assert client.get("/").status_code == 200
    assert client.post("/logout").json() == {"ok": True}
    assert client.get("/", follow_redirects=False).status_code == 303


def test_cross_site_state_change_is_refused(client):
    client.post("/login", data={"username": "seth", "password": "correct-horse"})
    response = client.post("/logout", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert client.get("/").status_code == 200


def test_direct_access_remains_disabled_without_credentials(client, monkeypatch):
    monkeypatch.setattr(main, "BASIC_AUTH_USERNAME", "")
    monkeypatch.setattr(main, "BASIC_AUTH_PASSWORD", "")
    response = client.get("/")
    assert response.status_code == 403
    assert "Direct access is disabled" in response.json()["detail"]


def test_home_assistant_ingress_still_bypasses_direct_login(client, monkeypatch):
    monkeypatch.setattr(main, "BASIC_AUTH_USERNAME", "")
    monkeypatch.setattr(main, "BASIC_AUTH_PASSWORD", "")
    response = client.get("/api/auth/session", headers={"X-Ingress-Path": "/api/hassio_ingress/test"})
    assert response.status_code == 200
    assert response.json() == {"direct": False}
