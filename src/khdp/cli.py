"""``khdp`` command-line interface.

Subcommand groups:

* ``khdp login``       -- PKCE Authorization Code login (opens a browser).
* ``khdp logout``      -- delete the cached token.
* ``khdp status``      -- show whether a token is cached.
* ``khdp refresh``     -- force a refresh-token rotation.
* ``khdp token``       -- print the current access token (use with care).
* ``khdp datasets ...`` -- public dataset operations (list / show / files /
  download-link / download).
* ``khdp submissions ...`` -- own dataset submission operations.
* ``khdp api METHOD PATH`` -- escape hatch: any authenticated API call.
* ``khdp config``      -- show the resolved configuration.
* ``khdp mcp``         -- start the MCP server on stdio.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence

from khdp import __version__, cli_datasets, cli_submissions
from khdp.config import load_config
from khdp.oauth import AuthError
from khdp.session import Session


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="khdp",
        description="KHDP connector -- login + API calls + MCP server.",
    )
    parser.add_argument("--version", action="version", version=f"khdp {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="increase logging verbosity (repeat for debug)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # ── auth + housekeeping ───────────────────────────────────────────
    p_login = sub.add_parser(
        "login", help="log in via the KHDP login page (opens a browser)",
    )
    p_login.add_argument(
        "--no-browser", action="store_true",
        help="print the login URL instead of opening a browser",
    )
    p_login.set_defaults(func=_cmd_login)

    sub.add_parser("logout", help="delete cached tokens").set_defaults(func=_cmd_logout)
    sub.add_parser("status", help="show cached token state").set_defaults(func=_cmd_status)
    sub.add_parser("refresh", help="force-refresh the access token").set_defaults(func=_cmd_refresh)

    p_token = sub.add_parser("token", help="print the current access token")
    p_token.add_argument(
        "--raw", action="store_true",
        help="print only the token value (no JSON envelope)",
    )
    p_token.set_defaults(func=_cmd_token)

    # ── domain commands ───────────────────────────────────────────────
    cli_datasets.add_subparser(sub)
    cli_submissions.add_subparser(sub)

    # ── escape hatch + meta ───────────────────────────────────────────
    p_api = sub.add_parser("api", help="make an authenticated API call (escape hatch)")
    p_api.add_argument("method", help="HTTP method, e.g. GET / POST")
    p_api.add_argument("path", help="API path or full URL")
    p_api.add_argument(
        "--query", action="append", default=[], metavar="KEY=VAL",
        help="query parameter (repeatable)",
    )
    p_api.add_argument("--data", help="JSON body string")
    p_api.set_defaults(func=_cmd_api)

    sub.add_parser("mcp", help="run the KHDP MCP server on stdio").set_defaults(func=_cmd_mcp)
    sub.add_parser("config", help="show resolved configuration").set_defaults(func=_cmd_config)

    return parser


def _setup_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="[khdp] %(levelname)s %(message)s")


def _emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _cmd_login(session: Session, args: argparse.Namespace) -> int:
    if args.no_browser:
        def _print_url(url: str) -> bool:
            print(f"Open this URL in a browser to log in:\n  {url}", file=sys.stderr)
            return True

        tokens = session.login(open_browser=_print_url)
    else:
        tokens = session.login()
    _emit({"ok": True, "expires_at": tokens.expires_at})
    return 0


def _cmd_logout(session: Session, _args: argparse.Namespace) -> int:
    deleted = session.logout()
    _emit({"ok": True, "deleted": deleted})
    return 0


def _cmd_status(session: Session, _args: argparse.Namespace) -> int:
    _emit(session.status())
    return 0


def _cmd_refresh(session: Session, _args: argparse.Namespace) -> int:
    tokens = session.store.load(session.config.app_id or None)
    if not tokens or not tokens.refresh_token:
        print("[khdp] no refresh token cached; run `khdp login` first.", file=sys.stderr)
        return 1
    refreshed = session.auth.refresh(tokens.refresh_token)
    if not refreshed.refresh_token:
        refreshed.refresh_token = tokens.refresh_token
    if not refreshed.app_id:
        refreshed.app_id = tokens.app_id or session.config.app_id
    session.store.save(refreshed)
    _emit({"ok": True, "expires_at": refreshed.expires_at})
    return 0


def _cmd_token(session: Session, args: argparse.Namespace) -> int:
    token = session.access_token()
    if args.raw:
        print(token)
    else:
        _emit({"access_token": token})
    return 0


def _cmd_api(session: Session, args: argparse.Namespace) -> int:
    params: dict[str, str] = {}
    for kv in args.query:
        if "=" not in kv:
            print(f"[khdp] invalid --query (expected KEY=VAL): {kv}", file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        params[k] = v
    body = json.loads(args.data) if args.data else None
    resp = session.authed_request(
        args.method, args.path, params=params or None, json=body,
    )
    print(f"[khdp] {resp.status_code} {resp.reason_phrase}", file=sys.stderr)
    try:
        _emit(resp.json())
    except ValueError:
        sys.stdout.write(resp.text)
    return 0 if resp.is_success else 1


def _cmd_mcp(_session: Session, _args: argparse.Namespace) -> int:
    from khdp.mcp_server import run_stdio
    run_stdio()
    return 0


def _cmd_config(session: Session, _args: argparse.Namespace) -> int:
    cfg = session.config
    _emit({
        "app_id": cfg.app_id or None,
        "api_base": cfg.api_base,
        "token_dir": str(cfg.token_dir),
    })
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    config = load_config()
    try:
        with Session.open(config=config) as session:
            return args.func(session, args)
    except AuthError as exc:
        print(f"[khdp] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("[khdp] interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
