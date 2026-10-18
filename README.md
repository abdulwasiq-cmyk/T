# Torrent Studio

A self-hostable movie and series discovery app built to run as **one Docker web service on Render**. The React/Vite frontend is compiled into static files and served by the FastAPI backend, so you don't need separate frontend hosting, a database service.

> **Deployment goal:** keep the production setup simple and cache-first, so a small app can serve catalogue pages without making a live metadata request for every card.

## What it does

- **Browse curated catalogues:** Marvel, DC live-action and animated titles, classics, anime, web series, and regional movie collections.
- **Explore movie metadata:** poster art, synopsis, ratings when available, and India streaming availability where cached or supported by configured providers.
- **Search and prepare titles:** provider searches are triggered by the relevant user action rather than automatically for every catalogue card.
- **Connect a personal Seedr account:** use the app's device authorization flow to manage transfers and files through the connected account.
- **Use prebuilt data:** catalogue and metadata snapshots are bundled with the repository and prepared during the Docker image build.
- **Run as one service:** frontend and API share the same origin, reducing deployment complexity and avoiding cross-origin setup for the default configuration.

Availability, ratings, posters, and provider results depend on the data available from the configured services; a missing listing does not necessarily mean a title is unavailable.

## How it is deployed

```text
Browser
   │
   ▼
Single Render Web Service (Docker)
   ├── React + Vite frontend (built static assets)
   └── FastAPI backend
        ├── bundled catalogue/cache snapshots
        └── external metadata and playback providers (optional/configured)
```

The root `Dockerfile` builds the frontend with Node.js, installs the Python backend dependencies, prepares catalogue pages, and starts Uvicorn on Render's `PORT`. There is no separate frontend service.

### Keeping it lightweight

- **One web service:** no separate frontend host or application database is required for the default setup.
- **Static frontend build:** Vite assets are generated at image-build time and served by the backend.
- **Cache-first catalogue loading:** bundled snapshots provide prebuilt data for supported catalogue views.
- **On-demand work:** provider searches and selected metadata operations run when needed instead of being required for every initial card.
- **Optional API credentials:** external metadata enrichments can be configured server-side; keep credentials out of the frontend and source control.

These choices reduce unnecessary services and requests; they are not a promise of a specific RAM/CPU footprint. Actual usage depends on traffic, cache size, enabled integrations, and the Render instance type. No benchmark numbers are claimed here.

## Deploy on Render

1. Fork this repository to your GitHub account.
2. In Render, create a **New Web Service** and connect your fork.
3. Choose **Docker** as the runtime and leave the **Root Directory** at the repository root.
4. Deploy the service. Render supplies the `PORT` environment variable, which the container uses automatically.
5. Add the environment variables below in Render's service settings.

### Required environment variable

| Variable | Purpose |
| --- | --- |
| `SEEDR_SESSION_SECRET` | A long, random secret used to protect personal Seedr sessions. Set a stable value and keep it private. |

Generate a value locally with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Optional environment variables

| Variable | Purpose |
| --- | --- |
| `CACHE_ONLY_MODE` | Set to `true` to prefer the cache-only behavior supported by the backend. |
| `CORS_ORIGINS` | Comma-separated allowed frontend origins. For the single-service deployment, same-origin requests are the default. |
| `TMDB_READ_ACCESS_TOKEN` or `TMDB_API_KEY` | Optional TMDB metadata/discovery access. |
| `WATCHMODE_API_KEY` or `WATCHMODE_API_KEYS` | Optional Watchmode streaming availability enrichment. |
| `OMDB_API_KEY` or `OMDB_API_KEYS` | Optional OMDb metadata fallback. |
| `NEXSTREAM_API_KEY` | Optional NexStream integration where required by the enabled backend workflow. |

Configure only the integrations you intend to use. Provider credentials belong in Render's **Environment** settings, never in committed files or frontend variables. Respect each provider's terms, rate limits, and quotas.

For personal Seedr connections, use `SEEDR_SESSION_SECRET`; the normal per-user flow does not require a shared developer Seedr token. Do not enable legacy shared-token behavior for a multi-user public deployment.

## Run locally with Docker

### Requirements

- Docker Engine
- Docker Compose plugin

Clone the repository and enter it:

```bash
git clone https://github.com/wroxtaaar/Torrent-Studio.git
cd Torrent-Studio
cp .env.example .env
```

Edit `.env` and replace `SEEDR_SESSION_SECRET` with a long random value. Then run:

```bash
docker compose up --build -d
```

Open [http://localhost:10000](http://localhost:10000).

Useful commands:

```bash
# Follow logs
docker compose logs -f torrent-studio

# Check status
docker compose ps

# Stop the app
docker compose down
```

By default, Compose maps host port `10000` to container port `10000`. Set `HOST_PORT` in `.env` to use a different host port. Compose keeps the app's `/app/data` directory in a named volume; deleting the volume with `docker compose down -v` removes that persisted local data.

## Local development

### Backend

```bash
cd backend
python -m venv .venv
# Activate the virtual environment, then:
pip install -r requirements.txt
uvicorn main:app --reload
```

### Frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Configure the frontend API URL for your local backend as needed. The production Docker image instead builds the frontend and serves it from the same origin as FastAPI.

## Configuration and data notes

- Catalogue seed files live under `backend/cache-seed/` and are prepared during image build.
- Rebuild and redeploy the Docker image after changing source catalogues or poster mappings.
- The container's writable filesystem on a typical Render service should not be treated as permanent storage across replacements or restarts. If your workflow writes data that must survive deployments, configure an appropriate persistent disk or an external storage/snapshot workflow.
- External metadata services can rate-limit requests or change their APIs. Cache availability and provider results may vary over time.
- Keep `.env`, API keys, OAuth/session secrets, and private deployment configuration out of Git.

## API health check

The backend exposes:

```text
GET /api/health
```

On a local Docker run, check [http://localhost:10000/api/health](http://localhost:10000/api/health).

## Project layout

```text
.
├── backend/             # FastAPI application, integrations and cache seeds
├── frontend/            # React + Vite application
├── Dockerfile           # Single-container production build
├── docker-compose.yml   # Local Docker setup
├── .env.example         # Safe configuration template
└── search_limits.json   # Search limits shared with the frontend/backend
```

## Open-source status and attribution

This repository includes integrations with third-party services. Their APIs, content, trademarks, and usage restrictions remain governed by their respective terms. In particular, configure TMDB attribution and comply with the terms for any metadata or streaming provider you enable.

**License:** A license file has not yet been added to this repository. Until a license is published, do not assume the code is available for unrestricted reuse. Add a license before accepting contributions or inviting others to reuse or redistribute the project.

## Contributing

Issues and pull requests are welcome. Please:

1. Describe the change and the behavior it addresses.
2. Avoid committing secrets, personal tokens, or private configuration.
3. Include reproduction steps for bugs and validation details for behavior changes.
4. Keep new integrations optional and avoid unnecessary per-card network requests.

---

Built with React, Vite, FastAPI, and Docker; designed for a simple single-service Render deployment.
