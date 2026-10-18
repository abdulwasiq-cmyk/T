# Bollywood torrent-matched catalogue

The Full Bollywood Library is a separate, disk-backed view. It reads
`/app/data/bollywood_torrent_matched_catalogue.json`; normal catalogue requests
never launch bulk provider searches. The API filters the persisted matches by the
selected 1–5 GiB maximum and returns every valid cached release for each title.

## Incremental refresh

Run inside the deployed backend container:

```sh
python /app/refresh_bollywood_torrent_catalogue.py
```

Each run checks a bounded batch (150 titles by default), then atomically writes the
updated cache and cursor state. Existing successful matches are retained if a
refresh returns no qualifying releases, preventing transient provider misses from
removing titles from the library. The next run resumes from the saved cursor and
rechecks stale matched titles.

To tune a run without changing the code:

```sh
BOLLYWOOD_TORRENT_REFRESH_BATCH_SIZE=300 python /app/refresh_bollywood_torrent_catalogue.py
```

The metadata input defaults to
`/app/data/bollywood_tmdb_updated_metadata.json`. If that file is missing on first
run, the scanner looks for a bundled
`backend/cache-seed/bollywood_tmdb_updated_metadata.json`. Keep the metadata file,
match cache, and cursor state on the persistent `APP_DATA_DIR` volume so scans
resume after a container replacement.

The scanner uses the backend's existing torrent search provider implementation.
It stores only known-size releases up to 5 GiB and applies title/year matching and
adult-result exclusions. The API independently enforces the selected size limit.
