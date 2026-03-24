import json
import logging
import os
import random
import re
import uuid
from typing import Optional

from .base import Attachment, EmailAccount, EmailProvider, Message
from ..utils.flaresolverr import FlareSolverrClient

log = logging.getLogger(__name__)

BASE_URL = "https://tempmailo.com"

# Headers sent on every API call (inside the FlareSolverr session)
_API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr,fr-FR;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/json;charset=utf-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
}


_EMAIL_RE = re.compile(r'^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]{2,}$')


def _is_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value.strip()))


def _extract_csrf(html: str) -> Optional[str]:
    """Find the ASP.NET Core antiforgery token in the page HTML."""
    for pattern in [
        r'<input[^>]+name="__RequestVerificationToken"[^>]+value="([^"]+)"',
        r'<input[^>]+value="([^"]+)"[^>]+name="__RequestVerificationToken"',
        r'"requestVerificationToken"\s*:\s*"([^"]+)"',
    ]:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _extract_domains(html: str) -> list[str]:
    """Parse available domains from the homepage HTML."""
    # tempmailo typically embeds domain options in the page
    domains = re.findall(r'@([\w-]+\.[\w.]{2,})', html)
    unique = list(dict.fromkeys(domains))  # preserve order, deduplicate
    return unique if unique else ["forexzig.com", "fxzig.com"]


def _parse_message(data: dict) -> Message:
    return Message(
        id=data["id"],
        from_addr=data.get("from", ""),
        to_addr=data.get("to", ""),
        subject=data.get("subject", ""),
        body_text=data.get("text"),
        body_html=data.get("html"),
        created_at=data.get("date", ""),
        attachments=[],
    )


class TempMailoProvider(EmailProvider):
    """
    Temp-mail provider for tempmailo.com.

    Uses FlareSolverr to bypass Cloudflare.  The provider creates one
    persistent FlareSolverr browser session; cookies (cf_clearance, CSRF)
    are maintained automatically inside that session.

    Required env var (or constructor arg):
      FLARESOLVERR_URL  — default: http://localhost:8191
    """

    name = "tempmailo"

    def __init__(self, flaresolverr_url: Optional[str] = None) -> None:
        url = flaresolverr_url or os.getenv("FLARESOLVERR_URL", "http://localhost:8191")
        self._fs = FlareSolverrClient(url=url)
        self._session_id: Optional[str] = None
        self._csrf_token: Optional[str] = None
        self._domains: list[str] = []

    # ------------------------------------------------------------------ session

    async def _init_session(self) -> None:
        """Create a FlareSolverr browser session and solve the CF challenge."""
        sid = f"tempmailo_{uuid.uuid4().hex[:8]}"
        await self._fs.create_session(sid)
        self._session_id = sid

        solution = await self._fs.get(BASE_URL, session_id=sid)
        html = solution.get("response", "")
        self._csrf_token = _extract_csrf(html)
        self._domains = _extract_domains(html)

    async def _ensure_session(self) -> None:
        if not self._session_id:
            await self._init_session()

    async def _refresh_session(self) -> None:
        """Destroy the current session and start a fresh one (e.g. after 403)."""
        if self._session_id:
            await self._fs.destroy_session(self._session_id)
        self._session_id = None
        self._csrf_token = None
        await self._init_session()

    # ------------------------------------------------------------------ helpers

    def _api_headers(self) -> dict:
        headers = dict(_API_HEADERS)
        if self._csrf_token:
            headers["Requestverificationtoken"] = self._csrf_token
        return headers

    # ------------------------------------------------------------------ interface

    async def create_email(
        self,
        min_name_length: int = 10,
        max_name_length: int = 10,
        domain: Optional[str] = None,
    ) -> EmailAccount:
        await self._ensure_session()
        r = f"{random.random():.16f}"
        solution = await self._fs.get(
            f"{BASE_URL}/changemail?_r={r}",
            session_id=self._session_id,
        )
        email = solution.get("response", "").strip()
        if not _is_email(email):
            log.warning("tempmailo: /changemail non-email (attempt 1): %r", email[:200])
            # Session may have expired — retry once with a fresh session
            await self._refresh_session()
            r2 = f"{random.random():.16f}"
            solution = await self._fs.get(
                f"{BASE_URL}/changemail?_r={r2}",
                session_id=self._session_id,
            )
            email = solution.get("response", "").strip()
        if not _is_email(email):
            log.warning("tempmailo: /changemail non-email (attempt 2): %r", email[:200])
            raise RuntimeError(f"tempmailo: /changemail returned non-email: {email[:120]!r}")
        return EmailAccount(email=email, token="", provider=self.name)

    async def get_messages(self, account: EmailAccount) -> list[Message]:
        await self._ensure_session()
        body = json.dumps({"mail": account.email})
        solution = await self._fs.post(
            BASE_URL,
            body=body,
            headers=self._api_headers(),
            session_id=self._session_id,
        )
        raw = solution.get("response", "[]")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [_parse_message(m) for m in data]

    async def get_message(self, account: EmailAccount, message_id: str) -> Message:
        messages = await self.get_messages(account)
        for m in messages:
            if m.id == message_id:
                return m
        raise ValueError(f"Message '{message_id}' not found for {account.email}")

    async def delete_email(self, account: EmailAccount) -> bool:
        # tempmailo has no delete endpoint; the address is simply abandoned.
        return True

    async def get_domains(self) -> list[str]:
        await self._ensure_session()
        return self._domains

    async def health_check(self) -> bool:
        return await self._fs.health_check()

    async def aclose(self) -> None:
        if self._session_id:
            await self._fs.destroy_session(self._session_id)
        await self._fs.aclose()
