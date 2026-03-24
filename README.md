# tempmail-api

A self-hosted temporary email REST API with a built-in web UI and optional MCP server.
Multiple disposable-inbox providers are aggregated behind a single API; broken providers are automatically detected and disabled at startup.

---

## Features

- **Multi-provider** — mail.tm, tempmail.io, Gmail (IMAP), mailticking, tempmailo, tempail
- **Auto-fallback** — picks the first healthy provider in priority order
- **Circuit breaker** — providers that fail 3× in a row are auto-disabled; re-enable via API
- **Startup health-check** — broken providers are disabled before the first request
- **Web UI** — two-column email client, multi-account, localStorage persistence, auto-refresh
- **Swagger docs** — `/docs`
- **MCP server** — optional, usable standalone by Claude / any MCP client
- **Cloudflare bypass** — via [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) for providers that require it

---

## Quick start (Docker Compose)

```bash
cp .env.example .env
# edit .env if needed (Gmail credentials, ports, …)
docker compose up -d
```

- Web UI → http://localhost:8000
- Swagger → http://localhost:8000/docs

---

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8000` | HTTP port |
| `RELOAD` | `false` | Uvicorn hot-reload (dev only) |
| `ENABLE_FRONTEND` | `true` | Serve the web UI |
| `FLARESOLVERR_URL` | `http://localhost:8191` | FlareSolverr endpoint |
| `HEALTH_CHECK_ON_STARTUP` | `true` | Probe all providers at startup and disable failures |
| `GMAIL_EMAIL` | — | Gmail address (for Gmail IMAP provider) |
| `GMAIL_APP_PASSWORD` | — | Gmail app password |

---

## REST API

### Providers

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/providers` | List providers with status (`disabled`, `failures`) |
| `POST` | `/api/providers/{name}/disable` | Manually disable a provider |
| `POST` | `/api/providers/{name}/enable` | Re-enable a provider (resets failure count) |
| `GET` | `/api/health` | Health status of all providers |

### Email

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/email?name=<provider>` | Create a new temporary address |
| `GET` | `/api/email/{email}/messages?token=X&name=Y` | List messages |
| `GET` | `/api/email/{email}/message/{id}?token=X&name=Y` | Get full message (with body) |
| `DELETE` | `/api/email/{email}?token=X&name=Y` | Delete the mailbox |
| `GET` | `/api/domains?name=<provider>` | List available domains |

`name` is optional everywhere — omitted means "auto" (first healthy provider).

---

## Providers

Priority order (first healthy provider wins):

| Priority | Name | Type | Requires FlareSolverr | Notes |
|---|---|---|---|---|
| 1 | `gmail` | IMAP | No | Needs `GMAIL_EMAIL` + `GMAIL_APP_PASSWORD` |
| 2 | `mail.tm` | REST API | No | Most reliable fallback |
| 3 | `tempmail.io` | REST API | No | Reliable |
| 4 | `mailticking` | Scraping | Yes | CF-protected |
| 5 | `tempmailo` | Scraping | Yes | CF-protected |
| 6 | `tempail` | Scraping | Yes | May be blocked by reCAPTCHA |

At startup, each provider is probed with a real `create_email` call. Any provider that fails is auto-disabled before serving requests.

---

## MCP server (optional)

```bash
python -m src.mcp_server
```

Tools exposed: `list_providers`, `get_domains`, `create_email`, `get_messages`, `read_message`, `delete_email`.

Claude Desktop config (`~/.config/claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "tempmail": {
      "command": "python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/path/to/tempmail-api"
    }
  }
}
```

---

## Development

```bash
pip install -r src/requirements.txt
RELOAD=true python -m src.main
```

FlareSolverr (required for CF-protected providers):

```bash
docker run -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest
```
