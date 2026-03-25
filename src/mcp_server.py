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
    host="0.0.0.0",
    lifespan=_lifespan,
    streamable_http_path="/",
    instructions=(
        "Use these tools to create and manage disposable email addresses. "
        "Always store the returned token — it is required for subsequent calls. "
        "Prefer create_email without arguments (auto-selects best provider). "
        "Poll get_messages every few seconds to wait for incoming mail."
    ),
)


@mcp.tool()
async def list_providers() -> list[dict]:
    """List all providers with their current status (name, disabled, failures)."""
    return registry.provider_status()


@mcp.tool()
async def disable_provider(name: str) -> dict:
    """Disable a provider so it is skipped when creating new emails."""
    registry.disable(name)
    return {"name": name, "disabled": True}


@mcp.tool()
async def enable_provider(name: str) -> dict:
    """Re-enable a disabled provider and reset its failure counter."""
    registry.enable(name)
    return {"name": name, "disabled": False}


@mcp.tool()
async def get_domains(provider: Optional[str] = None) -> list[str]:
    """List available email domains for a provider (empty = auto)."""
    p = registry.get(provider)
    return await p.get_domains()


@mcp.tool()
async def create_email(provider: Optional[str] = None) -> dict:
    """
    Create a new disposable email address.
    Returns email, token (store it — required for subsequent calls), and provider.
    Leave provider empty to auto-select the best available one.
    """
    p = registry.get(provider)
    account = await p.create_email()
    registry.record_success(p.name)
    return {"email": account.email, "token": account.token, "provider": account.provider}


@mcp.tool()
async def get_messages(email: str, token: str, provider: str) -> list[dict]:
    """Poll the inbox. Returns a list of {id, from, subject, date}. Use read_message for full body."""
    p = registry.get(provider)
    account = EmailAccount(email=email, token=token, provider=provider)
    messages = await p.get_messages(account)
    return [
        {"id": m.id, "from": m.from_addr, "subject": m.subject, "date": m.created_at}
        for m in messages
    ]


@mcp.tool()
async def read_message(email: str, message_id: str, token: str, provider: str) -> dict:
    """Read the full content of a message (body_text, body_html, attachments)."""
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
    """Delete / destroy a temporary email address."""
    p = registry.get(provider)
    account = EmailAccount(email=email, token=token, provider=provider)
    ok = await p.delete_email(account)
    return {"deleted": ok, "email": email}


@mcp.tool()
async def list_pinned() -> list[dict]:
    """List all shared/pinned emails visible to every client."""
    return shared_store.all_pinned()


@mcp.tool()
async def pin_email(email: str, token: str, provider: str, label: str = "") -> dict:
    """Pin an email so all clients can see and reuse it."""
    try:
        return shared_store.pin(email, token, provider, label)
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
async def unpin_email(email: str) -> dict:
    """Remove a pinned email from the shared list."""
    removed = shared_store.unpin(email)
    return {"unpinned": removed, "email": email}


@mcp.tool()
async def rename_email(email: str, new_label: str) -> dict:
    """Update the display label of a pinned email (address is unchanged)."""
    entry = shared_store.rename(email, new_label)
    if entry is None:
        return {"error": f"{email!r} is not pinned — use pin_email first"}
    return entry


if __name__ == "__main__":
    mcp.run()
