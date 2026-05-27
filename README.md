# StreamRelay

Self-hosted Twitch viewer. Server resolves stream URLs via Streamlink — your browser plays them directly from Twitch's CDN using your own bandwidth. Ad filtering included.

## Run

```bash
docker compose up -d
```

Open `http://localhost:8000`

## Configuration

| Variable | Default | Description |
|---|---|---|
| `ALLOWED_ORIGINS` | `http://localhost:8000` | Comma-separated CORS origins |

For HTTPS / custom domain, put behind a reverse proxy (Caddy, nginx) and update `ALLOWED_ORIGINS`.
