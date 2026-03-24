import asyncio
import hashlib
import logging
import os
from typing import Optional

import httpx

from .base import EmailAccount, EmailProvider, Message
from ..utils.flaresolverr import FlareSolverrClient

log = logging.getLogger(__name__)

BASE_URL = "https://www.mailticking.com"

_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "fr,fr-FR;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:148.0) Gecko/20100101 Firefox/148.0",
}

# Mailbox types offered by /get-mailbox:
#   1 → abc@<temp-domain>      (non-Gmail temp domain)
#   2 → abc+tag@gmail.com      (Gmail plus addressing)  ← recommended
#   3 → a.b.c@gmail.com        (Gmail dot trick)
#   4 → abc@googlemail.com     (googlemail alias)
MAILBOX_TYPE_TEMP = "2"


def _email_code(email: str) -> str:
    """SHA-256 of the email — used as the auth token for /get-emails."""
    return hashlib.sha256(email.encode()).hexdigest()


def _is_cf_blocked(resp: httpx.Response) -> bool:
    """Detect a Cloudflare block/challenge page."""
    if resp.status_code in (403, 503):
        return True
    if resp.status_code == 200 and b"Just a moment" in resp.content:
        return True
    return False


def _parse_list_item(item: dict, to_addr: str) -> Message:
    return Message(
        id=item["Code"],
        from_addr=f'"{item.get("FromName", "")}" <{item.get("FromEmail", "")}>',
        to_addr=to_addr,
        subject=item.get("Subject", ""),
        body_text=None,
        body_html=None,
        created_at=str(item.get("SendTime", "")),
        attachments=[],
    )


class MailTickingProvider(EmailProvider):
    """
    Temp-mail provider for mailticking.com.

    Strategy:
      1. Try httpx directly (preserves Content-Type: application/json).
      2. If Cloudflare blocks the request, use FlareSolverr to solve the
         challenge and extract cf_clearance cookies, then retry with httpx
         using those cookies. This avoids FlareSolverr v2's header removal
         while still supporting CF-protected scenarios.

    The `token` field of EmailAccount is sha256(email), required as `code`
    in POST /get-emails.

    Optional env var:
      FLARESOLVERR_URL — default: http://localhost:8191
    """

    name = "mailticking"

    def __init__(
        self,
        flaresolverr_url: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        self._client = httpx.AsyncClient(headers=_HEADERS, timeout=timeout)
        self._fs = FlareSolverrClient(
            url=flaresolverr_url or os.getenv("FLARESOLVERR_URL", "http://localhost:8191")
        )
        self._cf_cookies: dict[str, str] = {}

    # ------------------------------------------------------------------ CF

    async def _solve_cf(self) -> None:
        """Use FlareSolverr to get CF clearance cookies, then cache them."""
        log.info("mailticking: Cloudflare detected, solving via FlareSolverr…")
        self._cf_cookies = await self._fs.get_clearance_cookies(BASE_URL)
        log.info("mailticking: CF clearance obtained (%d cookies)", len(self._cf_cookies))

    async def _get(self, url: str, **kwargs) -> httpx.Response:
        resp = await self._client.get(url, cookies=self._cf_cookies, **kwargs)
        if _is_cf_blocked(resp):
            await self._solve_cf()
            resp = await self._client.get(url, cookies=self._cf_cookies, **kwargs)
        return resp

    async def _post(self, url: str, **kwargs) -> httpx.Response:
        resp = await self._client.post(url, cookies=self._cf_cookies, **kwargs)
        if _is_cf_blocked(resp):
            await self._solve_cf()
            resp = await self._client.post(url, cookies=self._cf_cookies, **kwargs)
        return resp

    # ------------------------------------------------------------------ interface

    async def create_email(
        self,
        min_name_length: int = 10,
        max_name_length: int = 10,
        domain: Optional[str] = None,
    ) -> EmailAccount:
        # 1. Get a new mailbox address
        resp = await self._post(
            f"{BASE_URL}/get-mailbox",
            json={"types": [MAILBOX_TYPE_TEMP]},
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"mailticking: /get-mailbox failed: {data}")
        email = data["email"]

        # 2. Activate it (sets active_mailbox + temp_mail_history cookies)
        await self._post(f"{BASE_URL}/activate-email", json={"email": email})

        return EmailAccount(email=email, token=_email_code(email), provider=self.name)

    async def _reactivate(self, email: str) -> None:
        """Re-activate a mailbox to restore the active_mailbox session cookie."""
        await self._post(f"{BASE_URL}/activate-email", json={"email": email})

    async def get_messages(self, account: EmailAccount) -> list[Message]:
        resp = await self._post(
            f"{BASE_URL}/get-emails?lang=",
            json={"email": account.email, "code": account.token},
        )
        if resp.status_code == 400:
            # Cookie lost (server restart) — solve CF first, then re-activate and retry
            log.info("mailticking: /get-emails 400, solving CF + re-activating %s", account.email)
            await self._solve_cf()
            await self._reactivate(account.email)
            resp = await self._post(
                f"{BASE_URL}/get-emails?lang=",
                json={"email": account.email, "code": account.token},
            )
        if resp.status_code == 429:
            log.warning("mailticking: rate limited, backing off 15s")
            await asyncio.sleep(15)
            resp = await self._post(
                f"{BASE_URL}/get-emails?lang=",
                json={"email": account.email, "code": account.token},
            )
        if not resp.is_success:
            log.warning("mailticking: /get-emails %d — %s", resp.status_code, resp.text[:300])
            return []
        try:
            data = resp.json()
        except Exception:
            log.warning("mailticking: /get-emails non-JSON response — %s", resp.text[:300])
            return []
        if not data.get("success"):
            log.debug("mailticking: /get-emails success=false — %s", data)
            return []
        return [_parse_list_item(item, account.email) for item in data.get("emails", [])]

    async def get_message(self, account: EmailAccount, message_id: str) -> Message:
        """
        Merges metadata from /get-emails (from, subject…) with the HTML body
        from /mail/gmail-content/{id}.
        """
        resp = await self._get(f"{BASE_URL}/mail/gmail-content/{message_id}")
        if not resp.is_success:
            log.warning("mailticking: /gmail-content %d — %s", resp.status_code, resp.text[:300])
            raise RuntimeError(f"mailticking: /gmail-content returned {resp.status_code}")
        try:
            content_data = resp.json()
        except Exception:
            log.warning("mailticking: /gmail-content non-JSON — %s", resp.text[:300])
            content_data = {}
        body_html = content_data.get("result", {}).get("content")

        messages = await self.get_messages(account)
        base = next((m for m in messages if m.id == message_id), None)

        if base:
            return Message(
                id=base.id,
                from_addr=base.from_addr,
                to_addr=base.to_addr,
                subject=base.subject,
                body_text=None,
                body_html=body_html,
                created_at=base.created_at,
                attachments=[],
            )

        result = content_data.get("result", {})
        return Message(
            id=message_id,
            from_addr=f'"{result.get("from_name", "")}" <{result.get("from", "")}>',
            to_addr=result.get("receiver", account.email),
            subject=result.get("subject", ""),
            body_text=None,
            body_html=body_html,
            created_at=str(result.get("send_time", "")),
            attachments=[],
        )

    async def delete_email(self, account: EmailAccount) -> bool:
        """Destroy the active mailbox (POST /destroy)."""
        resp = await self._post(
            f"{BASE_URL}/destroy",
            headers={**_HEADERS, "X-Requested-With": "XMLHttpRequest"},
            content=b"",
        )
        try:
            return bool(resp.json().get("success"))
        except Exception:
            return False

    async def get_domains(self) -> list[str]:
        return ["gmail.com"]

    async def health_check(self) -> bool:
        try:
            resp = await self._post(
                f"{BASE_URL}/get-mailbox",
                json={"types": [MAILBOX_TYPE_TEMP]},
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._fs.aclose()
