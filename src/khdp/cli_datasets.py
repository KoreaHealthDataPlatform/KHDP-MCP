"""``khdp datasets`` subcommand group.

Wraps ``/open/datasets/*`` endpoints with arguments friendlier than
``khdp api GET /open/datasets``. Falls through to the same session
machinery, so anonymous and authenticated calls work identically.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from khdp.session import Session

# ── argparse wiring ───────────────────────────────────────────────────


_POLICY_TO_QUERY = {
    "open": "0",
    "restricted": "1",
    "credentialed": "2",
    "contributor_review": "3",
}


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("datasets", help="public dataset operations")
    sp = p.add_subparsers(dest="datasets_command", required=True)

    p_list = sp.add_parser("list", help="search public datasets")
    p_list.add_argument("--query", help="keyword (matches title / summary)")
    p_list.add_argument(
        "--policy", choices=list(_POLICY_TO_QUERY),
        help="filter by access policy",
    )
    p_list.add_argument("--page", type=int, default=1)
    p_list.add_argument("--limit", type=int, default=10)
    p_list.add_argument("--json", action="store_true", help="raw JSON output")
    p_list.set_defaults(func=_cmd_list)

    p_show = sp.add_parser("show", help="show dataset detail")
    p_show.add_argument("ref", help="<code>[@<version>] (defaults to @latest)")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=_cmd_show)

    p_files = sp.add_parser("files", help="list files in a dataset")
    p_files.add_argument("ref")
    p_files.add_argument("--key", default="", help="directory prefix")
    p_files.add_argument("--json", action="store_true")
    p_files.set_defaults(func=_cmd_files)

    p_dlink = sp.add_parser(
        "download-link", help="get a presigned URL for a single file"
    )
    p_dlink.add_argument("ref")
    p_dlink.add_argument("--key", required=True)
    p_dlink.set_defaults(func=_cmd_download_link)

    p_dl = sp.add_parser(
        "download", help="download every file of an open dataset"
    )
    p_dl.add_argument("ref")
    p_dl.add_argument(
        "--out", default=".", help="output directory (default: current dir)",
    )
    p_dl.add_argument(
        "--max-pages", type=int, default=0,
        help="stop after N pages of links (default: 0 = all). Server "
             "returns up to 1000 files per page.",
    )
    p_dl.add_argument(
        "--dry-run", action="store_true",
        help="list keys/sizes only, don't fetch file contents",
    )
    p_dl.set_defaults(func=_cmd_download)


# ── helpers ───────────────────────────────────────────────────────────


def _parse_ref(ref: str) -> tuple[str, str]:
    """Parse a dataset ref ``<code>[@<version>]``. Defaults to ``latest``."""
    code, _, version = ref.partition("@")
    if not code:
        raise SystemExit(f"[khdp] dataset ref is empty: {ref!r}")
    return code, version or "latest"


def _emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _try_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text


def _check_response(resp: httpx.Response, body: Any) -> int | None:
    """Print a status line + body to stderr/stdout when the call failed.

    Returns a non-zero exit code on failure, ``None`` on success.
    """
    if resp.is_success:
        return None
    print(f"[khdp] {resp.status_code} {resp.reason_phrase}", file=sys.stderr)
    msg = body.get("message") if isinstance(body, dict) else None
    if msg:
        print(f"[khdp] {msg}", file=sys.stderr)
    else:
        _emit(body)
    return 1


# ── pretty printers ───────────────────────────────────────────────────


def _print_dataset_list(body: Any) -> None:
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        _emit(body)
        return
    items = body["data"]
    if not items:
        print("(no datasets)")
        return
    code_w = max(len("code"), max(len(str(i.get("code", ""))) for i in items))
    ver_w = max(len("version"), max(len(str(i.get("version", ""))) for i in items))
    pol_w = max(len("policy"), max(len(str(i.get("accessPolicy", ""))) for i in items))
    print(f"{'code':<{code_w}}  {'version':<{ver_w}}  {'policy':<{pol_w}}  title")
    print(f"{'-'*code_w}  {'-'*ver_w}  {'-'*pol_w}  -----")
    for it in items:
        print(
            f"{it.get('code','')!s:<{code_w}}  "
            f"{it.get('version','')!s:<{ver_w}}  "
            f"{it.get('accessPolicy','')!s:<{pol_w}}  "
            f"{it.get('title','')}"
        )
    total = body.get("totalCnt")
    page = body.get("page")
    total_page = body.get("totalPage")
    if total is not None:
        print(f"\npage {page}/{total_page}, total {total}")


def _print_files(body: Any) -> None:
    if not isinstance(body, dict):
        _emit(body)
        return
    sub_dirs = body.get("subDirs") or []
    contents = body.get("contents") or []
    for d in sub_dirs:
        print(f"D  {d.get('key', '')}")
    for f in contents:
        size = f.get("size", 0)
        print(f"F  {f.get('key', '')}  ({size} bytes)")
    if not sub_dirs and not contents:
        print("(empty)")


# ── commands ──────────────────────────────────────────────────────────


def _cmd_list(session: Session, args: argparse.Namespace) -> int:
    params: dict[str, str] = {
        "page": str(args.page),
        "limit": str(args.limit),
    }
    if args.query:
        params["query"] = args.query
    if args.policy:
        params["accessPolicy"] = _POLICY_TO_QUERY[args.policy]
    resp = session.request("GET", "/open/datasets", params=params)
    body = _try_json(resp)
    if (rc := _check_response(resp, body)) is not None:
        return rc
    if args.json:
        _emit(body)
    else:
        _print_dataset_list(body)
    return 0


def _cmd_show(session: Session, args: argparse.Namespace) -> int:
    code, version = _parse_ref(args.ref)
    resp = session.request("GET", f"/open/datasets/{code}/{version}")
    body = _try_json(resp)
    if (rc := _check_response(resp, body)) is not None:
        return rc
    _emit(body)
    return 0


def _cmd_files(session: Session, args: argparse.Namespace) -> int:
    code, version = _parse_ref(args.ref)
    params = {"key": args.key} if args.key else None
    resp = session.authed_request(
        "GET", f"/open/datasets/{code}/{version}/files", params=params,
    )
    body = _try_json(resp)
    if (rc := _check_response(resp, body)) is not None:
        return rc
    if args.json:
        _emit(body)
    else:
        _print_files(body)
    return 0


def _cmd_download_link(session: Session, args: argparse.Namespace) -> int:
    code, version = _parse_ref(args.ref)
    resp = session.authed_request(
        "GET",
        f"/open/datasets/{code}/{version}/files/download-link",
        params={"key": args.key},
    )
    body = _try_json(resp)
    if (rc := _check_response(resp, body)) is not None:
        return rc
    url = body.get("url") if isinstance(body, dict) else None
    if not url:
        _emit(body)
        return 0
    print(url)
    return 0


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def _cmd_download(session: Session, args: argparse.Namespace) -> int:
    code, version = _parse_ref(args.ref)
    out_dir = Path(args.out).expanduser().resolve()
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    cont: str | None = None
    page = 0
    cum_files = 0       # files fetched / listed so far across all pages
    cum_bytes = 0       # bytes fetched / listed so far across all pages
    while True:
        page += 1
        params: dict[str, str] = {}
        if cont:
            params["continueToken"] = cont
        resp = session.authed_request(
            "GET",
            f"/open/datasets/{code}/{version}/files-download-link-all",
            params=params or None,
        )
        body = _try_json(resp)
        if (rc := _check_response(resp, body)) is not None:
            return rc
        items = body.get("items") if isinstance(body, dict) else None
        if not items:
            break
        page_count = len(items)
        page_bytes = sum(int(it.get("size") or 0) for it in items)
        print(
            f"[khdp] page {page}: {page_count} file(s), {_fmt_size(page_bytes)}",
            file=sys.stderr,
        )
        for i, it in enumerate(items, start=1):
            key = it.get("key")
            url = it.get("url") or it.get("downloadUrl")
            if not key or not url:
                continue
            size = int(it.get("size") or 0)
            if args.dry_run:
                print(f"  · {key}  ({_fmt_size(size)})")
                cum_files += 1
                cum_bytes += size
                continue
            target = out_dir / key
            target.parent.mkdir(parents=True, exist_ok=True)
            print(
                f"  [{i:>4}/{page_count}] {key}  ({_fmt_size(size)})",
                file=sys.stderr,
            )
            with httpx.stream("GET", url, timeout=300.0) as r:
                r.raise_for_status()
                with target.open("wb") as fh:
                    for chunk in r.iter_bytes(chunk_size=64 * 1024):
                        fh.write(chunk)
            cum_files += 1
            cum_bytes += size
        print(
            f"[khdp] page {page} done. cumulative: "
            f"{cum_files} file(s), {_fmt_size(cum_bytes)}",
            file=sys.stderr,
        )
        cont = body.get("continueToken")
        if not cont:
            break
        if args.max_pages and page >= args.max_pages:
            print(
                f"[khdp] reached --max-pages={args.max_pages}, stopping",
                file=sys.stderr,
            )
            break

    if args.dry_run:
        print(
            f"[khdp] dry-run: {page} page(s), "
            f"{cum_files} file(s), {_fmt_size(cum_bytes)} total",
            file=sys.stderr,
        )
    else:
        print(
            f"[khdp] downloaded {cum_files} file(s), "
            f"{_fmt_size(cum_bytes)} → {out_dir}",
            file=sys.stderr,
        )
    return 0
