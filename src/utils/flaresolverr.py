import httpx
from typing import Optional


class FlareSolverrClient:
    """
    Thin async wrapper around the FlareSolverr v1 REST API.
    Sessions maintain their cookie jar across requests, so always
    create a session once per provider and reuse it.
    """

    def __init__(self, url: str = "http://localhost:8191", timeout: float = 120.0):
        self.url = url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def _send(self, payload: dict) -> dict:
        resp = await self._client.post(f"{self.url}/v1", json=payload)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "ok":
            raise RuntimeError(f"FlareSolverr error: {data.get('message', 'unknown')}")
        return data["solution"]

    async def create_session(self, session_id: str) -> None:
        resp = await self._client.post(
            f"{self.url}/v1",
            json={"cmd": "sessions.create", "session": session_id},
        )
        resp.raise_for_status()

    async def destroy_session(self, session_id: str) -> None:
        try:
            await self._client.post(
                f"{self.url}/v1",
                json={"cmd": "sessions.destroy", "session": session_id},
            )
        except Exception:
            pass

    async def get(self, url: str, session_id: Optional[str] = None) -> dict:
        """GET request through FlareSolverr. Returns the solution dict."""
        payload: dict = {"cmd": "request.get", "url": url, "maxTimeout": 60000}
        if session_id:
            payload["session"] = session_id
        return await self._send(payload)

    async def post(
        self,
        url: str,
        body: str,
        headers: Optional[dict] = None,
        session_id: Optional[str] = None,
    ) -> dict:
        """
        POST request through FlareSolverr.
        `body` is sent as-is (raw string). Pass JSON strings for JSON APIs.
        `headers` allows overriding Content-Type, CSRF tokens, etc.
        """
        payload: dict = {
            "cmd": "request.post",
            "url": url,
            "postData": body,
            "maxTimeout": 60000,
        }
        if headers:
            payload["headers"] = headers
        if session_id:
            payload["session"] = session_id
        return await self._send(payload)

    async def get_clearance_cookies(self, url: str, session_id: Optional[str] = None) -> dict[str, str]:
        """
        Solve a Cloudflare challenge for `url` and return the resulting cookies
        as a plain {name: value} dict (suitable for httpx `cookies=`).
        """
        solution = await self.get(url, session_id=session_id)
        return {c["name"]: c["value"] for c in solution.get("cookies", [])}

    async def health_check(self) -> bool:
        """Ping FlareSolverr's /health endpoint."""
        try:
            resp = await self._client.get(f"{self.url}/health", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
