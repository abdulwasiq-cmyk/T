# Torrent Studio

A self-hostable movie and series discovery app built to run as **one Docker web service on Render**.

▶️ **[Try the live app](https://torrent-studio-e5v2.onrender.com)**

## What it does

- Browse curated movies, TV series, anime, and regional catalogues.
- Discover posters, synopses, ratings, and streaming information when available.
- Find titles and use torrent/Seedr features to prepare transfers and manage files through your personal Seedr account.
- Use bundled catalogue data to reduce unnecessary live metadata requests.

Torrent availability and provider features depend on the configured services and available results. Use the app only with content you have the right to access.

## Run your own instance on Render

1. Fork this repository.
2. Create a **Web Service** in Render, connect your fork, and select **Docker**.
3. Add `SEEDR_SESSION_SECRET` as an environment variable, then deploy.

Generate a secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Optional integrations can use `TMDB_READ_ACCESS_TOKEN` / `TMDB_API_KEY`, `WATCHMODE_API_KEY`, `OMDB_API_KEY`, and `NEXSTREAM_API_KEY`. Add credentials in Render's environment settings—never commit secrets.

## Run locally with Docker

Requirements: Docker and the Docker Compose plugin.

```bash
git clone https://github.com/wroxtaaar/Torrent-Studio.git
cd Torrent-Studio
cp .env.example .env
```

Set `SEEDR_SESSION_SECRET` in `.env`, then run:

```bash
docker compose up --build -d
```

Open [http://localhost:10000](http://localhost:10000). Health check: [http://localhost:10000/api/health](http://localhost:10000/api/health).

## Tech stack

React + Vite · FastAPI · Docker · Render

The frontend and backend run in one service, and bundled catalogue snapshots help reduce repeated requests. Actual resource usage depends on traffic, cache size, integrations, and the Render instance.

## Notes

- Personal Seedr use requires a session secret; follow Seedr's terms and authorization flow.
- Render's default filesystem may not persist across replacements. Use persistent storage or a cache-sync workflow for data that must survive.
- Third-party providers have their own terms, quotas, and attribution requirements.
- **License:** No license file is included yet; add one before inviting reuse or redistribution.

Issues and contributions are welcome. Please never commit API keys, session secrets, or private configuration.
