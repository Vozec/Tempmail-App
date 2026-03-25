"""
Server-side shared/pinned email store.
Imported by both api.py and mcp_server.py so state is shared in-process.
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_SHARED_PATH = Path(os.getenv("SHARED_EMAILS_PATH", "shared_emails.json"))
_shared: list[dict] = []


def load() -> None:
    global _shared
    if _SHARED_PATH.exists():
        try:
            _shared = json.loads(_SHARED_PATH.read_text())
        except Exception:
            _shared = []


def _save() -> None:
    try:
        _SHARED_PATH.write_text(json.dumps(_shared, indent=2))
    except Exception as exc:
        log.warning("shared_store: could not save %s: %s", _SHARED_PATH, exc)


def all_pinned() -> list[dict]:
    return list(_shared)


def get(email: str) -> Optional[dict]:
    return next((e for e in _shared if e["email"] == email), None)


def pin(email: str, token: str, provider: str, label: str = "") -> dict:
    if any(e["email"] == email for e in _shared):
        raise ValueError(f"{email!r} is already pinned")
    entry = {
        "email": email,
        "token": token,
        "provider": provider,
        "label": label,
        "pinned_at": int(time.time()),
    }
    _shared.append(entry)
    _save()
    return entry


def unpin(email: str) -> bool:
    before = len(_shared)
    _shared[:] = [e for e in _shared if e["email"] != email]
    if len(_shared) == before:
        return False
    _save()
    return True


def rename(email: str, new_label: str) -> Optional[dict]:
    entry = next((e for e in _shared if e["email"] == email), None)
    if entry is None:
        return None
    entry["label"] = new_label
    _save()
    return entry
