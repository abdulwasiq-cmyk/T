# Full Bollywood Library bootstrap

The Full Bollywood Library is a disk-backed catalogue. The web request only reads `/app/data/bollywood_torrent_matched_catalogue.json`; it does not launch bulk torrent searches.

## First-time setup on the running Home container

Run these commands after the image containing `refresh_bollywood_metadata.py` is deployed. Keep the existing `/app/data` volume mounted so generated data survives container replacement.

```sh
# 1. Build the Hindi/India-origin feature-film metadata snapshot using configured TMDB credentials.
docker exec home python /app/refresh_bollywood_metadata.py

# 2. Search the first bounded batch of movie titles and publish size-filtered matches.
docker exec home python /app/refresh_bollywood_torrent_catalogue.py

# 3. Confirm the cached library API responds with matched records.
curl -sS 'http://127.0.0.1:10000/api/movies/catalogue/bollywood-library?page=1&released_only=true&max_size_gb=5'
```

The metadata bootstrap uses TMDB discover filters for Hindi-language, India-origin feature films. The historical IMDb seed is optional; if it is not present in the image, the script starts with TMDB discovery instead. Metadata output defaults to `/app/data/bollywood_tmdb_updated_metadata.json`.

The torrent scanner checks a bounded batch (150 titles by default) per run and saves its cursor in `/app/data/bollywood_torrent_catalogue_state.json`. Run it repeatedly or schedule it daily until the cursor has covered the full list. Only known-size releases up to 5 GiB are cached; unknown sizes are excluded. Do not run a full scan in the normal page-request path.

## Scheduled refresh

After verifying the first run, schedule metadata refresh weekly and the torrent matcher daily with host cron. Redirect logs to a persistent host directory and monitor provider failures. Never print TMDB tokens in shell output or logs.
