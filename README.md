# Torrent Studio

Dockerized build of the uploaded `new-test-main.zip` project.

## Run with Docker Compose

Requirements: Docker Engine and the Docker Compose plugin.

1. Create a local environment file:

   ```bash
   cp .env.example .env
   ```

2. Generate a private session-encryption secret and put it in `.env`:

   ```bash
   openssl rand -hex 32
   ```

   Replace `SEEDR_SESSION_SECRET` in `.env` with the generated value. Do not commit `.env`.

3. Build and start the app:

   ```bash
   docker compose up -d --build
   ```

4. Open [http://localhost:10000](http://localhost:10000).

Useful commands:

```bash
docker compose ps
docker compose logs -f torrent-studio
docker compose restart torrent-studio
docker compose down
```

The app's data directory is persisted in the named Docker volume `torrent_studio_data`. `docker compose down` keeps the volume; `docker compose down -v` deletes it.

## What the Docker build does

The build extracts the uploaded ZIP into a temporary build stage, builds the React/Vite frontend, installs the FastAPI backend requirements, and packages both into one container. The ZIP's top-level folder name does not matter as long as it contains the expected `frontend/`, `backend/`, `search_limits.json`, and root `Dockerfile` files.

The app listens on container port `10000`. Change `HOST_PORT` in `.env` if port 10000 is already in use, for example `HOST_PORT=10001`.

## Configuration and security

- `CACHE_ONLY_MODE=true` is the default, to avoid unnecessary live metadata API calls.
- `SEEDR_SESSION_SECRET` must be stable across restarts so encrypted personal Seedr sessions remain usable.
- Add optional provider API keys to the local `.env` only when required. Never commit real keys.
- If accessed from a public URL or a different frontend origin, set `CORS_ORIGINS` to the exact trusted origin(s).
- The application may require outbound network access for provider integrations and Seedr authorization. Provider free tiers and API limits still apply.

## Build without Compose

```bash
docker build -t torrent-studio:local .
docker run --rm -p 10000:10000 --env-file .env -v torrent_studio_data:/app/data torrent-studio:local
```

The source archive is retained in this repository; Docker extracts it only during image build, so the application source is not yet expanded into individual GitHub files.
