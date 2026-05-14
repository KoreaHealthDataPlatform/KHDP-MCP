"""Session-level behavior -- auto refresh and authed_request header injection."""

import time
from pathlib import Path

import pytest

from khdp.config import Config
from khdp.oauth import KhdpAuthClient, TokenSet
from khdp.session import Session
from khdp.token_store import TokenStore


@pytest.fixture
def session(tmp_path: Path) -> Session:
    cfg = Config(
        app_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        redirect_url="https://example.org/cb",
        api_base="https://api.example/_api",
        token_dir=tmp_path,
        use_keyring=False,
    )
    return Session(
        config=cfg,
        auth=KhdpAuthClient(cfg),
        store=TokenStore(tmp_path, use_keyring=False),
    )


def test_status_unauthenticated(session: Session) -> None:
    assert session.status() == {
        "authenticated": False,
        "app_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    }


def test_access_token_returns_cached_when_fresh(session: Session) -> None:
    session.store.save(TokenSet(
        access_token="AT", refresh_token="RT",
        expires_at=time.time() + 600, app_id=session.config.app_id,
    ))
    assert session.access_token() == "AT"


def test_access_token_auto_refresh_when_expired(
    session: Session, httpx_mock,
) -> None:
    session.store.save(TokenSet(
        access_token="OLD", refresh_token="RT",
        expires_at=time.time() - 1, app_id=session.config.app_id,
    ))
    httpx_mock.add_response(
        url="https://api.example/_api/oauth/refresh-token",
        method="POST",
        json={
            "accessToken": "NEW",
            "refreshToken": "RT2",
            "tokenType": "Bearer",
            "expires_in": 1200,
        },
    )
    assert session.access_token() == "NEW"
    again = session.store.load(session.config.app_id)
    assert again is not None
    assert again.access_token == "NEW"
    assert again.refresh_token == "RT2"


def test_authed_request_attaches_bearer(session: Session, httpx_mock) -> None:
    session.store.save(TokenSet(
        access_token="AT", refresh_token="RT",
        expires_at=time.time() + 600, app_id=session.config.app_id,
    ))

    def _check(request):
        import httpx
        assert request.headers["authorization"] == "Bearer AT"
        return httpx.Response(200, json={"ok": True})

    httpx_mock.add_callback(_check, url="https://api.example/_api/member/me")
    resp = session.authed_request("GET", "/member/me")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
