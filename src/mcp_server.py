"""
TempMail MCP server — exposes temp email tools to AI assistants.

Standalone (stdio):
    python -m src.mcp_server

Mounted in the FastAPI app at /mcp (streamable-http) — see api.py.

Claude Desktop config (~/.config/claude/claude_desktop_config.json):
    {
      "mcpServers": {
        "tempmail": {
          "command": "python",
          "args": ["-m", "src.mcp_server"],
          "cwd": "/path/to/tempmail"
        }
      }
    }
"""
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

from . import registry
from . import shared_store
from .providers import EmailAccount


@asynccontextmanager
async def _lifespan(server: FastMCP):
    shared_store.load()
    await registry.startup()
    yield
    await registry.shutdown()


mcp = FastMCP(
    "TempMail",
    lifespan=_lifespan,
    instructions=(
        "Use these tools to create and manage disposable email addresses. "
        "Always store the returned token — it is required for subsequent calls. "
        "Prefer create_email without arguments (auto-selects best provider). "
        "Poll get_messages every few seconds to wait for incoming mail."
    ),
)


# ---------------------------------------------------------------------------
# Provider management
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_providers() -> list[dict]:
    """
    List all available email providers with their current status.

    Returns a list of {name, disabled, failures} sorted by priority.
    """
    return registry.provider_status()


@mcp.tool()
async def disable_provider(name: str) -> dict:
    """
    Manually disable a provider so it is skipped for new emails.

    Args:
        name: Provider name (e.g. "mail.tm", "mailticking").
    """
    registry.disable(name)
    return {"name": name, "disabled": True}


@mcp.tool()
async def enable_provider(name: str) -> dict:
    """
    Re-enable a previously disabled provider and reset its failure counter.

    Args:
        name: Provider name.
    """
    registry.enable(name)
    return {"name": name, "disabled": False}


@mcp.tool()
async def get_domains(provider: Optional[str] = None) -> list[str]:
    """
    List available email domains for a provider.

    Args:
        provider: Provider name. Leave empty to use the default (highest priority) provider.
    """
    p = registry.get(provider)
    return await p.get_domains()


# ---------------------------------------------------------------------------
# Email lifecycle
# ---------------------------------------------------------------------------

@mcp.tool()
async def create_email(provider: Optional[str] = None) -> dict:
    """
    Create a new disposable email address.

    Args:
        provider: Provider name (e.g. "mail.tm", "tempmail.io"). Leave empty for auto.

    Returns:
        email: The generated email address.
        token: Auth token — store it, required for subsequent calls.
        provider: Which provider was used.
    """
    p = registry.get(provider)
    account = await p.create_email()
    registry.record_success(p.name)
    return {"email": account.email, "token": account.token, "provider": account.provider}


@mcp.tool()
async def get_messages(email: str, token: str, provider: str) -> list[dict]:
    """
    List messages in the inbox. Poll every few seconds to wait for new mail.

    Args:
        email: The email address (from create_email).
        token: The auth token (from create_email).
        provider: The provider name (from create_email).

    Returns:
        List of messages with id, from, subject, date.
        Use read_message to get the full body.
    """
    p = registry.get(provider)
    account = EmailAccount(email=email, token=token, provider=provider)
    messages = await p.get_messages(account)
    return [
        {"id": m.id, "from": m.from_addr, "subject": m.subject, "date": m.created_at}
        for m in messages
    ]


@mcp.tool()
async def read_message(email: str, message_id: str, token: str, provider: str) -> dict:
    """
    Read the full content of a specific message.

    Args:
        email: The email address.
        message_id: The message ID (from get_messages).
        token: The auth token.
        provider: The provider name.

    Returns:
        Full message with from, subject, body_text, body_html, attachments.
    """
    p = registry.get(provider)
    account = EmailAccount(email=email, token=token, provider=provider)
    m = await p.get_message(account, message_id)
    return {
        "id": m.id,
        "from": m.from_addr,
        "to": m.to_addr,
        "subject": m.subject,
        "date": m.created_at,
        "body_text": m.body_text,
        "body_html": m.body_html,
        "attachments": [
            {"filename": a.filename, "content_type": a.content_type, "size": a.size}
            for a in (m.attachments or [])
        ],
    }


@mcp.tool()
async def delete_email(email: str, token: str, provider: str) -> dict:
    """
    Delete / destroy the temporary email address.

    Args:
        email: The email address.
        token: The auth token.
        provider: The provider name.
    """
    p = registry.get(provider)
    account = EmailAccount(email=email, token=token, provider=provider)
    ok = await p.delete_email(account)
    return {"deleted": ok, "email": email}


# ---------------------------------------------------------------------------
# Shared / pinned emails
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_pinned() -> list[dict]:
    """
    List all pinned/shared email accounts (visible to every client).

    Returns a list of {email, token, provider, label, pinned_at}.
    """
    return shared_store.all_pinned()


@mcp.tool()
async def pin_email(email: str, token: str, provider: str, label: str = "") -> dict:
    """
    Pin an email address so all clients can see and reuse it.

    Args:
        email: The email address to pin.
        token: Auth token for this mailbox.
        provider: Provider name.
        label: Optional human-readable display name.
    """
    try:
        return shared_store.pin(email, token, provider, label)
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
async def unpin_email(email: str) -> dict:
    """
    Remove a pinned email address from the shared list.

    Args:
        email: The email address to unpin.
    """
    removed = shared_store.unpin(email)
    return {"unpinned": removed, "email": email}


@mcp.tool()
async def rename_email(email: str, new_label: str) -> dict:
    """
    Rename a pinned email by updating its display label.
    The actual email address is unchanged; only the label shown in the UI changes.

    Args:
        email: The email address (must already be pinned).
        new_label: The new display name.
    """
    entry = shared_store.rename(email, new_label)
    if entry is None:
        return {"error": f"{email!r} is not pinned — pin it first with pin_email"}
    return entry


# ---------------------------------------------------------------------------
# Entry point (standalone stdio mode)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
