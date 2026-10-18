One-time trigger for the resumable OTT enrichment workflow. This file is intentionally used as a push-path trigger; edit or remove only when deliberately starting another enrichment run.
Latest one-time run: retry the three exact title/year aliases added in commit 179fc39cf12a8c36360fade2be17461ee9df03d6.
Latest one-time run: resolve Battle of the Super Sons using verified TMDB ID 886396 (2022).
Run after the cache maintenance change: remove cached 3D releases/unwanted titles, fix posters, sort by year, and refresh missing OTT provider data.
Retry after permissions fix: finish the cache cleanup, OTT refresh and snapshot sync.
Final retry: preserve HOME_DATA_DIR through sudo and complete all catalogue operations.
Run after Classics enrichment fix: populate missing OTT provider data and TMDB IDs for Classics, then sync the enriched cache snapshot.
