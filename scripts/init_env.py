#!/usr/bin/env python3
"""Create a .env with freshly generated secrets. Standard library only.

Usage: python3 scripts/init_env.py [--force] [--domain stream.example.com]

Prints the generated admin password ONCE; it is also written to .env as
OMNI_ADMIN_PASSWORD (hashed in memory at startup). Swap it for
OMNI_ADMIN_PASSWORD_HASH via `make hash-password` whenever you like.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import secrets
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def fernet_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="overwrite an existing .env")
    parser.add_argument("--domain", default="localhost", help="public hostname for Caddy")
    parser.add_argument("--output", default=str(ROOT / ".env"))
    args = parser.parse_args(argv)

    out = Path(args.output)
    if out.exists() and not args.force:
        print(f"{out} already exists (use --force to overwrite)", file=sys.stderr)
        return 1

    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    admin_password = secrets.token_urlsafe(18)
    values = {
        "OMNI_SECRET_KEY": fernet_key(),
        "OMNI_SESSION_SECRET": secrets.token_urlsafe(48),
        "OMNI_ADMIN_PASSWORD": admin_password,
        "OMNI_INGEST_STREAM_KEY": secrets.token_hex(20),
        "OMNI_DOMAIN": args.domain,
    }

    def sub(line: str) -> str:
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if m and m.group(1) in values:
            return f"{m.group(1)}={values[m.group(1)]}"
        return line

    content = "\n".join(sub(line) for line in template.splitlines()) + "\n"
    out.write_text(content, encoding="utf-8")
    os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)

    print(f"wrote {out} (mode 600)")
    print("admin username: admin")
    print(f"admin password: {admin_password}")
    print(f"OBS server:     rtmp://{args.domain}:1935/live")
    print(f"OBS stream key: {values['OMNI_INGEST_STREAM_KEY']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
