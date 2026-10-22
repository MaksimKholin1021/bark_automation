"""Bark seller logins, one account per daily No value."""

from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ACCOUNTS_PATH = BASE_DIR / "accounts.json"


def account_for(number: int) -> tuple[str, str] | None:
    if not ACCOUNTS_PATH.exists():
        return None
    rows = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
    for row in rows:
        if int(row["no"]) == int(number):
            return str(row["email"]).strip(), str(row["password"])
    return None
