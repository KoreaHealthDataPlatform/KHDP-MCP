"""KHDP OAuth client.

Implements the **Authorization Code flow with PKCE** (RFC 7636) using
the **Loopback Redirect** pattern (RFC 8252 §7.3) for installed
applications such as this CLI.

Endpoints used:

* ``GET <authorize_url>?appId&redirectUrl&codeChallenge&codeChallengeMethod=S256&state``
  -- the user's browser is sent here. KHDP's web UI handles login and
  consent, then redirects to the loopback ``redirectUrl`` with
  ``?code=...&state=...``.
* ``POST /_api/oauth/token  { code, appId, codeVerifier }``
  -- the CLI exchanges the authorization code for a Bearer token pair.
* ``POST /_api/oauth/refresh-token  { appId, refreshToken }``
  -- rotate an expired access token.

The legacy ``mail + password`` flow (``POST /_api/oauth/login``) is
preserved at the bottom of this module as commented-out reference --
KHDP's web SPA still uses that endpoint, but exposing it through a
third-party CLI is discouraged (RFC 6749 §4.3 deprecates the
Resource Owner Password Credentials grant).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import logging
import secrets
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

import httpx

from khdp.config import Config

log = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """Raised when KHDP rejects an OAuth request or the local flow fails."""


# Backward-compat alias -- previous draft used the OIDC name.
OAuthError = AuthError


@dataclass
class TokenSet:
    """Bearer token pair issued by KHDP."""

    access_token: str
    refresh_token: str | None = None
    # Absolute expiry moment in unix seconds (normalised from KHDP's payload).
    expires_at: float = 0.0
    app_id: str = ""
    obtained_at: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        if self.expires_at == 0.0:
            return False
        # 30 second skew so the server doesn't reject a token we just refreshed.
        return time.time() >= (self.expires_at - 30)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TokenSet:
        return cls(**payload)

    @classmethod
    def from_khdp_response(cls, payload: dict[str, Any], *, app_id: str) -> TokenSet:
        """Normalise a KHDP token response.

        The PKCE token endpoint returns:
        ```
        { "accessToken": "...", "refreshToken": "...",
          "tokenType": "Bearer", "expires_in": 3600 }
        ```

        The legacy password endpoint returned ``expireTime`` instead --
        either an absolute unix-millis timestamp or, on some
        environments, a relative duration in seconds. Both shapes are
        tolerated.
        """
        access = payload.get("accessToken") or payload.get("access_token")
        if not access:
            raise AuthError(f"Token response missing accessToken: {payload}")
        refresh = payload.get("refreshToken") or payload.get("refresh_token")

        # Prefer OAuth-standard ``expires_in`` (relative seconds) when present.
        # Fall back to legacy ``expireTime`` (absolute ms or relative seconds).
        expires_at = 0.0
        if (raw := payload.get("expires_in")) is not None:
            try:
                expires_at = time.time() + float(raw)
            except (TypeError, ValueError):
                expires_at = 0.0
        elif (raw := payload.get("expireTime") or payload.get("expire_time")):
            try:
                num = float(raw)
            except (TypeError, ValueError):
                num = 0.0
            if num >= 1e12:        # absolute milliseconds
                expires_at = num / 1000.0
            elif num >= 1e9:       # absolute seconds
                expires_at = num
            elif num > 0:          # relative seconds
                expires_at = time.time() + num

        return cls(
            access_token=access,
            refresh_token=refresh,
            expires_at=expires_at,
            app_id=app_id,
            obtained_at=time.time(),
        )


# ────────────────────────────────────────────────────────────────────────
#  PKCE helpers
# ────────────────────────────────────────────────────────────────────────

def _generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` per RFC 7636 §4.1-4.2."""
    verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    )
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _generate_state() -> str:
    return secrets.token_urlsafe(16)


class _LoopbackCallbackHandler(http.server.BaseHTTPRequestHandler):
    """One-shot HTTP handler that captures the redirect's query string."""

    received: ClassVar[dict[str, str]] = {}

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        type(self).received = params
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (
            "<!doctype html><html><body style='font-family:sans-serif;padding:2rem'>"
            "<h2>KHDP login complete</h2>"
            "<p>You can close this tab and return to your terminal.</p>"
            "</body></html>"
        )
        self.wfile.write(body.encode())

    def log_message(self, *_args: Any) -> None:
        return


def _start_callback_server(
    host: str, port: int
) -> http.server.HTTPServer:
    """Bind + listen on (host, port). Caller drives ``handle_request``.

    Port ``0`` lets the kernel pick a free port; the actual port is read
    back via ``server.server_address[1]``.
    """
    _LoopbackCallbackHandler.received = {}
    return http.server.HTTPServer((host, port), _LoopbackCallbackHandler)


def _wait_for_callback(
    server: http.server.HTTPServer, *, timeout: float
) -> dict[str, str]:
    server.timeout = timeout
    try:
        # ``handle_request`` blocks until one request arrives or timeout fires.
        server.handle_request()
    finally:
        server.server_close()
    if not _LoopbackCallbackHandler.received:
        host, port = server.server_address[:2]
        raise AuthError(
            f"OAuth callback never arrived on {host}:{port} "
            f"(timeout {timeout}s)"
        )
    captured = _LoopbackCallbackHandler.received
    _LoopbackCallbackHandler.received = {}
    return captured


def _derive_default_authorize_url(api_base: str) -> str:
    """Heuristic: ``https://khdp.net/_api`` → ``https://khdp.net/external/oauth-login``."""
    parsed = urllib.parse.urlparse(api_base)
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, "/external/oauth-login", "", "", "")
    )


# ────────────────────────────────────────────────────────────────────────
#  Client
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _Endpoints:
    authorize: str   # KHDP web URL the user's browser is sent to (login + consent)
    token: str       # POST /oauth/token        — exchange auth code → tokens
    refresh: str     # POST /oauth/refresh-token — rotate refresh token


class KhdpAuthClient:
    """KHDP OAuth client implementing PKCE Authorization Code (RFC 7636 / 8252)."""

    def __init__(
        self, config: Config, *, http_client: httpx.Client | None = None
    ) -> None:
        self.config = config
        self._http = http_client or httpx.Client(
            timeout=30.0, headers={"User-Agent": "khdp/0.3.0"}
        )
        api_base = config.api_base.rstrip("/")
        self._endpoints = _Endpoints(
            authorize=config.authorize_url
            or _derive_default_authorize_url(api_base),
            token=f"{api_base}/oauth/token",
            refresh=f"{api_base}/oauth/refresh-token",
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> KhdpAuthClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── PKCE login ─────────────────────────────────────────────────

    def pkce_login(
        self,
        *,
        callback_host: str = "127.0.0.1",
        callback_port: int = 0,
        callback_path: str = "/callback",
        open_browser: Callable[[str], bool] = webbrowser.open,
        callback_timeout: float = 300.0,
    ) -> TokenSet:
        """Run the PKCE Authorization Code flow with a loopback redirect.

        Spins up a one-shot HTTP server on ``callback_host:callback_port``
        (port ``0`` = random free), opens the user's browser at the KHDP
        login page, waits for the redirect to deliver the authorization
        ``code``, then exchanges it for a token via ``POST /oauth/token``.
        """
        if not self.config.app_id:
            raise AuthError(
                "config.app_id is required for KHDP login. "
                "Register a KHDP app and set it via KHDP_APP_ID or khdp.local.toml."
            )

        verifier, challenge = _generate_pkce_pair()
        state = _generate_state()

        # 1) 서버를 먼저 띄워 listen 상태로 만든다 (race 방지).
        server = _start_callback_server(callback_host, callback_port)
        actual_port = server.server_address[1]
        redirect_uri = f"http://{callback_host}:{actual_port}{callback_path}"

        authorize_url = (
            f"{self._endpoints.authorize}?"
            + urllib.parse.urlencode(
                {
                    "appId": self.config.app_id,
                    "redirectUrl": redirect_uri,
                    "codeChallenge": challenge,
                    "codeChallengeMethod": "S256",
                    "state": state,
                }
            )
        )

        # 2) 브라우저 오픈 (이 시점에 server 는 이미 listen 중).
        log.info("Opening browser for KHDP login: %s", authorize_url)
        try:
            open_browser(authorize_url)
        except Exception as exc:
            server.server_close()
            raise AuthError(f"failed to open browser: {exc}") from exc

        # 3) 콜백 도착 대기.
        params = _wait_for_callback(server, timeout=callback_timeout)

        returned_state = params.get("state")
        if returned_state is None:
            log.warning(
                "OAuth callback did not include `state`; CSRF check skipped. "
                "Ask the backend to forward the `state` parameter on redirect."
            )
        elif returned_state != state:
            raise AuthError(
                "OAuth state mismatch -- possible CSRF or stale callback"
            )
        code = params.get("code")
        if not code:
            err = (
                params.get("error_description")
                or params.get("error")
                or "no code in callback"
            )
            raise AuthError(f"OAuth callback did not include a code: {err}")

        return self._exchange_authorization_code(code, verifier)

    def refresh(self, refresh_token: str) -> TokenSet:
        """Rotate an expired access token via ``POST /oauth/refresh-token``."""
        body = {
            "appId": self.config.app_id,
            "refreshToken": refresh_token,
        }
        return self._post_token(self._endpoints.refresh, body)

    # ── internals ──────────────────────────────────────────────────

    def _exchange_authorization_code(self, code: str, verifier: str) -> TokenSet:
        body = {
            "code": code,
            "appId": self.config.app_id,
            "codeVerifier": verifier,
        }
        return self._post_token(self._endpoints.token, body)

    def _post_token(self, url: str, body: dict[str, Any]) -> TokenSet:
        try:
            resp = self._http.post(url, json=body)
        except httpx.HTTPError as exc:
            raise AuthError(f"KHDP endpoint unreachable ({url}): {exc}") from exc

        if resp.status_code in (200, 201):
            try:
                payload = resp.json()
            except ValueError as exc:
                raise AuthError(
                    f"KHDP returned non-JSON success: {resp.text[:200]}"
                ) from exc
            return TokenSet.from_khdp_response(payload, app_id=self.config.app_id)

        detail: str = resp.text[:400]
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                msg = payload.get("message")
                if isinstance(msg, list):
                    detail = "; ".join(str(m) for m in msg)
                elif isinstance(msg, str):
                    detail = msg
        except ValueError:
            pass
        raise AuthError(f"KHDP {resp.status_code} {url}: {detail}")


# ────────────────────────────────────────────────────────────────────────
#  Legacy password / auto-login flow (DEPRECATED, kept for reference)
# ────────────────────────────────────────────────────────────────────────
#  The KHDP web SPA still uses ``POST /_api/oauth/login`` and
#  ``POST /_api/member/auto-login``. Exposing those endpoints through a
#  third-party CLI puts the user's password into the CLI process and
#  has been deprecated by RFC 6749 §4.3. The PKCE flow above replaces
#  them; the code below is preserved verbatim only as a reference for
#  anyone resurrecting the password path (e.g. for SPA-class apps).
#
# def password_login(self, *, email: str, password: str) -> TokenSet:
#     """[DEPRECATED] Direct ``mail + password`` exchange against
#     ``POST /_api/oauth/login``. Requires ``app_id`` and ``redirect_url``.
#     """
#     if not self.config.app_id:
#         raise AuthError(
#             "config.app_id is required for KHDP login."
#         )
#     if not self.config.redirect_url:
#         raise AuthError(
#             "config.redirect_url is required for KHDP login."
#         )
#     body = {
#         "appId": self.config.app_id,
#         "redirectUrl": self.config.redirect_url,
#         "mail": email,
#         "password": password,
#     }
#     return self._post_token(f"{self.config.api_base}/oauth/login", body)
#
# def auto_login(self, *, access_token: str, refresh_token: str) -> TokenSet:
#     """[DEPRECATED] Sliding-refresh login used by the KHDP web client at
#     startup -- ``POST /_api/member/auto-login {accessToken, refreshToken}``.
#     """
#     body = {"accessToken": access_token, "refreshToken": refresh_token}
#     return self._post_token(f"{self.config.api_base}/member/auto-login", body)
