"""Unit + integration coverage for ``khdp datasets`` subcommands.

Hits the same code paths as the CLI by driving ``khdp.cli.main``
directly with mocked HTTP. The PKCE callback / OAuth issuance is not
exercised here -- ``test_auth_client.py`` covers that.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from khdp.cli import main as cli_main
from khdp.cli_datasets import _fmt_size, _parse_ref
from khdp.oauth import TokenSet

# ── pure helpers ──────────────────────────────────────────────────────


def test_parse_ref_default_latest() -> None:
    assert _parse_ref("vitaldb_open") == ("vitaldb_open", "latest")


def test_parse_ref_trailing_at_defaults_to_latest() -> None:
    assert _parse_ref("vitaldb_open@") == ("vitaldb_open", "latest")


def test_parse_ref_explicit_version() -> None:
    assert _parse_ref("vitaldb_open@1.0.0") == ("vitaldb_open", "1.0.0")


def test_parse_ref_empty_raises() -> None:
    with pytest.raises(SystemExit):
        _parse_ref("")
    with pytest.raises(SystemExit):
        _parse_ref("@1.0.0")


def test_fmt_size_boundaries() -> None:
    assert _fmt_size(0) == "0 B"
    assert _fmt_size(1023) == "1023 B"
    assert _fmt_size(1024) == "1.0 KB"
    assert _fmt_size(1024 * 1024) == "1.0 MB"
    assert _fmt_size(2 * 1024 ** 3) == "2.00 GB"


# ── CLI integration (with mocked HTTP + token store) ──────────────────


_APP_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_API = "https://api.example/_api"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KHDP_APP_ID", _APP_ID)
    monkeypatch.setenv("KHDP_API_BASE", _API)
    monkeypatch.setenv("KHDP_TOKEN_DIR", str(tmp_path))
    monkeypatch.setenv("KHDP_USE_KEYRING", "0")
    # Pre-seed a valid token so authed_request paths work.
    from khdp.token_store import TokenStore
    store = TokenStore(tmp_path, use_keyring=False)
    store.save(TokenSet(
        access_token="AT",
        refresh_token="RT",
        expires_at=time.time() + 3600,
        app_id=_APP_ID,
    ))


def test_datasets_list_default_table(
    env: None, httpx_mock: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    httpx_mock.add_response(
        url=f"{_API}/open/datasets?page=1&limit=10",
        method="GET",
        json={
            "totalCnt": 1, "totalPage": 1, "page": 1, "limit": 10,
            "data": [{
                "code": "vitaldb_open",
                "title": "VitalDB",
                "version": "1.0.0",
                "accessPolicy": "open",
            }],
        },
    )
    rc = cli_main(["datasets", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "vitaldb_open" in out
    assert "1.0.0" in out
    assert "page 1/1, total 1" in out


def test_datasets_list_json_mode(
    env: None, httpx_mock: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    httpx_mock.add_response(
        url=f"{_API}/open/datasets?page=1&limit=10",
        method="GET",
        json={"totalCnt": 0, "totalPage": 1, "page": 1, "limit": 10, "data": []},
    )
    rc = cli_main(["datasets", "list", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"totalCnt": 0' in out


def test_datasets_show_uses_at_latest_by_default(
    env: None, httpx_mock: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    httpx_mock.add_response(
        url=f"{_API}/open/datasets/vitaldb_open/latest",
        method="GET",
        json={"code": "vitaldb_open", "version": "1.0.0", "accessPolicy": "open"},
    )
    rc = cli_main(["datasets", "show", "vitaldb_open"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"code": "vitaldb_open"' in out


def test_datasets_download_link_prints_url(
    env: None, httpx_mock: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    httpx_mock.add_response(
        url=f"{_API}/open/datasets/vitaldb_open/1.0.0/files/download-link?key=imaging%2Fa.dcm",
        method="GET",
        json={"url": "https://s3.example/signed-url"},
    )
    rc = cli_main([
        "datasets", "download-link",
        "vitaldb_open@1.0.0",
        "--key", "imaging/a.dcm",
    ])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "https://s3.example/signed-url"


def test_datasets_download_dry_run_paginates_with_continue_token(
    env: None, httpx_mock: Any, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # First page returns a continueToken so the CLI fetches a second page.
    httpx_mock.add_response(
        url=(
            f"{_API}/open/datasets/vitaldb_open/1.0.0/files-download-link-all"
        ),
        method="GET",
        json={
            "items": [
                {"key": "a.csv", "size": 1024, "url": "https://x/a"},
                {"key": "b.csv", "size": 2048, "url": "https://x/b"},
            ],
            "continueToken": "TOK",
        },
    )
    httpx_mock.add_response(
        url=(
            f"{_API}/open/datasets/vitaldb_open/1.0.0/files-download-link-all"
            f"?continueToken=TOK"
        ),
        method="GET",
        json={
            "items": [
                {"key": "c.csv", "size": 4096, "url": "https://x/c"},
            ],
            "continueToken": None,
        },
    )
    out_dir = tmp_path / "out"
    rc = cli_main([
        "datasets", "download",
        "vitaldb_open@1.0.0",
        "--dry-run",
        "--out", str(out_dir),
    ])
    assert rc == 0
    captured = capsys.readouterr()
    # dry-run keys are printed on stdout, summary lines on stderr.
    assert "a.csv" in captured.out
    assert "c.csv" in captured.out
    assert "page 1: 2 file(s)" in captured.err
    assert "page 2: 1 file(s)" in captured.err
    assert "3 file(s)" in captured.err  # cumulative summary
    # Dry-run must not create files.
    assert not out_dir.exists() or not any(out_dir.iterdir())


def test_datasets_download_max_pages_stops_early(
    env: None, httpx_mock: Any, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    httpx_mock.add_response(
        url=(
            f"{_API}/open/datasets/vitaldb_open/1.0.0/files-download-link-all"
        ),
        method="GET",
        json={
            "items": [{"key": "a", "size": 0, "url": "https://x/a"}],
            "continueToken": "TOK",
        },
    )
    # Note: second call should NOT happen because --max-pages 1 stops.
    rc = cli_main([
        "datasets", "download",
        "vitaldb_open@1.0.0",
        "--dry-run",
        "--max-pages", "1",
        "--out", str(tmp_path / "out"),
    ])
    assert rc == 0
    err = capsys.readouterr().err
    assert "reached --max-pages=1" in err
