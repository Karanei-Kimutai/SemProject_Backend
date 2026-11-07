"""Admin commands: `python -m app.cli <command>`."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from pathlib import Path

from sqlalchemy import select

from app.db import get_sessionmaker
from app.models import User
from app.security import hash_password


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("seed-reference", help="Load the demo workspace from an mlengine export")
    seed.add_argument("source", type=Path, help="Folder written by `python -m mlengine export-reference`")
    user = sub.add_parser("create-user", help="Create a user (or update the password of an existing one)")
    user.add_argument("email")
    user.add_argument("--name", default=None)
    user.add_argument("--research", action="store_true", help="Allow model-version choice and research dates")
    user.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args(argv)

    with get_sessionmaker()() as db:
        if args.command == "seed-reference":
            from app.seed import seed_reference
            print(json.dumps(seed_reference(db, args.source), indent=2))
        elif args.command == "create-user":
            password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("Password: ")
            if len(password) < 10:
                print("Passwords need at least 10 characters.", file=sys.stderr)
                return 1
            email = args.email.lower()
            account = db.scalar(select(User).where(User.email == email))
            if account is None:
                account = User(email=email, display_name=args.name or email.split("@")[0], password_hash="")
                db.add(account)
            account.password_hash = hash_password(password)
            account.is_research = args.research or account.is_research
            db.commit()
            print(f"user {email} ready (research={account.is_research})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
