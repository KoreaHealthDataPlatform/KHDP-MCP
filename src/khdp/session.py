"""Session helpers -- combine ``KhdpAuthClient`` and ``TokenStore`` to give
callers a single ``access_token()`` style API that handles refresh
transparently."""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from khdp.config import Config, load_config
from khdp.oauth import AuthError, KhdpAuthClient, TokenSet
from khdp.token_store import TokenStore

log = logging.getLogger(__name__)


@dataclass
class Session:
    config: Config
    auth: KhdpAuthClient
    store: TokenStore

    @classmethod
    def open(cls, *, config: Config | None = None) -> Session:
        cfg = config or load_config()
        return cls(
            config=cfg,
            auth=KhdpAuthClient(cfg),
            store=TokenStore(cfg.token_dir, use_keyring=cfg.use_keyring),
        )

    def close(self) -> None:
        self.auth.close()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------

    def login(self, **pkce_options: Any) -> TokenSet:
        """Run the PKCE Authorization Code login.

        Keyword arguments are forwarded to
        :meth:`KhdpAuthClient.pkce_login` so callers (CLI / tests) can
        override callback host/port, browser opener, and timeout.
        """
        tokens = self.auth.pkce_login(**pkce_options)
        self.store.save(tokens)
        return tokens

    def logout(self) -> bool:
        """Delete locally cached tokens.

        KHDP's public ``/_api`` surface does not expose a refresh-token
        revocation endpoint at the time of writing. The web SPA logs
        out by clearing local state and letting the access token expire
        naturally; we follow the same approach. Returns ``True`` if a
        token was deleted, ``False`` if there was nothing to delete.
        """
        return self.store.delete(self.config.app_id or None)

    def status(self) -> dict[str, Any]:
        tokens = self.store.load(self.config.app_id or None)
        if not tokens:
            return {
                "authenticated": False,
                "app_id": self.config.app_id or None,
            }
        return {
            "authenticated": True,
            "app_id": tokens.app_id or self.config.app_id,
            "expires_at": tokens.expires_at,
            "is_expired": tokens.is_expired,
            "has_refresh_token": tokens.refresh_token is not None,
        }

    def access_token(self) -> str:
        """Return a valid access token, refreshing if necessary.

        Raises :class:`AuthError` if the user has never logged in or the
        refresh token has been revoked.
        """
        tokens = self.store.load(self.config.app_id or None)
        if tokens is None:
            raise AuthError("Not logged in. Run `khdp login` first.")
        if not tokens.is_expired:
            return tokens.access_token
        if not tokens.refresh_token:
            raise AuthError("Access token expired and no refresh token is available.")
        log.debug("Refreshing expired access token for app %s", self.config.app_id)
        refreshed = self.auth.refresh(tokens.refresh_token)
        if not refreshed.refresh_token:
            # Some KHDP environments may omit refresh_token on refresh
            # -- keep the previous one.
            refreshed.refresh_token = tokens.refresh_token
        if not refreshed.app_id:
            refreshed.app_id = tokens.app_id or self.config.app_id
        self.store.save(refreshed)
        return refreshed.access_token

    def authed_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        """Issue an authenticated request against the KHDP API base.

        Raises :class:`AuthError` if the user is not logged in.
        ``path`` may be a full URL or a path relative to ``config.api_base``.
        """
        return self._request(method, path, params=params, json=json, require_auth=True)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        """Issue a request that uses the cached token if available.

        Falls back to an anonymous call when no token is cached -- useful
        for endpoints that allow anonymous access (e.g. ``/open/datasets``
        list / detail).
        """
        return self._request(method, path, params=params, json=json, require_auth=False)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json: Any,
        require_auth: bool,
    ) -> httpx.Response:
        url = path if path.startswith(("http://", "https://")) else (
            self.config.api_base.rstrip("/") + "/" + path.lstrip("/")
        )
        headers: dict[str, str] = {"User-Agent": "khdp/0.3.0"}
        if require_auth:
            headers["Authorization"] = f"Bearer {self.access_token()}"
        else:
            # Anonymous fall-through: attach the bearer if we have one,
            # otherwise just call without it.
            with contextlib.suppress(AuthError):
                headers["Authorization"] = f"Bearer {self.access_token()}"
        with httpx.Client(timeout=30.0) as http:
            return http.request(
                method.upper(), url, params=params, json=json, headers=headers,
            )
