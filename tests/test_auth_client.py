"""Verify ``KhdpAuthClient`` against mocked KHDP endpoints."""

from pathlib import Path

import pytest

from khdp import oauth as oauth_mod
from khdp.config import Config
from khdp.oauth import AuthError, KhdpAuthClient


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        app_id="635e3da0-ec5a-442e-a416-0824fae7a9e2",
        api_base="https://api.example/_api",
        token_dir=tmp_path,
        use_keyring=False,
    )


# ── PKCE login ────────────────────────────────────────────────────────


def test_pkce_login_success(
    config: Config, httpx_mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    httpx_mock.add_response(
        url="https://api.example/_api/oauth/token",
        method="POST",
        json={
            "accessToken": "AT",
            "refreshToken": "RT",
            "tokenType": "Bearer",
            "expires_in": 3600,
        },
    )

    def fake_wait(server, *, timeout):
        server.server_close()
        return {"code": "ABCDEFGHIJ", "state": "expected"}

    monkeypatch.setattr(oauth_mod, "_wait_for_callback", fake_wait)
    monkeypatch.setattr(oauth_mod, "_generate_state", lambda: "expected")

    opened: list[str] = []
    with KhdpAuthClient(config) as client:
        tokens = client.pkce_login(open_browser=opened.append)

    assert tokens.access_token == "AT"
    assert tokens.refresh_token == "RT"
    assert tokens.app_id == config.app_id
    assert tokens.is_expired is False
    assert opened, "browser opener should have been called once"
    assert "appId=" in opened[0]
    assert "codeChallenge=" in opened[0]
    assert "code_challenge_method".replace("_", "C") not in opened[0]  # camelCase
    assert "codeChallengeMethod=S256" in opened[0]


def test_pkce_login_state_skipped_when_callback_omits_it(
    config: Config, httpx_mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    httpx_mock.add_response(
        url="https://api.example/_api/oauth/token",
        method="POST",
        json={"accessToken": "AT", "refreshToken": "RT", "expires_in": 60},
    )
    monkeypatch.setattr(
        oauth_mod, "_wait_for_callback",
        lambda server, *, timeout: (server.server_close() or {"code": "ABCDEFGHIJ"}),
    )
    with KhdpAuthClient(config) as client:
        tokens = client.pkce_login(open_browser=lambda _u: True)
    assert tokens.access_token == "AT"


def test_pkce_login_callback_without_code_raises(
    config: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        oauth_mod, "_wait_for_callback",
        lambda server, *, timeout: (
            server.server_close() or {"error": "access_denied"}
        ),
    )
    with KhdpAuthClient(config) as client, pytest.raises(AuthError) as exc:
        client.pkce_login(open_browser=lambda _u: True)
    assert "access_denied" in str(exc.value)


def test_pkce_login_requires_app_id(tmp_path: Path) -> None:
    cfg = Config(token_dir=tmp_path, use_keyring=False)
    with KhdpAuthClient(cfg) as client, pytest.raises(AuthError) as exc:
        client.pkce_login(open_browser=lambda _u: True)
    assert "app_id" in str(exc.value)


# ── Refresh ───────────────────────────────────────────────────────────


def test_refresh_success(config: Config, httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.example/_api/oauth/refresh-token",
        method="POST",
        json={
            "accessToken": "new-AT",
            "refreshToken": "new-RT",
            "tokenType": "Bearer",
            "expires_in": 1200,
        },
    )
    with KhdpAuthClient(config) as client:
        tokens = client.refresh("old-RT")
    assert tokens.access_token == "new-AT"
    assert tokens.refresh_token == "new-RT"


def test_refresh_invalid_token_returns_authoritative_message(
    config: Config, httpx_mock,
) -> None:
    httpx_mock.add_response(
        url="https://api.example/_api/oauth/refresh-token",
        method="POST",
        status_code=400,
        json={"statusCode": 400, "message": "invalid_grant"},
    )
    with KhdpAuthClient(config) as client, pytest.raises(AuthError) as exc:
        client.refresh("revoked")
    assert "invalid_grant" in str(exc.value)
