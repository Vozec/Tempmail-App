"""
Shared provider registry — imported by both api.py and mcp_server.py.

Circuit breaker: a provider is auto-disabled after MAX_CONSECUTIVE_FAILURES
consecutive create_email failures. It can be re-enabled manually via the API.
Manually disabled providers are also skipped by get().

At startup, a background health-check probes all providers and auto-disables
any that are already unreachable (controlled by HEALTH_CHECK_ON_STARTUP env
var, default: true).
"""
import asyncio
import logging
import os
from typing import Optional

from .providers import (
    EmailProvider,
    GmailProvider,
    MailTickingProvider,
    MailTmProvider,
    TempAilProvider,
    TempMailIO,
    TempMailoProvider,
)

log = logging.getLogger(__name__)

_providers: dict[str, EmailProvider] = {}

# providers explicitly disabled by the user (survive re-enable only on demand)
_disabled: set[str] = set()

# consecutive create_email failure count per provider
_failures: dict[str, int] = {}

# auto-disable after this many consecutive create_email failures
MAX_CONSECUTIVE_FAILURES = 3

PRIORITY = [
    "gmail",        # IMAP +tag aliases (only if creds set)
    "mail.tm",      # clean REST API, real temp domains
    "tempmail.io",  # direct API
    "mailticking",  # Gmail +tag via FlareSolverr
    "tempmailo",    # FlareSolverr
    "tempail",      # FlareSolverr
]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(provider: EmailProvider) -> None:
    _providers[provider.name] = provider
    _failures.setdefault(provider.name, 0)


# ---------------------------------------------------------------------------
# Circuit-breaker helpers (called by api.py after create_email attempts)
# ---------------------------------------------------------------------------

def record_failure(name: str) -> None:
    """Increment failure counter; auto-disable on threshold."""
    _failures[name] = _failures.get(name, 0) + 1
    if _failures[name] >= MAX_CONSECUTIVE_FAILURES:
        if name not in _disabled:
            _disabled.add(name)
            log.warning(
                "registry: provider '%s' auto-disabled after %d consecutive failures",
                name, _failures[name],
            )


def record_success(name: str) -> None:
    """Reset failure counter (clears auto-disable too)."""
    _failures[name] = 0
    _disabled.discard(name)


# ---------------------------------------------------------------------------
# Manual disable / enable
# ---------------------------------------------------------------------------

def disable(name: str) -> None:
    if name not in _providers:
        raise KeyError(f"Provider '{name}' not found")
    _disabled.add(name)
    log.info("registry: provider '%s' manually disabled", name)


def enable(name: str) -> None:
    if name not in _providers:
        raise KeyError(f"Provider '{name}' not found")
    _disabled.discard(name)
    _failures[name] = 0
    log.info("registry: provider '%s' re-enabled", name)


def is_disabled(name: str) -> bool:
    return name in _disabled


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def get(name: Optional[str] = None) -> EmailProvider:
    if not _providers:
        raise RuntimeError("No email provider loaded")
    if name is None:
        for pname in PRIORITY:
            if pname in _providers and pname not in _disabled:
                return _providers[pname]
        # fallback: any non-disabled provider
        for pname, provider in _providers.items():
            if pname not in _disabled:
                return provider
        raise RuntimeError("All providers are disabled")
    provider = _providers.get(name)
    if provider is None:
        raise KeyError(f"Provider '{name}' not found. Available: {list(_providers)}")
    return provider


def all_providers() -> dict[str, EmailProvider]:
    return _providers


def list_names() -> list[str]:
    ordered = [p for p in PRIORITY if p in _providers]
    others = [p for p in _providers if p not in PRIORITY]
    return ordered + others


def provider_status() -> list[dict]:
    """Return all providers with their enabled/failure state."""
    result = []
    for name in list_names():
        result.append({
            "name": name,
            "disabled": name in _disabled,
            "failures": _failures.get(name, 0),
        })
    return result


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def _probe_and_disable(name: str, provider: EmailProvider) -> None:
    """Try create_email; disable the provider if it fails."""
    try:
        account = await asyncio.wait_for(provider.create_email(), timeout=30.0)
        log.info("startup probe: %s OK — %s", name, account.email)
        # clean up the test address (best-effort)
        try:
            await provider.delete_email(account)
        except Exception:
            pass
    except Exception as exc:
        _disabled.add(name)
        log.warning("startup probe: %s FAILED (%s) — auto-disabled", name, exc)


async def startup() -> None:
    register(TempMailIO())
    register(TempMailoProvider())
    register(MailTickingProvider())
    register(MailTmProvider())
    register(TempAilProvider())

    if os.getenv("GMAIL_EMAIL") and os.getenv("GMAIL_APP_PASSWORD"):
        register(GmailProvider())
        log.info("Gmail provider registered")
    else:
        log.info("Gmail provider skipped (GMAIL_EMAIL / GMAIL_APP_PASSWORD not set)")

    if os.getenv("HEALTH_CHECK_ON_STARTUP", "true").lower() not in ("0", "false", "no"):
        log.info("Running startup health-checks on all providers…")
        await asyncio.gather(*[
            _probe_and_disable(name, provider)
            for name, provider in _providers.items()
        ])


async def shutdown() -> None:
    for p in list(_providers.values()):
        if hasattr(p, "aclose"):
            await p.aclose()
    _providers.clear()
    _disabled.clear()
    _failures.clear()
