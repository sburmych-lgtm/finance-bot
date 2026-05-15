# Ruby Finance — Telegram Mini App

Luxury fintech Mini App, served as static HTML+CSS+JS through a tiny
`aiohttp` server. Talks to the bot service's `/api/*` endpoints with
HMAC-validated `initData`.

## Run locally

```bash
cd miniapp
pip install -r requirements.txt
python server.py            # serves on http://localhost:8080
```

Or just open `index.html` directly through any static dev server.

## Deploy on Railway

This folder is the root of the `miniapp` service. The bot lives in the
parent folder as its own `worker` service.

Env vars on the Railway service:
- `PORT` — provided automatically by Railway.
- `API_BASE_URL` — full HTTPS URL of the bot service that exposes `/api/*`.

Once deployed, get the public domain, then set the Mini App URL in
@BotFather (Bot Settings → Menu Button → Configure Menu Button →
`Open Web App` → paste URL).
