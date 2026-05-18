"""``khdp submissions`` subcommand group.

Wraps ``/open/dataset-submissions/*`` endpoints. OAuth-only -- the CLI
must have a cached user token (``khdp login``).

This module currently exposes only the parser scaffolding; per-command
implementations land in a follow-up change.
"""

from __future__ import annotations

import argparse

from khdp.session import Session


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("submissions", help="own dataset submission operations")
    sp = p.add_subparsers(dest="submissions_command", required=True)

    sp.add_parser("list", help="list my dataset submissions").set_defaults(func=_todo)
    sp.add_parser("show", help="show one submission detail").set_defaults(func=_todo)
    sp.add_parser("create", help="create a new dataset submission").set_defaults(func=_todo)
    sp.add_parser("mkdir", help="create a directory in a submission").set_defaults(func=_todo)
    sp.add_parser("upload", help="upload a local file to a submission").set_defaults(func=_todo)
    sp.add_parser("list-files", help="list files in a submission").set_defaults(func=_todo)
    sp.add_parser("delete", help="delete a file from a submission").set_defaults(func=_todo)
    sp.add_parser("submit", help="finalise a submission").set_defaults(func=_todo)


def _todo(_session: Session, args: argparse.Namespace) -> int:
    raise SystemExit(
        f"[khdp] `submissions {args.submissions_command}` is not implemented yet."
    )
