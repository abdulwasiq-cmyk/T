# Torrent Studio — Vercel + Render + Seedr

Torrent Studio uses each user's own Seedr account for cloud torrent transfers and storage.

## Architecture

- **frontend/** — React + Vite.
- **backend/** — FastAPI.
- **Torrent search** — fast search/indexer endpoints.
- **Seedr** — cloud torrent storage/transfers.
- No developer Seedr storage is used for a normal connected user.

## Seedr account connection

The app uses Seedr's device authorization flow:

1. Create a Seedr account at [seedr.cc](https://www.seedr.cc/).
2. Return to Torrent Studio and choose **Connect Seedr account**.
3. Torrent Studio requests a Seedr device code.
4. Open the Seedr authorization page and enter the displayed code.
5. After approval, Torrent Studio receives the user's Seedr access token on the backend and associates it with that browser session.
6. Torrents, quota, files, downloads, and streaming use that connected Seedr account.

For users who already created a Seedr account with Google/Facebook, Seedr may require setting a password through its password-reset flow before device approval. The password is entered only on Seedr, never in Torrent Studio.

Torrent Studio never asks the user for their Seedr password.

Seedr's current settings page exposes **Add Device Code** under **Extensions, API & External Access**, and Seedr says its REST API uses OAuth2 for third-party integrations.

The exact device-code endpoints used by the implementation are the currently reachable Seedr device-code endpoints; the live code endpoint returns a device code, user code, verification URL, expiry, and polling interval.

## Dynamic movie catalogues

Marvel Movies and DC Live-Action each have separate persistent English and Hindi torrent caches. Select a franchise shortcut and choose **English audio** or **Hindi audio**; the caches filter releases by language/audio markers so language variants do not get mixed together. DC Animated retains its persistent shared torrent cache. These cached views do not require TMDB for the initial load.

At Docker image build time, bundled catalogue pages replace mapped `/api/poster` URLs with the saved direct image URLs in `backend/cache-seed/poster_map.json`. Catalogue cards also use direct poster URLs returned by metadata APIs. This avoids an extra app request for each mapped poster, which helps on Render's free tier. To add or refresh mappings, run `scripts/build_poster_map.py` against a running backend, then deploy a new image.

The separate **TMDB movie discovery** control lets visitors select Marvel, DC Live-Action or DC Animated and browse paginated TMDB movie metadata. For these three franchise views, OTT details come from the saved enriched Marvel/DC torrent catalogues; titles without a saved match are shown as unconfirmed rather than starting a live per-card availability scan. Other TMDB catalogues can continue checking availability in the background. Torrent-provider searches happen only when a movie's **Prepare** action is selected. The **Clear TMDB cache & reload** button deletes the server's in-memory and persistent TMDB catalogue, OTT availability and company-discovery caches, then requests the selected catalogue again. The clear-cache endpoint has a short global cooldown to protect the TMDB API quota.

Hollywood and Bollywood Popular, Trending and Latest use 18-title pages. Popular is sorted by TMDB popularity, Latest by release date, and Trending by TMDB's weekly trending order, filtered to the original language. The Oracle backend refreshes and saves the first three pages weekly; later pages are fetched from TMDB on demand and cached on the VPS. During refresh, each saved regional movie row is enriched with its IMDb lookup result and India streaming/rental/purchase availability. IMDb ratings and provider data remain best-effort: a title may have no IMDb rating or no reported provider listing. GitHub Actions checks the VPS snapshots daily, rebuilds the regional movie and TV snapshots on Oracle when they are missing, incomplete or at least seven days old, then commits updated snapshots for Render. A failed rebuild is not synced, and is retried on the next daily check. Trending snapshots are archived weekly and deduplicated by movie, retaining the most recent trending week and rank on each card; browsing farther back shows earlier unique chart entries. Since TMDB may return fewer than 18 eligible titles for a language in a given week, the archive grows as later snapshots add newly trending titles.

Bollywood Latest only includes movies for which the Oracle cache builder has confirmed at least one supported NexStream playback source. The weekly validation is capped at three TMDB candidate pages (up to 60 titles), so the section may contain fewer than three full 18-title pages. Refreshes publish only verified matches; if NexStream is unavailable before any title can be verified, the refresh fails rather than replacing a saved page with unverified entries. NexStream playback URLs are not copied into weekly movie snapshots. Direct Stream resolves a fresh URL when clicked, and the browser reuses it for at most four minutes.

For non-commercial use, request a free TMDB API credential in your TMDB account under **Settings → API**. Add either variable to the private `.env.debug` file on the VPS (never commit credentials):

```dotenv
TMDB_READ_ACCESS_TOKEN=<your TMDB API Read Access Token>
WATCHMODE_API_KEY=<your Watchmode API key>
NEXSTREAM_API_KEY=<your NexStream API key>
# Or use the v3 API key instead:
# TMDB_API_KEY=<your TMDB API key>
```

The VPS deployment workflow passes the private `$APP_DIR/.env` file to the Docker container. Keep TMDB credentials there, set `CACHE_ONLY_MODE=false` on the Oracle VPS, and redeploy after saving changes. The separate Render service remains cache-only and receives refreshed cache snapshots through the scheduled GitHub cache-sync workflow. TMDB requires its logo and attribution notice in the app; the movie-browser UI includes both. Its free developer access is for non-commercial use—commercial use needs a separate TMDB agreement.

The weekly snapshot builder runs against the Oracle VPS backend, so its container must have the TMDB and NexStream keys needed to build Bollywood Latest; Watchmode and OMDb keys enable their respective best-effort enrichments. Keys configured only in Render support Render's own live requests and do not supply credentials to the Oracle cache builder.

## OTT availability

Movie catalogue cards check India (`IN`) streaming availability in the background. Home distinguishes subscription/free/ad-supported streaming from rental and purchase options, displays the regional digital release date when TMDB has one, and offers a **Streaming now in India** filter. A missing provider listing is shown as unconfirmed rather than proof that a movie is unavailable.

The backend endpoint is `GET /api/movies/ott/{movie_id}?region=IN`. When `WATCHMODE_API_KEY` is configured, it first checks Watchmode legal streaming, rental and purchase sources for India using the TMDB movie ID. TMDB/JustWatch is retained as a fallback and supplies regional release-date metadata. Requests are coalesced and cached for 24 hours in `/app/data/tmdb_ott_availability_cache.json` (override with `TMDB_OTT_CACHE_FILE` or `TMDB_OTT_CACHE_SECONDS`). Keep `WATCHMODE_API_KEY` server-side only; never expose it in frontend environment variables or commit it. Provider attribution is returned by the API, while TMDB attribution remains required as described above.

## Automatic movie and TV-show availability search

`GET /api/search/availability?q=<title>` automatically searches both movies and TV series without requiring a media-type parameter. Watchmode is queried for film and TV title types, and TMDB independently searches both its movie and TV catalogues and requests the matching regional provider list for India. Matching provider entries from the primary sources are merged and de-duplicated.

TVmaze is queried only when Watchmode and TMDB return no matching title. If TVmaze has no matching series, OMDb is the final metadata fallback when `OMDB_API_KEY` or `OMDB_API_KEYS` is configured. TVmaze and OMDb can help identify a title, but neither fallback is treated as proof of streaming availability; the API returns an unconfirmed result when no primary provider listing is available. The fallback metadata response includes a source label so the UI can distinguish it from confirmed availability.

Keep OMDb credentials on the backend only. Multiple keys may be comma- or newline-separated in `OMDB_API_KEYS`; the legacy `OMDB_API_KEY` variable is also supported.

## Vercel

Set the project Root Directory to `frontend`.

Environment variable:

```
VITE_API_URL=https://YOUR-RENDER-SERVICE.onrender.com
```

## Render

Set the service Root Directory to `backend`.

For the normal per-user flow, no personal `SEEDR_API_TOKEN` or shared `SEEDR_LIBRARY_FOLDER_ID` is required. Set `SEEDR_SESSION_SECRET` to a stable random secret so encrypted personal Seedr sessions survive backend restarts. If it is omitted, personal connections reset when the backend process restarts.

Optional settings:

```
SEEDR_DEVICE_CLIENT_ID=seedr_xbmc
SEEDR_SESSION_TTL_SECONDS=2592000
SEEDR_SESSION_SECRET=<long-random-secret>
CORS_ORIGINS=https://YOUR-VERCEL-DOMAIN.vercel.app
```

`CORS_ORIGINS` (comma-separated) is required whenever the frontend is hosted on a different origin than the API. Because credentialed requests are allowed, the backend deliberately does **not** trust all `*.vercel.app` / `*.onrender.com` origins by default; only `localhost` / `127.0.0.1` are allowed unless you list your own origins or set `CORS_ORIGIN_REGEX`. All state-changing `/api/*` requests must also send the session CSRF token (`X-Torrent-Studio-CSRF`).

A legacy developer token can only be used when explicitly enabled with:

```
ALLOW_LEGACY_SEEDR_TOKEN=true
SEEDR_API_TOKEN=<developer Seedr token>
```

Do not enable the legacy mode for the normal multi-user deployment.

## Local development

Backend:

```bash
cd backend
python -m venv .venv
pip install -r requirements.txt
uvicorn main:app --reload
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Then set `VITE_API_URL=http://127.0.0.1:8000` for the frontend.

## Main Seedr API routes

- `GET /api/seedr/session`
- `POST /api/seedr/connect/start`
- `GET /api/seedr/connect/status`
- `POST /api/seedr/connect/disconnect`
- `POST /api/seedr/add`
- `GET /api/seedr/quota`
- `GET /api/seedr/library`
- `GET /api/seedr/files`
- `GET /api/seedr/files/:id/download`

The repository also contains compatibility endpoints used by the existing UI. They do not start a qBittorrent process or create shared local torrent storage.


## Watchmode streaming availability

Configure the backend environment variable `WATCHMODE_API_KEYS` with Watchmode API keys separated by commas or newlines. Keep the keys in your hosting provider's environment settings; do not commit real keys to GitHub. The legacy single-key variable `WATCHMODE_API_KEY` remains supported.

Example using placeholders only:

```env
WATCHMODE_API_KEYS=your_first_key,your_second_key,your_third_key
```

The backend tries another configured key after an authentication failure, server error, or transport failure. It does not switch keys on HTTP 429 rate-limit/quota responses or HTTP 403 access restrictions. Multiple keys should only be used in accordance with Watchmode's terms and each account's plan; this code does not treat keys as a way to bypass quotas. Watchmode source lookups send the key using the recommended `X-API-Key` request header.
