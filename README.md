# StreamRelay

Self-hosted Twitch viewer. Server resolves stream URLs via Streamlink — your browser plays them directly from Twitch's CDN using your own bandwidth. Ad filtering included.

## Run

```bash
docker build -t streamrelay .
docker run -d -p 8000:8000 --restart unless-stopped streamrelay
```

Open `http://localhost:8000`

Or with compose:

```bash
docker compose up -d
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `ALLOWED_ORIGINS` | `http://localhost:8000` | Comma-separated CORS origins |



## Deployment

For HTTPS / custom domain, put behind a reverse proxy and update `ALLOWED_ORIGINS`. The Twitch chat iframe requires HTTPS on non-localhost domains.
