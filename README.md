# Torrent Studio

A self-hostable movie and series discovery app designed to run as **one Docker web service on Render**. The React/Vite frontend and FastAPI backend are deployed together—no separate frontend host or database service is needed for the default setup.

## Features

- Curated movie, series, anime, and regional catalogues
- Posters, synopses, ratings, and streaming details when available
- Cache-first catalogue data to reduce unnecessary live requests
- On-demand provider searches and personal Seedr device authorization

Provider data and availability depend on cached results, configuration, and third-party services.

## Deploy on Render

1. Fork this repository.
2. In Render, create a **Web Service** connected to your fork.
3. Select **Docker** and keep the root directory at the repository root.
4. Add the environment variable below and deploy.

| Variable | Required? | Purpose |
| --- | --- | --- |
| `SEEDR_SESSION_SECRET` | Yes | Long, random secret for protecting Seedr sessions |
| `TMDB_READ_ACCESS_TOKEN` or `TMDB_API_KEY` | Optional | TMDB metadata |
| `WATCHMODE_API_KEY` | Optional | Streaming availability |
| `OMDB_API_KEY` | Optional | Metadata fallback |
| `NEXSTREAM_API_KEY` | Optional | NexStream integration |
| `CACHE_ONLY_MODE` | Optional | Enable supported cache-only behavior |

Generate a session secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Add credentials only in Render's environment settings—never commit secrets. Render provides the `PORT` variable automatically.

## Run with Docker

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

Open [http://localhost:10000](http://localhost:10000). Check the backend at [http://localhost:10000/api/health](http://localhost:10000/api/health).

## Why one service?

- Vite builds the frontend during the Docker image build.
- FastAPI serves the frontend and API from the same origin.
- Bundled catalogue snapshots reduce repeated metadata requests.
- Optional integrations can be enabled only when needed.

This keeps deployment simple; actual resource usage depends on traffic, cache size, integrations, and the Render instance.

## Development

- **Frontend:** React + Vite (`frontend/`)
- **Backend:** FastAPI (`backend/`)
- **Deployment:** Docker (`Dockerfile`, `docker-compose.yml`)

For local development, install backend dependencies from `backend/requirements.txt` and frontend dependencies with `npm install` inside `frontend/`.

## Notes

- Catalogue seed data is in `backend/cache-seed/`; rebuild the image after changing it.
- Render's default filesystem may not persist across replacements. Use a persistent disk or an external cache-sync workflow for data that must survive.
- Follow the terms, quotas, and attribution requirements of any third-party provider you configure.
- **License:** No license file is included yet. Add one before inviting others to reuse or redistribute the code.

Contributions and bug reports are welcome. Please do not commit API keys, session secrets, or private configuration.
