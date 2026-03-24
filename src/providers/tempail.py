import logging
import os
import re
import uuid
from typing import Optional
from urllib.parse import urlencode

from .base import Attachment, EmailAccount, EmailProvider, Message
from ..utils.flaresolverr import FlareSolverrClient

log = logging.getLogger(__name__)

BASE_URL = "https://tempail.com"
LANG = "en"

_FORM_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/{LANG}/",
}


# ---------------------------------------------------------------------------
# HTML parsers
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r'^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]{2,}$')


def _extract_email(html: str) -> Optional[str]:
    # Try known input field patterns first
    for pat in [
        r'id="eposta_adres"[^>]+value="([^"]+)"',
        r'value="([^"@\s]+@[^"\s]+)"[^>]+id="eposta_adres"',
        r'id=["\']eposta_adres["\'][^>]+value=["\']([^"\']+)["\']',
        r'name=["\']eposta_adres["\'][^>]+value=["\']([^"\']+)["\']',
        r'value=["\']([^"\']+)["\'][^>]+name=["\']eposta_adres["\']',
        # JSON embedded in page
        r'"email"\s*:\s*"([^"@\s]+@[^"\s"]+)"',
        r'"adres"\s*:\s*"([^"@\s]+@[^"\s"]+)"',
        # Generic: any input value that looks like an email
        r'<input[^>]+value="([^"@\s<>]+@[^"@\s<>]+\.[^"@\s<>]{2,})"',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if _EMAIL_RE.match(val):
                return val
    return None


def _extract_oturum(html: str) -> Optional[str]:
    m = re.search(r'var oturum="([^"]+)"', html)
    return m.group(1) if m else None


def _extract_tarih(html: str) -> Optional[str]:
    # Both the initial page and kontrol responses embed the timestamp
    m = re.search(r'(?:var )?tarih="(\d+)"', html)
    return m.group(1) if m else None


def _parse_message_list(html: str, to_addr: str) -> list[Message]:
    messages = []
    for item_m in re.finditer(
        r'<li[^>]+id="mail_(\d+)"[^>]*>(.*?)</li>', html, re.DOTALL
    ):
        mail_id = item_m.group(1)
        item_html = item_m.group(2)

        sender = re.search(r'class="gonderen"[^>]*>([^<]*)<', item_html)
        subject = re.search(r'class="baslik"[^>]*>([^<]*)<', item_html)
        time_ = re.search(r'class="zaman"[^>]*>([^<]*)<', item_html)

        messages.append(Message(
            id=mail_id,
            from_addr=sender.group(1).strip() if sender else "",
            to_addr=to_addr,
            subject=subject.group(1).strip() if subject else "",
            body_text=None,
            body_html=None,
            created_at=time_.group(1).strip() if time_ else "",
            attachments=[],
        ))
    return messages


def _extract_message_hash(html: str, mail_id: str) -> Optional[str]:
    """Hash used in sil/duzelt calls: sil_posta("ed9c0","3896112192")"""
    m = re.search(rf'sil_posta\("([^"]+)",\s*"{re.escape(mail_id)}"\)', html)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class TempAilProvider(EmailProvider):
    """
    Temp-mail provider for tempail.com.

    Uses FlareSolverr to bypass Cloudflare. Session state (oturum, tarih)
    is maintained inside a FlareSolverr browser session.

    The `token` field of EmailAccount is the `oturum` session value.

    Required env var:
      FLARESOLVERR_URL  — default: http://localhost:8191
    """

    name = "tempail"

    def __init__(self, flaresolverr_url: Optional[str] = None) -> None:
        url = flaresolverr_url or os.getenv("FLARESOLVERR_URL", "http://localhost:8191")
        self._fs = FlareSolverrClient(url=url)
        self._session_id: Optional[str] = None
        self._tarih: str = "0"
        self._domains: list[str] = []

    # ------------------------------------------------------------------ session

    async def _init_session(self) -> None:
        sid = f"tempail_{uuid.uuid4().hex[:8]}"
        await self._fs.create_session(sid)
        self._session_id = sid

    async def _ensure_session(self) -> None:
        if not self._session_id:
            await self._init_session()

    async def _refresh_session(self) -> None:
        if self._session_id:
            await self._fs.destroy_session(self._session_id)
        self._session_id = None
        self._tarih = "0"
        await self._init_session()

    # ------------------------------------------------------------------ interface

    async def create_email(
        self,
        min_name_length: int = 10,
        max_name_length: int = 10,
        domain: Optional[str] = None,
    ) -> EmailAccount:
        # Fresh session → new oturum cookie + new random email
        await self._refresh_session()
        solution = await self._fs.get(
            f"{BASE_URL}/{LANG}/", session_id=self._session_id
        )
        html = solution.get("response", "")

        # Detect bot-protection pages (reCAPTCHA, Datadome, etc.)
        if "recaptcha" in html.lower() or "verifying your request" in html.lower() or "datadome" in html.lower():
            log.warning("tempail: bot-check page detected (reCAPTCHA/other), cannot solve. HTML[:200]: %s", html[:200])
            raise RuntimeError("tempail: bot-check page returned — unsolvable by FlareSolverr")

        email = _extract_email(html)
        oturum = _extract_oturum(html)
        tarih = _extract_tarih(html)

        if not email:
            log.warning("tempail: could not extract email. HTML snippet: %s", html[:800])
            raise RuntimeError("tempail: could not extract email from homepage")
        if not oturum:
            raise RuntimeError("tempail: could not extract oturum from homepage")

        if tarih:
            self._tarih = tarih
        if email and "@" in email:
            d = email.split("@")[1]
            if d not in self._domains:
                self._domains.append(d)

        return EmailAccount(email=email, token=oturum, provider=self.name)

    async def get_messages(self, account: EmailAccount) -> list[Message]:
        await self._ensure_session()
        body = urlencode({
            "oturum": account.token,
            "tarih": self._tarih,
            "geri_don": f"{BASE_URL}/{LANG}/",
        })
        solution = await self._fs.post(
            f"{BASE_URL}/{LANG}/api/kontrol/",
            body=body,
            headers=_FORM_HEADERS,
            session_id=self._session_id,
        )
        html = solution.get("response", "")
        new_tarih = _extract_tarih(html)
        if new_tarih:
            self._tarih = new_tarih
        return _parse_message_list(html, account.email)

    async def get_message(self, account: EmailAccount, message_id: str) -> Message:
        await self._ensure_session()

        # Fetch message page (for hash + from extraction)
        solution = await self._fs.get(
            f"{BASE_URL}/{LANG}/mail_{message_id}/",
            session_id=self._session_id,
        )
        msg_html = solution.get("response", "")

        # Fetch message body from iframe endpoint
        content = await self._fs.get(
            f"{BASE_URL}/{LANG}/api/icerik/?oturum={account.token}&mail_no={message_id}",
            session_id=self._session_id,
        )
        body_html = content.get("response", "")

        # Try to get metadata from the list (from_addr, subject)
        messages = await self.get_messages(account)
        base = next((m for m in messages if m.id == message_id), None)

        if base:
            return Message(
                id=message_id,
                from_addr=base.from_addr,
                to_addr=base.to_addr,
                subject=base.subject,
                body_text=None,
                body_html=body_html,
                created_at=base.created_at,
                attachments=[],
            )

        # Fallback — extract sender from page (may be CF-obfuscated)
        from_m = re.search(
            r'class="mail-oku-gonderen"[^>]*>.*?&lt;([^&]+)&gt;', msg_html, re.DOTALL
        )
        return Message(
            id=message_id,
            from_addr=from_m.group(1).strip() if from_m else "",
            to_addr=account.email,
            subject="",
            body_text=None,
            body_html=body_html,
            created_at="",
            attachments=[],
        )

    async def delete_message(
        self, account: EmailAccount, message_id: str, message_hash: str
    ) -> bool:
        """Delete a specific message (requires the hash from the message page)."""
        await self._ensure_session()
        # veri[] is a PHP array: first element = hash, second = numeric id
        body = (
            f"oturum={account.token}"
            f"&veri[]={message_hash}"
            f"&veri[]={message_id}"
        )
        await self._fs.post(
            f"{BASE_URL}/{LANG}/api/sil/",
            body=body,
            headers=_FORM_HEADERS,
            session_id=self._session_id,
        )
        return True

    async def delete_email(self, account: EmailAccount) -> bool:
        """Destroy the entire inbox (POST /api/yoket/)."""
        await self._ensure_session()
        body = urlencode({"oturum": account.token})
        await self._fs.post(
            f"{BASE_URL}/{LANG}/api/yoket/",
            body=body,
            headers=_FORM_HEADERS,
            session_id=self._session_id,
        )
        return True

    async def get_domains(self) -> list[str]:
        await self._ensure_session()
        return self._domains if self._domains else ["necub.com"]

    async def health_check(self) -> bool:
        if not await self._fs.health_check():
            return False
        try:
            sid = f"tempail_hc_{uuid.uuid4().hex[:6]}"
            await self._fs.create_session(sid)
            try:
                solution = await self._fs.get(f"{BASE_URL}/{LANG}/", session_id=sid)
                html = solution.get("response", "")
                return bool(_extract_email(html))
            finally:
                await self._fs.destroy_session(sid)
        except Exception:
            return False

    async def aclose(self) -> None:
        if self._session_id:
            await self._fs.destroy_session(self._session_id)
        await self._fs.aclose()
