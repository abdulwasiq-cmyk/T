import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import {
  Search,
  X,
  Loader2,
  Download,
  Play,
  Copy,
  Check,
  ExternalLink,
  Users,
  Database,
  AlertCircle,
  SlidersHorizontal,
  Film,
  Maximize2
} from 'lucide-react';
import { api, API_BASE, ImdbRatingResult, MarvelCatalogueResponse, MovieCatalogueItem, MovieCatalogueKey, MovieCataloguePage, MovieOttAvailability, MovieOttProvider, SearchMediaMetadata, TorrentSearchResult, TvCataloguePage } from '../api/client.ts';
import { formatBytes } from '../utils/formatters.ts';
import { MovieCompactCard, MoviePrepareCard, imdbTitleLink, setHighQualityPostersReady, type MovieCardProvider } from './MovieCards.tsx';
import searchLimits from '../../search_limits.json';

const CROW_2024_POSTER_URL = 'https://m.media-amazon.com/images/M/MV5BNjYyZjJjMzQtNmNlMS00NmE4LTljZTEtZDI5ZjIxYjE1MzI2XkEyXkFqcGc%40._V1_.jpg';

function isWonderWoman1984(movie: { title?: string; year?: number | string | null }): boolean {
  return /^wonder woman 1984$/i.test(String(movie.title || '').trim()) &&
    Number(movie.year || 0) === 2020;
}

function isTheCrow2024(movie: { title?: string; year?: number | string | null }): boolean {
  return /^the crow$/i.test(String(movie.title || '').trim()) &&
    Number(movie.year || 0) === 2024;
}

function imdbFallbackForCatalogueMovie(movie: { title?: string; year?: number | string | null }): ImdbRatingResult | undefined {
  if (isWonderWoman1984(movie)) {
    return {
      title: 'Wonder Woman 1984',
      year: 2020,
      mediaType: 'movie',
      status: 'found',
      imdbRating: 5.3,
      imdbId: 'tt7126948',
      imdbUrl: 'https://www.imdb.com/title/tt7126948/',
    };
  }
  if (isTheCrow2024(movie)) {
    return {
      title: 'The Crow',
      year: 2024,
      mediaType: 'movie',
      status: 'found',
      imdbRating: 4.7,
      imdbId: 'tt1340094',
      imdbUrl: 'https://www.imdb.com/title/tt1340094/',
    };
  }
  return undefined;
}

function imdbRatingForCatalogueMovie(movie: MovieCatalogueItem): ImdbRatingResult | undefined {
  if (movie.imdbRatingStatus !== 'found' && movie.imdbRatingStatus !== 'not_found') {
    return undefined;
  }
  return {
    title: movie.title,
    year: movie.year,
    mediaType: 'movie',
    status: movie.imdbRatingStatus,
    imdbRating: movie.imdbRating,
    imdbVotes: movie.imdbVotes,
    imdbId: movie.imdbId,
    imdbUrl: movie.imdbUrl,
  };
}

function cataloguePosterOverride(movie: { title?: string; year?: number | string | null; posterUrl?: string | null }): string {
  return isTheCrow2024(movie) ? CROW_2024_POSTER_URL : String(movie.posterUrl || '').trim();
}

function cataloguePosterSource(movie: { title?: string; year?: number | string | null; posterUrl?: string | null }): string {
  const known = cataloguePosterOverride(movie);
  if (known) return known;
  const directPosterUrl = String(movie.posterUrl || '').trim();
  if (/^https?:\/\//i.test(directPosterUrl)) return directPosterUrl;
  const params = new URLSearchParams({ title: String(movie.title || '').trim() });
  if (movie.year) params.set('year', String(movie.year));
  params.set('size', 'w185');
  return '/api/poster?' + params.toString();
}

type MovieOttCardState = MovieOttAvailability | { status: 'loading' | 'error'; message?: string };

type NexStreamEpisode = { episodeNumber: number; name: string; overview: string; airDate: string; still: string | null };
type NexStreamSeason = { seasonNumber: number; name: string; episodeCount: number };
type NexStreamEpisodePayload = { seasons: NexStreamSeason[]; episodes: NexStreamEpisode[]; selectedSeason: number };

// Episode metadata is prefetched after TV search results appear. Keep it in a
// module cache so opening the picker and revisiting a title is usually instant.
const nexStreamEpisodeCache = new Map<string, NexStreamEpisodePayload>();
const nexStreamEpisodeRequests = new Map<string, Promise<NexStreamEpisodePayload>>();
const NEXSTREAM_EPISODE_CACHE_STORAGE_KEY = 'home_nexstream_episode_metadata_v1';
const nexStreamEpisodeCacheKey = (id: number, season: number) => String(id) + ':' + String(season);

function readPersistentNexStreamEpisodeCache(key: string): NexStreamEpisodePayload | null {
  try {
    const parsed = JSON.parse(localStorage.getItem(NEXSTREAM_EPISODE_CACHE_STORAGE_KEY) || '{}');
    const payload = parsed && typeof parsed === 'object' ? parsed[key] : null;
    if (
      payload &&
      Array.isArray(payload.seasons) &&
      Array.isArray(payload.episodes) &&
      Number.isFinite(Number(payload.selectedSeason))
    ) {
      return payload as NexStreamEpisodePayload;
    }
  } catch {}
  return null;
}

function writePersistentNexStreamEpisodeCache(key: string, payload: NexStreamEpisodePayload): void {
  try {
    const parsed = JSON.parse(localStorage.getItem(NEXSTREAM_EPISODE_CACHE_STORAGE_KEY) || '{}');
    const cache = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
    cache[key] = payload;
    localStorage.setItem(NEXSTREAM_EPISODE_CACHE_STORAGE_KEY, JSON.stringify(cache));
  } catch {
    // Storage can be unavailable or full. In-memory caching still works.
  }
}
// v2: v1 also stored year-less keys, which let a poster for one year (e.g. The Crow 1994)
// be reused for a different year of the same title (The Crow 2024).
const POSTER_TITLE_CACHE_STORAGE_KEY = 'home_poster_title_cache_v2';
const POSTER_TITLE_CACHE_TTL_MS = 30 * 24 * 60 * 60 * 1000;
type CachedTitlePoster = { url: string; expiresAt: number };
const posterByTitleCache = new Map<string, CachedTitlePoster>();

function normalizePosterTitle(value: unknown): string {
  return String(value || '')
    .toLowerCase()
    .replace(/\\.[a-z0-9]{2,5}$/i, '')
    .replace(/[._]+/g, ' ')
    .replace(/[^a-z0-9]+/g, ' ')
    .replace(/\\s+/g, ' ')
    .trim();
}

function posterTitleCacheKeys(result: TorrentSearchResult): string[] {
  const title = normalizePosterTitle(result.mediaTitle || result.title);
  if (!title) return [];
  const rawYear = String(result.year || (String(result.title || '').match(/\\b((?:19|20)\\d{2})\\b/) || [])[1] || '').trim();
  const year = /^\\d{4}$/.test(rawYear) ? rawYear : '';
  // With a known year only the exact title+year key is valid; never fall back to another year.
  return year ? [title + '|' + year] : [title + '|'];
}

function readPosterTitleCache(): void {
  try {
    const raw = localStorage.getItem(POSTER_TITLE_CACHE_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    if (!parsed || typeof parsed !== 'object') return;
    const now = Date.now();
    for (const [key, value] of Object.entries(parsed as Record<string, CachedTitlePoster>)) {
      if (value && typeof value.url === 'string' && value.url && Number(value.expiresAt) > now) {
        posterByTitleCache.set(key, { url: value.url, expiresAt: Number(value.expiresAt) });
      }
    }
  } catch {}
}

function savePosterTitleCache(key: string, url: string): void {
  if (!key || !url) return;
  posterByTitleCache.set(key, { url, expiresAt: Date.now() + POSTER_TITLE_CACHE_TTL_MS });
  try {
    const now = Date.now();
    const entries = Array.from(posterByTitleCache.entries())
      .filter(([, value]) => value.expiresAt > now)
      .slice(-500);
    localStorage.setItem(POSTER_TITLE_CACHE_STORAGE_KEY, JSON.stringify(Object.fromEntries(entries)));
  } catch {}
}

function cachedPosterForTitle(result: TorrentSearchResult): string {
  const now = Date.now();
  for (const key of posterTitleCacheKeys(result)) {
    const cached = posterByTitleCache.get(key);
    if (cached && cached.expiresAt > now) return cached.url;
    if (cached) posterByTitleCache.delete(key);
  }
  return '';
}


async function fetchNexStreamEpisodeMetadata(tmdbId: number, season: number): Promise<NexStreamEpisodePayload> {
  const key = nexStreamEpisodeCacheKey(tmdbId, season);
  const cached = nexStreamEpisodeCache.get(key);
  if (cached) return cached;
  const persistentCached = readPersistentNexStreamEpisodeCache(key);
  if (persistentCached) {
    nexStreamEpisodeCache.set(key, persistentCached);
    return persistentCached;
  }
  const pending = nexStreamEpisodeRequests.get(key);
  if (pending) return pending;

  const request = (async () => {
    let lastError: unknown = null;
    // Transient network errors / TMDB 429s happen occasionally. Retry in the
    // background and on demand instead of leaving the picker permanently empty.
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        const params = new URLSearchParams({ tmdb_id: String(tmdbId), season: String(season) });
        const response = await fetch('/api/nexstream/episodes?' + params.toString(), {
          headers: { Accept: 'application/json' }, credentials: 'same-origin',
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.success) {
          const error = new Error(data.detail || 'Could not load episode list.');
          // Retry server errors and rate limits; don't retry invalid requests.
          if (response.status < 500 && response.status !== 429) throw Object.assign(error, { permanent: true });
          throw error;
        }
        const payload: NexStreamEpisodePayload = {
          seasons: Array.isArray(data.seasons) ? data.seasons : [],
          episodes: Array.isArray(data.episodes) ? data.episodes : [],
          selectedSeason: Number(data.selectedSeason) || season,
        };
        nexStreamEpisodeCache.set(nexStreamEpisodeCacheKey(tmdbId, payload.selectedSeason), payload);
        nexStreamEpisodeCache.set(key, payload);
        writePersistentNexStreamEpisodeCache(key, payload);
        writePersistentNexStreamEpisodeCache(nexStreamEpisodeCacheKey(tmdbId, payload.selectedSeason), payload);
        return payload;
      } catch (error: any) {
        lastError = error;
        if (error?.permanent || attempt === 2) break;
        await new Promise(resolve => window.setTimeout(resolve, 400 * (attempt + 1)));
      }
    }
    throw lastError instanceof Error ? lastError : new Error('Could not load episode list after retries.');
  })();
  nexStreamEpisodeRequests.set(key, request);
  try {
    return await request;
  } finally {
    nexStreamEpisodeRequests.delete(key);
  }
}

function prefetchNexStreamEpisodeMetadata(
  items: NexStreamTitleLike[],
  onStatus: (id: number, status: 'loading' | 'ready' | 'error') => void,
) {
  const queue = items.slice();
  let active = 0;
  const pump = () => {
    while (active < 3 && queue.length) {
      const item = queue.shift();
      if (!item) continue;
      const key = nexStreamEpisodeCacheKey(item.id, 1);
      if (nexStreamEpisodeCache.has(key)) {
        onStatus(item.id, 'ready');
        continue;
      }
      if (nexStreamEpisodeRequests.has(key)) {
        onStatus(item.id, 'loading');
        void fetchNexStreamEpisodeMetadata(item.id, 1).then(
          () => onStatus(item.id, 'ready'),
          () => onStatus(item.id, 'error'),
        );
        continue;
      }
      onStatus(item.id, 'loading');
      active += 1;
      void fetchNexStreamEpisodeMetadata(item.id, 1).then(
        () => onStatus(item.id, 'ready'),
        () => onStatus(item.id, 'error'),
      ).finally(() => {
        active -= 1;
        pump();
      });
    }
  };
  pump();
}

type NexStreamTitleLike = { id: number; type: 'movie' | 'tv' };

function normalizeTorrentTitleForNexStream(value: string): string {
  return normalizeGroupTitle(String(value || '')
    .replace(/\\b(?:19|20)\\d{2}\\b/g, ' ')
    .replace(/\\b(?:s\\d{1,2}(?:e\\d{1,2})?|season\\s*\\d+|complete\\s*(?:series|season)?|all\\s*episodes|episodes?)\\b/gi, ' ')
    .replace(/\\b(?:2160p|1080p|720p|480p|4k|uhd|hdr|bluray|blu[ -]?ray|brrip|bdrip|web[ -]?dl|webrip|hdtv|x264|x265|hevc|aac|proper|repack|extended|remastered)\\b/gi, ' ')
    .replace(/\\[[^\\]]*\\]|\\([^)]*\\)/g, ' '));
}

function isPresetSearchLabel(value: string): boolean {
  const normalized = String(value || '').trim().toLowerCase().replace(/\s+/g, ' ');
  return new Set([
    'bollywood movies',
    'marvel movies (english)',
    'marvel movies (hindi)',
    'dc live-action movies (english)',
    'dc live-action movies (hindi)',
    'dc animated movies',
  ]).has(normalized);
}

function providerNames(providers: MovieOttProvider[]): React.ReactNode {
  const shown = providers.slice(0, 3);
  return (
    <>
      {shown.map((provider, index) => (
        <React.Fragment key={String(provider.providerId ?? provider.name) + '-' + index}>
          {index > 0 ? ', ' : ''}
          {provider.webUrl ? (
            <a href={provider.webUrl} target="_blank" rel="noreferrer" className="text-cyan-300 underline underline-offset-2 hover:text-cyan-200">
              {provider.name}
            </a>
          ) : provider.name}
        </React.Fragment>
      ))}
      {providers.length > 3 ? ' +' + (providers.length - 3) : ''}
    </>
  );
}


type SeedrSearchFile = {
  id: string;
  streamId?: string;
  name: string;
  size: number;
  folderId: string;
  folderPath: string;
};

interface TorrentSearchPanelProps {
  onPrepare: (
    result: TorrentSearchResult,
    metadata?: {
      name: string;
      hash: string;
      files: { index: number; name: string; size: number; path: string; type: string; priority?: number }[];
      totalSize: number;
    }
  ) => Promise<{ files: SeedrSearchFile[]; deletedFolderIds?: string[] }>;
  onCancelPrepare?: () => void | Promise<void>;
  onOpenProgress?: () => void;
  seedrFiles?: SeedrSearchFile[];
  seedrDeletedFolderIds?: string[];
  onPlaySeedrFile?: (file: SeedrSearchFile) => void | Promise<void>;
}

function formatPublished(value?: string) {
  if (!value) return 'Unknown date';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Unknown date';
  return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

interface TorrentQualityGroup {
  key: string;
  title: string;
  year: string;
  variants: TorrentSearchResult[];
  posterResult: TorrentSearchResult;
}

function torrentGroupReleaseYear(group: TorrentQualityGroup): number {
  // The year shown on the card is the most trustworthy user-facing sort key.
  const displayedYear = group.title.match(/\(((?:19|20)\d{2})\)\s*$/)?.[1];
  if (displayedYear) return Number(displayedYear);

  const groupYear = Number.parseInt(String(group.year || ''), 10);
  if (groupYear > 0) return groupYear;

  for (const variant of group.variants) {
    const year = Number.parseInt(String(variant.year || ''), 10);
    if (year > 0) return year;
  }
  return 0;
}

function releaseDateSortValue(item: { releaseDate?: string | null; year?: number | string | null }): number {
  const rawDate = String(item.releaseDate || '').trim();
  // TMDB release dates are ISO YYYY-MM-DD. Use the full date so later months
  // sort ahead of earlier months within the same year.
  if (/^\d{4}-\d{2}-\d{2}$/.test(rawDate)) {
    const timestamp = Date.parse(rawDate + 'T00:00:00Z');
    if (Number.isFinite(timestamp)) return timestamp;
  }
  const year = Number(item.year) || Number(rawDate.slice(0, 4)) || 0;
  return year ? Date.UTC(year, 0, 1) : 0;
}

function compareReleaseDateDescending(
  a: { title?: string; releaseDate?: string | null; year?: number | string | null },
  b: { title?: string; releaseDate?: string | null; year?: number | string | null },
): number {
  const dateA = releaseDateSortValue(a);
  const dateB = releaseDateSortValue(b);
  if (!dateA && !dateB) return String(a.title || '').localeCompare(String(b.title || ''), undefined, { numeric: true });
  if (!dateA) return 1;
  if (!dateB) return -1;
  if (dateA !== dateB) return dateB - dateA;
  return String(a.title || '').localeCompare(String(b.title || ''), undefined, { numeric: true });
}

function torrentResultKey(result: TorrentSearchResult): string {
  return String(result.guid || result.infoHash || result.magnetUrl || result.downloadUrl || result.title);
}

function normalizeGroupTitle(value: string): string {
  return String(value || '')
    .toLowerCase()
    .replace(/&/g, ' and ')
    .replace(/[’']/g, '')
    .replace(/[._]+/g, ' ')
    .replace(/[^a-z0-9]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^(?:the|a|an)\s+/, '');
}

function normalizeSavedOttTitle(value: string): string {
  // Saved franchise catalogues often store the canonical title without the
  // release year, while torrent cards include it (for example "(2019)").
  const withoutTrailingYear = String(value || '')
    .replace(/\s*\(?((?:19|20)\d{2})\)?\s*$/, '')
    .trim();
  return normalizeGroupTitle(withoutTrailingYear);
}

function episodeSeriesTitle(value: string): string | null {
  // Episode torrents usually contain the show name followed by SxxExx or 1x01.
  const raw = String(value || '').trim();
  const match = raw.match(/^(.*?)\s+(?:S\d{1,2}E\d{1,3}|\d{1,2}x\d{1,3})(?=$|[\s._-])/i);
  const title = String(match?.[1] || '').replace(/[._]+/g, ' ').trim();
  return title.length >= 2 ? title : null;
}

function imdbRatingKey(
  title: string,
  year?: number | string | null,
  mediaType: 'movie' | 'tv' = 'movie',
): string {
  const raw = String(title || '').trim();
  const embeddedYear = raw.match(/\s*\(?((?:19|20)\d{2})\)?\s*$/)?.[1] || '';
  const resolvedYear = String(year || embeddedYear || '');
  const normalizedTitle = normalizeSavedOttTitle(raw).replace(/\bfour\b/g, '4');
  return (mediaType === 'tv' ? 'tv|' : 'movie|') + normalizedTitle + '|' + resolvedYear;
}

function imdbRatingQueryTitle(title: string, year?: number | string | null): string {
  const raw = String(title || '').trim();
  const embeddedYear = raw.match(/\s*\(?((?:19|20)\d{2})\)?\s*$/)?.[1] || '';
  const resolvedYear = String(year || embeddedYear || '');
  const cleanTitle = embeddedYear ? raw.replace(/\s*\(?((?:19|20)\d{2})\)?\s*$/, '').trim() : raw;
  return cleanTitle + (resolvedYear ? ' (' + resolvedYear + ')' : '');
}

function searchMetadataForGroup(
  group: TorrentQualityGroup,
  items: SearchMediaMetadata[],
): SearchMediaMetadata | undefined {
  const key = normalizeSavedOttTitle(group.title);
  let candidates = items.filter(item => normalizeSavedOttTitle(item.title) === key);
  if (!candidates.length) {
    // TV torrent card names include episode titles (e.g. "The Rookie S06E10 Escape Plan").
    // Match the series title separately so show synopsis and TMDB rating appear on the card.
    const seriesTitle = episodeSeriesTitle(group.title);
    if (seriesTitle) {
      const seriesKey = normalizeSavedOttTitle(seriesTitle);
      candidates = items.filter(item =>
        item.mediaType === 'tv' && normalizeSavedOttTitle(item.title) === seriesKey
      );
    }
  }
  if (!candidates.length) return undefined;

  const year = String(group.year || '');
  if (year) {
    const sameYear = candidates.find(item => String(item.year || '') === year);
    if (sameYear) return sameYear;
    const noYear = candidates.filter(item => !item.year);
    // Unknown-year records can be used when uniquely identifiable, but never
    // substitute a different known release year (remakes often share titles).
    return noYear.length === 1 ? noYear[0] : undefined;
  }
  const noYear = candidates.filter(item => !item.year);
  if (noYear.length === 1) return noYear[0];
  return candidates.length === 1 ? candidates[0] : undefined;
}

function mergeSearchMetadataItems(
  current: SearchMediaMetadata[],
  incoming: SearchMediaMetadata[],
): SearchMediaMetadata[] {
  const merged = [...current];
  for (const candidate of incoming) {
    const candidateTitle = normalizeSavedOttTitle(candidate.title);
    const candidateYear = String(candidate.year || '');
    const candidateType = candidate.mediaType || 'movie';
    const index = merged.findIndex(item =>
      normalizeSavedOttTitle(item.title) === candidateTitle &&
      String(item.year || '') === candidateYear &&
      (item.mediaType || 'movie') === candidateType
    );
    if (index < 0) {
      merged.push(candidate);
      continue;
    }

    const previous = merged[index];
    merged[index] = {
      ...previous,
      ...candidate,
      overview: String(candidate.overview || '').trim() || previous.overview,
      posterUrl: candidate.posterUrl || previous.posterUrl,
      backdropUrl: candidate.backdropUrl || previous.backdropUrl,
      tmdbUrl: candidate.tmdbUrl || previous.tmdbUrl,
      rating: candidate.rating || previous.rating,
      tmdbId: candidate.tmdbId || previous.tmdbId || previous.id,
    };
  }
  return merged;
}

const PREFERRED_OTT_PROVIDERS = [
  'amazon prime video',
  'netflix',
  'sonyliv',
  'hotstar',
  'apple tv',
  'zee5',
];

function normalizeOttProviderName(name: string): string {
  const normalized = String(name || '')
    .toLowerCase()
    .replace(/\\s*[-·:]\\s*(?:rent|buy|subscription|sub|streaming)\\s*$/i, '')
    .replace(/\\s+with\\s+ads?$/i, '')
    .replace(/\\s+(?:channel|add-on)$/i, '')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim()
    .replace(/\\s+/g, ' ');

  if (/^(?:primevideo|prime video|amazon video|amazon primevideo|amazon prime video)$/.test(normalized)) {
    return 'amazon prime video';
  }
  if (/^(?:appletv|apple tv|apple tv store|apple tv plus|apple tv channel)$/.test(normalized)) {
    return 'apple tv';
  }
  if (/^(?:jiohotstar|jio hotstar|hotstar)$/.test(normalized)) {
    return 'hotstar';
  }
  if (/^sonyliv$/.test(normalized)) return 'sonyliv';
  if (/^zee ?5$/.test(normalized)) return 'zee5';
  // TMDB can list the same service as both "MX Player" and "Amazon MX Player".
  if (/^(?:amazon )?mx player$/.test(normalized)) return 'mx player';
  if (normalized.startsWith('sony pictures ') && normalized.includes('amazon channel')) {
    return 'sony pictures amazon channel';
  }
  if (normalized.includes('lionsgate play')) return 'lionsgate play';
  return normalized;
}

function displayOttProviderName(name: string): string {
  const key = normalizeOttProviderName(name);
  const preferredLabels: Record<string, string> = {
    'amazon prime video': 'Amazon Prime',
    'mx player': 'MX Player',
    netflix: 'Netflix',
    sonyliv: 'SonyLIV',
    hotstar: 'Hotstar',
    'apple tv': 'Apple TV',
    zee5: 'ZEE5',
  };
  if (preferredLabels[key]) return preferredLabels[key];
  return String(name || '')
    .replace(/\\s*[-·:]\\s*(?:rent|buy|subscription|sub|streaming)\\s*$/i, '')
    .replace(/\\s+with\\s+ads?$/i, '')
    .trim();
}

function uniqueOttProviders<T extends { name: string; type?: string; webUrl?: string; providerId?: number | string }>(providers: T[]): T[] {
  const unique = new Map<string, T>();
  providers.forEach(provider => {
    const key = normalizeOttProviderName(provider.name);
    if (!key) return;
    const existing = unique.get(key);
    if (existing) {
      unique.set(key, {
        ...existing,
        webUrl: existing.webUrl || provider.webUrl,
        providerId: existing.providerId ?? provider.providerId,
      });
      return;
    }
    unique.set(key, { ...provider, name: displayOttProviderName(provider.name) });
  });

  const all = Array.from(unique.entries());
  const preferred = all.filter(([key]) => PREFERRED_OTT_PROVIDERS.includes(key));
  return (preferred.length ? preferred : all).map(([, provider]) => provider);
}

/** Maps provider objects to the shape the shared movie cards render. */
function toCardProviders(
  providers: Array<{ name: string; webUrl?: string; providerId?: number | string }>,
): MovieCardProvider[] {
  return providers.map((provider, index) => {
    const name = displayOttProviderName(provider.name);
    return {
      key: String(provider.providerId ?? name ?? index).toLowerCase(),
      name,
      webUrl: provider.webUrl,
    };
  });
}

const PLAYABLE_FILE_PATTERN = /\.(mkv|mp4|m4v|webm|mov|avi|m3u8|ts|mp3|wav|flac|aac|ogg|m4a)$/i;

function normalizeSavedOttProviders(
  items: unknown,
  fallbackType = 'streaming',
  fallbackRegion = 'IN',
): Array<MovieOttProvider & { region: string; type: string }> {
  if (!Array.isArray(items)) return [];
  return items.flatMap((raw: any) => {
    const item = typeof raw === 'string'
      ? { name: raw }
      : raw && typeof raw === 'object'
        ? raw
        : null;
    const name = String(
      item?.name || item?.provider_name || item?.providerName || item?.provider || item?.title || ''
    ).trim();
    if (!name) return [];
    return [{
      name,
      region: String(item?.region || fallbackRegion || 'IN'),
      type: String(item?.type || fallbackType || 'streaming'),
      webUrl: item?.webUrl || item?.web_url || item?.url || undefined,
      logoUrl: item?.logoUrl || item?.logo_url || item?.logo_path || item?.logo || item?.image_url || undefined,
    }];
  });
}

function savedOttAvailabilityForMovie(movie: MovieCatalogueItem): MovieOttAvailability | undefined {
  const saved = movie.ottAvailability;
  if (!saved || typeof saved !== 'object') return undefined;
  const region = String(saved.region || 'IN');
  const streamingProviders = normalizeSavedOttProviders(saved.streamingProviders, 'streaming', region);
  const rentProviders = normalizeSavedOttProviders(saved.rentProviders, 'rent', region);
  const buyProviders = normalizeSavedOttProviders(saved.buyProviders, 'buy', region);
  const savedStatus = String(saved.status || '').toLowerCase();
  return {
    movieId: Number(movie.tmdbId || movie.id),
    region,
    status: (
      savedStatus === 'streaming' ||
      savedStatus === 'streaming_info_found' ||
      streamingProviders.length > 0
    ) ? 'streaming'
      : (savedStatus === 'rent_buy' || rentProviders.length > 0 || buyProviders.length > 0) ? 'rent_buy'
      : savedStatus === 'digital_scheduled' ? 'digital_scheduled'
      : savedStatus === 'digital_release_known' ? 'digital_release_known'
      : 'not_confirmed',
    streamingProviders,
    rentProviders,
    buyProviders,
    providerLink: String(saved.providerLink || ''),
    checkedAt: String(saved.checkedAt || ''),
    source: String(saved.source || 'saved-catalogue'),
    providerAttribution: String(saved.providerAttribution || ''),
    attribution: String(saved.attribution || ''),
  };
}

function torrentMediaIdentity(result: TorrentSearchResult): { title: string; normalized: string; year: string } {
  const source = String(result.mediaTitle || result.title || '').trim();
  const embeddedYears: Array<{ year: string; index: number }> = [];
  const yearPattern = /\b((?:19|20)\d{2})\b/g;
  let yearMatch: RegExpExecArray | null;
  while ((yearMatch = yearPattern.exec(source)) !== null) {
    embeddedYears.push({ year: yearMatch[1], index: yearMatch.index });
  }
  const metadataYear = String(result.year || '').trim();
  // Titles such as "Wonder Woman 1984" can contain a year-like token that is
  // not the film's release year. Only remove a token matching release metadata.
  const embeddedYear = metadataYear
    ? [...embeddedYears].reverse().find(match => match.year === metadataYear) || null
    : embeddedYears[embeddedYears.length - 1] || null;
  let year = metadataYear || embeddedYear?.year || '';
  let title = source.replace(/[._]+/g, ' ');

  if (embeddedYear) {
    title = title.slice(0, embeddedYear.index);
  } else {
    // Release tags in brackets are often incomplete on public indexers.
    title = title
      .replace(/\[[^\]]*(?:\]|$)/g, ' ')
      .replace(/\([^)]*(?:\)|$)/g, ' ');
    title = title.split(
      /\b(?:2160p|1440p|1080p|720p|576p|480p|4k|8k|web[- ]?dl|web[- ]?rip|webrip|bluray|blu[- ]?ray|brrip|hdrip|dvdrip|dvdscr|telesync|telecine|r5|r6|cam|hdcam|x264|x265|h264|h265|hevc|xvid|divx|dts|aac|ac3|ddp|eng|english|nlsub|dual[ .-]?audio|hindi|tamil|telugu|malayalam|kannada|proper|repack|remux|hdr|bluelady|jaybob|haggis|voltage|dtrg|document|vision|sonido|saimorny|ltt)\b/i
    )[0];
  }

  title = title.replace(/\s+/g, ' ').trim().replace(/[\s._:[\](){}-]+$/g, '').trim();
  let normalized = normalizeGroupTitle(title);

  // Common LimeTorrents release shorthand: keep the sequel distinct from
  // The Avengers (2012), while presenting it with its catalogue title.
  if ((normalized === 'avengers 2' || normalized === 'the avengers 2') && (!year || year === '2015')) {
    title = 'Avengers: Age of Ultron';
    normalized = normalizeGroupTitle(title);
    year = year || '2015';
  } else if (
    (normalized === 'marvel s the avengers' || normalized === 'marvels the avengers') &&
    (!year || year === '2012')
  ) {
    title = 'The Avengers';
    normalized = normalizeGroupTitle(title);
    year = year || '2012';
  } else if (normalized === 'avengers age of ultron' && (!year || year === '2015')) {
    title = 'Avengers: Age of Ultron';
    year = year || '2015';
  } else if (normalized === 'the avengers' && year === '2012') {
    title = 'The Avengers';
  }

  if (!title) title = String(result.title || result.mediaTitle || 'Unknown title').trim();
  return { title, normalized: normalizeGroupTitle(title), year };
}

function torrentQualityDetails(result: TorrentSearchResult): { key: string; label: string; rank: number } {
  const raw = `${String(result.quality || '')} ${String(result.title || '')}`;
  const resolution = raw.match(/\b(2160p|1440p|1080p|720p|576p|480p|4k|8k)\b/i)?.[1]?.toLowerCase() || '';
  const resolutionLabel = resolution === '4k' ? '2160p (4K)' : resolution.toUpperCase();
  const formats: Array<[RegExp, string, number]> = [
    [/\b3d\b.*\b(?:bluray|blu[ .-]?ray)\b|\b(?:bluray|blu[ .-]?ray)\b.*\b3d\b/i, '3D BluRay', 950],
    [/\b(?:blu[ .-]?ray|bluray)\b/i, 'BluRay', 900],
    [/\b(?:web[- .]?dl)\b/i, 'WEB-DL', 850],
    [/\b(?:web[- .]?rip|webrip)\b/i, 'WEBRip', 800],
    [/\b(?:brrip)\b/i, 'BRRip', 700],
    [/\b(?:hdtv)\b/i, 'HDTV', 650],
    [/\b(?:hdrip)\b/i, 'HDRip', 600],
    [/\b(?:dvdscr|dvd[ .-]?scr)\b/i, 'DVDScr', 500],
    [/\b(?:dvdrip|dvd)\b/i, 'DVDRip', 450],
    [/\b(?:telesync|ts)\b/i, 'Telesync', 400],
    [/\b(?:telecine|tc)\b/i, 'Telecine', 350],
    [/\b(?:r5|r6)\b/i, 'R5/R6', 300],
    [/\b(?:hdcam|cam)\b/i, 'CAM', 200],
  ];
  const format = formats.find(([pattern]) => pattern.test(raw));
  const label = [resolutionLabel, format?.[1]].filter(Boolean).join(' ') || (resolutionLabel || 'Other release');
  const resolutionRank = resolution === '2160p' || resolution === '4k' ? 2160
    : resolution === '1440p' ? 1440
    : resolution === '1080p' ? 1080
    : resolution === '720p' ? 720
    : resolution === '576p' ? 576
    : resolution === '480p' ? 480
    : resolution === '8k' ? 4320
    : 0;
  // Resolution dominates source format so 1080p is never ranked below 720p
  // just because the latter carries a BluRay tag.
  const rank = resolutionRank * 1000 + (format?.[2] || 0);
  return { key: label.toLowerCase().replace(/[^a-z0-9]+/g, '-'), label, rank };
}

function groupTorrentResults(results: TorrentSearchResult[]): TorrentQualityGroup[] {
  const titleBuckets = new Map<string, Array<{ result: TorrentSearchResult; identity: ReturnType<typeof torrentMediaIdentity> }>>();
  for (const result of results) {
    const identity = torrentMediaIdentity(result);
    if (!identity.normalized) continue;
    const bucket = titleBuckets.get(identity.normalized) || [];
    bucket.push({ result, identity });
    titleBuckets.set(identity.normalized, bucket);
  }

  const groups: TorrentQualityGroup[] = [];
  for (const [normalized, entries] of titleBuckets) {
    const knownYears = Array.from(new Set(entries.map(entry => entry.identity.year).filter(Boolean)));
    const yearBuckets = new Map<string, typeof entries>();
    for (const entry of entries) {
      // Unknown-year releases can join a title only if this result set has one
      // unambiguous year for that title; don't merge remakes with one another.
      const year = entry.identity.year || (knownYears.length === 1 ? knownYears[0] : '');
      const bucket = yearBuckets.get(year) || [];
      bucket.push(entry);
      yearBuckets.set(year, bucket);
    }

    for (const [year, yearEntries] of yearBuckets) {
      // Keep distinct torrent releases even when they share the same resolution
      // and source label. This lets users choose another encode, size, or swarm,
      // rather than hiding those options and making the quality selector disappear.
      const uniqueVariants = new Map<string, { result: TorrentSearchResult; quality: ReturnType<typeof torrentQualityDetails> }>();
      for (const entry of yearEntries) {
        const key = torrentResultKey(entry.result);
        if (!uniqueVariants.has(key)) {
          uniqueVariants.set(key, { result: entry.result, quality: torrentQualityDetails(entry.result) });
        }
      }

      const variants = Array.from(uniqueVariants.values())
        .sort((a, b) => {
          const qualityDifference = b.quality.rank - a.quality.rank;
          if (qualityDifference !== 0) return qualityDifference;
          const seederDifference = (Number(b.result.seeders) || 0) - (Number(a.result.seeders) || 0);
          if (seederDifference !== 0) return seederDifference;
          return (Number(b.result.size) || 0) - (Number(a.result.size) || 0);
        })
        .map(value => value.result);
      if (!variants.length) continue;
      const title = yearEntries[0].identity.title;
      groups.push({
        key: `${normalized}|${year || 'unknown'}`,
        title: year ? `${title} (${year})` : title,
        year,
        variants,
        posterResult: variants.find(item => item.posterUrl || item.mediaTitle) || variants[0],
      });
    }
  }

  return groups;
}

type CachedFranchiseSearchMode = 'marvel' | 'marvel-hindi' | 'dc-live-action' | 'dc-live-action-hindi' | 'dc-animated' | 'classics';
type FranchisePageCacheEntry = {
  results: TorrentSearchResult[];
  totalMovies: number;
  totalPages: number;
  savedAt: number;
};

const FRANCHISE_PAGE_CACHE_STORAGE_KEY = 'home_franchise_catalogue_pages_v1';
const FRANCHISE_PAGE_CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const FRANCHISE_PAGE_CACHE_MAX_ENTRIES = 48;
const SEARCH_METADATA_CACHE_STORAGE_KEY = 'home_search_media_metadata_v1';
const SEARCH_METADATA_CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const SEARCH_METADATA_CACHE_MAX_ENTRIES = 500;
const SEARCH_AVAILABILITY_CACHE_STORAGE_KEY = 'home_search_ott_availability_v1';
const SEARCH_AVAILABILITY_CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const IMDB_RATINGS_CACHE_STORAGE_KEY = 'home_imdb_ratings_v1';
const IMDB_RATINGS_CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1000;

function readFranchisePageCache(mode: CachedFranchiseSearchMode, page: number): FranchisePageCacheEntry | null {
  try {
    const stored = JSON.parse(localStorage.getItem(FRANCHISE_PAGE_CACHE_STORAGE_KEY) || '{}');
    const entry = stored?.[mode + ':' + page] as FranchisePageCacheEntry | undefined;
    if (
      entry &&
      Array.isArray(entry.results) &&
      Date.now() - Number(entry.savedAt) < FRANCHISE_PAGE_CACHE_TTL_MS
    ) return entry;
  } catch {
    console.warn('Could not read the saved catalogue page from browser storage.');
  }
  return null;
}

function writeFranchisePageCache(
  mode: CachedFranchiseSearchMode,
  page: number,
  results: TorrentSearchResult[],
  totalMovies: number,
  totalPages: number,
): void {
  try {
    const now = Date.now();
    const stored = JSON.parse(localStorage.getItem(FRANCHISE_PAGE_CACHE_STORAGE_KEY) || '{}');
    const entries = stored && typeof stored === 'object' && !Array.isArray(stored) ? stored : {};
    entries[mode + ':' + page] = { results, totalMovies, totalPages, savedAt: now };
    const retained = Object.entries(entries)
      .filter(([, entry]) => entry && now - Number((entry as FranchisePageCacheEntry).savedAt) < FRANCHISE_PAGE_CACHE_TTL_MS)
      .sort((a, b) => Number((b[1] as FranchisePageCacheEntry).savedAt) - Number((a[1] as FranchisePageCacheEntry).savedAt))
      .slice(0, FRANCHISE_PAGE_CACHE_MAX_ENTRIES);
    localStorage.setItem(FRANCHISE_PAGE_CACHE_STORAGE_KEY, JSON.stringify(Object.fromEntries(retained)));
  } catch {
    console.warn('Could not save the catalogue page in browser storage; it will be fetched again next time.');
  }
}

function readPersistentSearchMetadata(): SearchMediaMetadata[] {
  try {
    const stored = JSON.parse(localStorage.getItem(SEARCH_METADATA_CACHE_STORAGE_KEY) || '{}');
    const now = Date.now();
    return (Array.isArray(stored?.entries) ? stored.entries : [])
      .filter((entry: any) =>
        entry?.item &&
        typeof entry.item.title === 'string' &&
        now - Number(entry.savedAt) < SEARCH_METADATA_CACHE_TTL_MS
      )
      .map((entry: any) => entry.item);
  } catch {
    console.warn('Could not read saved movie details from browser storage.');
    return [];
  }
}

function persistSearchMetadata(items: SearchMediaMetadata[]): void {
  try {
    const now = Date.now();
    const existing = readPersistentSearchMetadata();
    const merged = mergeSearchMetadataItems(existing, items);
    const entries = merged.slice(-SEARCH_METADATA_CACHE_MAX_ENTRIES)
      .map(item => ({ item, savedAt: now }));
    localStorage.setItem(SEARCH_METADATA_CACHE_STORAGE_KEY, JSON.stringify({ entries }));
  } catch {
    console.warn('Could not save movie details in browser storage; they may be fetched again.');
  }
}

function readPersistentSearchAvailability(): Record<string, {
  status: 'ready' | 'not_found' | 'not_confirmed' | 'unavailable' | 'loading';
  query: string;
  title?: string;
  year?: number | string;
  matches?: Array<{
    id: string;
    title: string;
    year?: number | string;
    type?: string;
    tmdbId?: number | string;
    status: 'ready' | 'not_found' | 'not_confirmed' | 'unavailable';
    providers: Array<{ name: string; region: string; type: string; webUrl?: string }>;
  }>;
  providers: Array<{ name: string; region: string; type: string; webUrl?: string }>;
  message?: string;
}> {
  try {
    const stored = JSON.parse(localStorage.getItem(SEARCH_AVAILABILITY_CACHE_STORAGE_KEY) || '{}');
    const now = Date.now();
    const result: Record<string, any> = {};
    for (const [key, entry] of Object.entries(stored || {})) {
      const value = entry as { savedAt?: number; value?: any };
      if (
        value?.value &&
        ['ready', 'not_confirmed', 'not_found'].includes(value.value.status) &&
        now - Number(value.savedAt) < SEARCH_AVAILABILITY_CACHE_TTL_MS
      ) result[key] = value.value;
    }
    return result;
  } catch {
    console.warn('Could not read saved streaming details from browser storage.');
    return {};
  }
}

function persistSearchAvailability(value: ReturnType<typeof readPersistentSearchAvailability>): void {
  try {
    const now = Date.now();
    const entries = Object.fromEntries(
      Object.entries(value)
        .filter(([, item]) => ['ready', 'not_confirmed', 'not_found'].includes(item.status))
        .map(([key, item]) => [key, { value: item, savedAt: now }]),
    );
    localStorage.setItem(SEARCH_AVAILABILITY_CACHE_STORAGE_KEY, JSON.stringify(entries));
  } catch {
    console.warn('Could not save streaming details in browser storage; they may be checked again.');
  }
}

function readPersistentImdbRatings(): Record<string, ImdbRatingResult> {
  try {
    const stored = JSON.parse(localStorage.getItem(IMDB_RATINGS_CACHE_STORAGE_KEY) || '{}');
    const now = Date.now();
    const result: Record<string, ImdbRatingResult> = {};
    for (const [key, entry] of Object.entries(stored || {})) {
      const cached = entry as { savedAt?: number; value?: ImdbRatingResult };
      if (
        cached?.value &&
        ['found', 'not_found'].includes(cached.value.status) &&
        now - Number(cached.savedAt) < IMDB_RATINGS_CACHE_TTL_MS
      ) result[key] = cached.value;
    }
    return result;
  } catch {
    console.warn('Could not read saved IMDb ratings from browser storage.');
  }
  return {};
}

function persistImdbRatings(value: Record<string, ImdbRatingResult>): void {
  try {
    const now = Date.now();
    const entries = Object.fromEntries(
      Object.entries(value)
        .filter(([, rating]) => ['found', 'not_found'].includes(rating.status))
        .map(([key, rating]) => [key, { value: rating, savedAt: now }]),
    );
    localStorage.setItem(IMDB_RATINGS_CACHE_STORAGE_KEY, JSON.stringify(entries));
  } catch {
    console.warn('Could not save IMDb ratings in browser storage; they may be fetched again.');
  }
}

export const TorrentSearchPanel: React.FC<TorrentSearchPanelProps> = ({ onPrepare, onCancelPrepare, onOpenProgress, seedrFiles = [], seedrDeletedFolderIds = [], onPlaySeedrFile }) => {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<TorrentSearchResult[]>([]);
  type NexStreamTitle = { id: number; title: string; date: string; overview: string; poster: string | null; type: 'movie' | 'tv' };
  const [nexStreamResults, setNexStreamResults] = useState<NexStreamTitle[]>([]);
  const [nexStreamSearchMode, setNexStreamSearchMode] = useState<'movie' | 'tv' | null>(null);
  const [nexStreamLoading, setNexStreamLoading] = useState(false);
  const [nexStreamLoadingTitle, setNexStreamLoadingTitle] = useState('');
  const [nexStreamEpisodePrefetchStatus, setNexStreamEpisodePrefetchStatus] = useState<Record<string, 'loading' | 'ready' | 'error'>>({});
  const nexStreamSearchControllerRef = useRef<AbortController | null>(null);
  const nexStreamSearchGenerationRef = useRef(0);
  const [nexStreamError, setNexStreamError] = useState('');
  const [nexStreamPlayer, setNexStreamPlayer] = useState<{ title: string; sourceName: string; url: string; type: 'movie' | 'tv'; season?: number; episode?: number } | null>(null);
  const [nexStreamResolvingId, setNexStreamResolvingId] = useState<string | null>(null);
  const [searchMetadata, setSearchMetadata] = useState<SearchMediaMetadata[]>(readPersistentSearchMetadata);
  const [searchMetadataStatus, setSearchMetadataStatus] = useState<'idle' | 'loading' | 'ready' | 'not_found' | 'unavailable'>('idle');
  const [imdbRatingsByKey, setImdbRatingsByKey] = useState<Record<string, ImdbRatingResult>>(readPersistentImdbRatings);
  const imdbRatingRequestedRef = useRef(new Set<string>());
  const [imdbRetryTick, setImdbRetryTick] = useState(0);
  const imdbInFlightRef = useRef(0);
  const imdbRetryCountRef = useRef(0);
  const tvMetadataRequestedRef = useRef(new Set<string>());
  const [searchAvailabilityByGroup, setSearchAvailabilityByGroup] = useState(readPersistentSearchAvailability);
  const [selectedQualityByGroup, setSelectedQualityByGroup] = useState<Record<string, string>>({});
  // Search results are compact cards until the user opens one with Get.
  // The live search, posters, OTT lookups and metadata prefetch keep running independently.
  const [expandedSearchGroupKey, setExpandedSearchGroupKey] = useState<string | null>(null);
  const [movieTorrentPreviewGroup, setMovieTorrentPreviewGroup] = useState<TorrentQualityGroup | null>(null);
  // A TMDB ID alone is not unique across catalogue views. Scope a Prepare
  // preview to the exact catalogue item the user clicked.
  const [movieTorrentPreviewCatalogueId, setMovieTorrentPreviewCatalogueId] = useState<string | null>(null);
  const catalogueMovieIdentity = (movie: MovieCatalogueItem, catalogueKey?: string | null) =>
    [String(catalogueKey || 'catalogue'), String(movie.id), normalizeGroupTitle(movie.title), String(movie.year || '')].join('|');
  const [pendingPosterAction, setPendingPosterAction] = useState<
    | { kind: 'catalogue'; movie: MovieCatalogueItem; catalogue: string; page: number }
    | { kind: 'search'; groupKey: string; page: number }
    | null
  >(null);
  // Preserve the exact poster URL from the catalogue card the user clicked.
  // The selected inline card must keep using that same source, even while the
  // catalogue list re-renders or torrent metadata is substituted.
  const [movieTorrentPreviewPosterUrl, setMovieTorrentPreviewPosterUrl] = useState('');
  // Capture the URL of the poster that actually loaded in the original catalogue card.
  const cataloguePosterUrlsRef = useRef<Record<string, string>>({});
  const searchPosterUrlsRef = useRef<Record<string, string>>({});
  const [movieTorrentPickerGroups, setMovieTorrentPickerGroups] = useState<TorrentQualityGroup[] | null>(null);
  const [movieTorrentSearchLoading, setMovieTorrentSearchLoading] = useState(false);
  const [movieTorrentSearchTitle, setMovieTorrentSearchTitle] = useState('');
  const [movieTorrentSearchError, setMovieTorrentSearchError] = useState('');
  const [selectedMovieTorrentQualityByGroup, setSelectedMovieTorrentQualityByGroup] = useState<Record<string, string>>({});
  const movieTorrentSearchControllerRef = useRef<AbortController | null>(null);
  const movieTorrentSearchGenerationRef = useRef(0);
  const prepareClickIntentRef = useRef(false);
  const prepareTraceIdRef = useRef<string | null>(null);
  // Keep per-title torrent results and grouped quality variants in memory across
  // catalogue switches, so reopening a Bollywood/Hollywood movie uses the same
  // already-resolved details as Marvel instead of repeating provider searches.
  const movieTorrentDetailsCacheRef = useRef(new Map<string, {
    groups: TorrentQualityGroup[];
    savedAt: number;
  }>());
  const MOVIE_TORRENT_CACHE_STORAGE_KEY = 'home_movie_torrent_details_v1';
  const MOVIE_TORRENT_CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1000;
  const NEXSTREAM_SEARCH_CACHE_KEY = 'home_nexstream_search_v1';
  const NEXSTREAM_RESOLVE_CACHE_KEY = 'home_nexstream_resolve_v1';
  const NEXSTREAM_CACHE_TTL_MS = 24 * 60 * 60 * 1000;
  const NEXSTREAM_RESOLVE_CACHE_TTL_MS = 4 * 60 * 1000;

  const persistMovieTorrentCache = () => {
    try {
      const entries = Array.from(movieTorrentDetailsCacheRef.current.entries())
        .filter(([, value]) => Date.now() - value.savedAt < MOVIE_TORRENT_CACHE_TTL_MS)
        .slice(-250);
      localStorage.setItem(MOVIE_TORRENT_CACHE_STORAGE_KEY, JSON.stringify(entries));
    } catch { /* Storage can be unavailable or full; in-memory caching still works. */ }
  };

  const readLocalCache = <T,>(
    key: string,
    cacheKey: string,
    ttlMs = NEXSTREAM_CACHE_TTL_MS,
  ): T | null => {
    try {
      const parsed = JSON.parse(localStorage.getItem(key) || '{}') as
        | Record<string, { value: T; savedAt: number }>
        | Array<[string, T & { savedAt?: number }]>;
      // Movie torrent details are persisted as [key, value] tuples, while
      // NexStream caches use an object keyed by cacheKey. Read both formats.
      if (Array.isArray(parsed)) {
        const tuple = parsed.find(entry => Array.isArray(entry) && entry[0] === cacheKey);
        if (tuple && tuple[1] && Date.now() - Number(tuple[1].savedAt || 0) < MOVIE_TORRENT_CACHE_TTL_MS) {
          return tuple[1] as T;
        }
        return null;
      }
      const entry = parsed[cacheKey];
      if (entry && Date.now() - entry.savedAt < ttlMs) return entry.value;
    } catch {}
    return null;
  };

  const writeLocalCache = <T,>(key: string, cacheKey: string, value: T) => {
    try {
      const entries = JSON.parse(localStorage.getItem(key) || '{}') as Record<string, { value: T; savedAt: number }>;
      entries[cacheKey] = { value, savedAt: Date.now() };
      const keys = Object.keys(entries);
      for (const oldKey of keys.slice(0, Math.max(0, keys.length - 300))) delete entries[oldKey];
      localStorage.setItem(key, JSON.stringify(entries));
    } catch { /* Storage can be unavailable or full. */ }
  };

  useEffect(() => {
    try {
      const entries = JSON.parse(localStorage.getItem(MOVIE_TORRENT_CACHE_STORAGE_KEY) || '[]') as Array<[string, { groups: TorrentQualityGroup[]; savedAt: number }]>;
      for (const [key, value] of entries) {
        if (value && Array.isArray(value.groups) && Date.now() - value.savedAt < MOVIE_TORRENT_CACHE_TTL_MS) {
          movieTorrentDetailsCacheRef.current.set(key, value);
        }
      }
    } catch {}
  }, []);

  const [isSearching, setIsSearching] = useState(false);
  const [isPresetCatalogueAction, setIsPresetCatalogueAction] = useState(false);
  const [searched, setSearched] = useState(false);
  const [error, setError] = useState('');
  const [catalogueBuildProgress, setCatalogueBuildProgress] = useState('');
  const [movieCatalogue, setMovieCatalogue] = useState<MovieCataloguePage | null>(null);
  const [movieCatalogueSearchText, setMovieCatalogueSearchText] = useState('');
  const [releasedOnly, setReleasedOnly] = useState(true);
  type FranchiseTmdbKey = 'marvel' | 'dc-live-action' | 'dc-animated';
  type CachedFranchiseKey = 'marvel' | 'dc-live-action';
  const [tmdbFranchiseKey, setTmdbFranchiseKey] = useState<FranchiseTmdbKey>('marvel');
  const [openCachedLanguageMenu, setOpenCachedLanguageMenu] = useState<CachedFranchiseKey | null>(null);
  type MainCatalogueMenu = 'dc' | 'bollywood' | 'hollywood';
  const [openMainCatalogueMenu, setOpenMainCatalogueMenu] = useState<MainCatalogueMenu | null>(null);
  type ActiveTopSection = 'marvel' | 'dc' | 'hollywood' | 'bollywood' | 'classics' | null;
  const [activeTopSection, setActiveTopSection] = useState<ActiveTopSection>(null);
  const [dcContentTab, setDcContentTab] = useState<'movies' | 'animated' | 'web-series'>('movies');
  type RegionalContentTab = 'movies' | 'web-series' | null;
  const [regionalContentTabs, setRegionalContentTabs] = useState<Record<RegionalCatalogueGroup, RegionalContentTab>>({ bollywood: null, hollywood: null });
  const [marvelContentTab, setMarvelContentTab] = useState<RegionalContentTab>(null);
  type ClassicsContentTab = 'movies' | 'web-series' | 'anime';
  const [classicsContentTab, setClassicsContentTab] = useState<ClassicsContentTab>('movies');
  const [activeFranchiseSearchMode, setActiveFranchiseSearchMode] = useState<CachedFranchiseSearchMode | null>(null);
  const franchisePageRequestRef = useRef<AbortController | null>(null);
  const franchisePageGenerationRef = useRef(0);
  const franchiseLoadedPagesRef = useRef(new Map<CachedFranchiseSearchMode, Set<number>>());
  const [franchiseGridPage, setFranchiseGridPage] = useState(1);
  // Hold explicit Prepare clicks until the visible catalogue posters settle.
  const prepareBuildPendingRef = useRef(false);
  const previousPosterPageRef = useRef(1);
  const [posterFirstPageReady, setPosterFirstPageReady] = useState(false);
  const posterFirstPageReadyRef = useRef(false);
  const [catalogueExpectedTitles, setCatalogueExpectedTitles] = useState(0);
  const franchiseGridPageSize = 18;
  useEffect(() => persistSearchMetadata(searchMetadata), [searchMetadata]);
  useEffect(() => persistSearchAvailability(searchAvailabilityByGroup), [searchAvailabilityByGroup]);
  useEffect(() => persistImdbRatings(imdbRatingsByKey), [imdbRatingsByKey]);
  const renderFranchisePagination = (total: number) => total > franchiseGridPageSize ? <div className="flex items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-900 px-3 py-2"><button type="button" disabled={franchiseGridPage <= 1} onClick={() => navigateFranchisePage(Math.max(1, franchiseGridPage - 1))} className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs font-semibold text-slate-200 disabled:opacity-40">← Previous</button><span className="text-xs text-slate-400">Page {franchiseGridPage} of {Math.max(1, Math.ceil(total / franchiseGridPageSize))} · {total} titles</span><button type="button" disabled={franchiseGridPage >= Math.ceil(total / franchiseGridPageSize)} onClick={() => navigateFranchisePage(Math.min(Math.ceil(total / franchiseGridPageSize), franchiseGridPage + 1))} className="rounded-lg bg-cyan-500 px-3 py-2 text-xs font-bold text-slate-950 disabled:opacity-40">Next →</button></div> : null;
  type RegionalCatalogueGroup = 'bollywood' | 'hollywood';
  type RegionalCatalogueMode = 'popular' | 'trending' | 'latest';
  const [openRegionalCatalogueMenu, setOpenRegionalCatalogueMenu] = useState<RegionalCatalogueGroup | null>(null);
  const [regionalCatalogueModes, setRegionalCatalogueModes] = useState<Record<RegionalCatalogueGroup, RegionalCatalogueMode>>({
    bollywood: 'popular',
    hollywood: 'popular',
  });
  type RegionalTvGroup = 'indian' | 'hollywood';
  type RegionalTvMode = 'popular' | 'trending' | 'latest';
  const [openRegionalTvMenu, setOpenRegionalTvMenu] = useState<RegionalTvGroup | null>(null);
  const [regionalTvModes, setRegionalTvModes] = useState<Record<RegionalTvGroup, RegionalTvMode>>({
    indian: 'popular',
    hollywood: 'popular',
  });
  const [regionalWebSeriesCatalogue, setRegionalWebSeriesCatalogue] = useState<TvCataloguePage | null>(null);
  const [regionalWebSeriesLoading, setRegionalWebSeriesLoading] = useState(false);
  const [regionalWebSeriesError, setRegionalWebSeriesError] = useState('');


  useEffect(() => {
    if (!openCachedLanguageMenu) return;

    const handleOutsidePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Element && !target.closest('[data-cached-language-menu="true"]')) {
        setOpenCachedLanguageMenu(null);
      }
    };
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpenCachedLanguageMenu(null);
    };

    document.addEventListener('pointerdown', handleOutsidePointerDown);
    document.addEventListener('keydown', handleEscape);
    return () => {
      document.removeEventListener('pointerdown', handleOutsidePointerDown);
      document.removeEventListener('keydown', handleEscape);
    };
  }, [openCachedLanguageMenu]);

  useEffect(() => {
    if (!openRegionalCatalogueMenu) return;
    const handleOutsidePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Element && !target.closest('[data-regional-catalogue-menu="true"]')) {
        setOpenRegionalCatalogueMenu(null);
      }
    };
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpenRegionalCatalogueMenu(null);
    };
    document.addEventListener('pointerdown', handleOutsidePointerDown);
    document.addEventListener('keydown', handleEscape);
    return () => {
      document.removeEventListener('pointerdown', handleOutsidePointerDown);
      document.removeEventListener('keydown', handleEscape);
    };
  }, [openRegionalCatalogueMenu]);

  useEffect(() => {
    if (!openRegionalTvMenu) return;
    const handleOutsidePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Element && !target.closest('[data-regional-tv-menu="true"]')) {
        setOpenRegionalTvMenu(null);
      }
    };
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpenRegionalTvMenu(null);
    };
    document.addEventListener('pointerdown', handleOutsidePointerDown);
    document.addEventListener('keydown', handleEscape);
    return () => {
      document.removeEventListener('pointerdown', handleOutsidePointerDown);
      document.removeEventListener('keydown', handleEscape);
    };
  }, [openRegionalTvMenu]);

  const openRegionalTvCatalogue = async (group: RegionalTvGroup, mode: RegionalTvMode) => {
    const key = group + '-' + mode;
    setRegionalTvModes(previous => ({ ...previous, [group]: mode }));
    setOpenRegionalTvMenu(null);
    setShowMarvelWebSeries(false);
    setShowDcWebSeries(false);
    setRegionalWebSeriesCatalogue(null);
    setRegionalWebSeriesError('');
    setRegionalWebSeriesLoading(true);
    setMovieCatalogue(null);
    setResults([]);
    setSearched(false);
    setIsSearching(false);
    setShowRecentSearches(false);
    try {
      setRegionalWebSeriesCatalogue(await api.getTvCatalogue(key));
    } catch (error: any) {
      setRegionalWebSeriesError(String(error?.message || 'Could not load web series.'));
    } finally {
      setRegionalWebSeriesLoading(false);
    }
  };

  const openRegionalCatalogue = (group: RegionalCatalogueGroup, mode: RegionalCatalogueMode) => {
    setRegionalWebSeriesCatalogue(null);
    setRegionalWebSeriesError('');
    setRegionalWebSeriesLoading(false);
    setShowMarvelWebSeries(false);
    setShowDcWebSeries(false);
    const key = (mode + '-' + group) as MovieCatalogueKey;
    const label = (mode.charAt(0).toUpperCase() + mode.slice(1)) + ' ' + (group === 'bollywood' ? 'Bollywood' : 'Hollywood');
    setRegionalCatalogueModes(previous => ({ ...previous, [group]: mode }));
    setOpenRegionalCatalogueMenu(null);
    setResolutionFilter(null);
    setSizeSort(null);
    setTimeSort(null);
    void openMovieCatalogue(key, label, 1);
  };

  const [isClearingTmdbCache, setIsClearingTmdbCache] = useState(false);
  const [tmdbCacheMessage, setTmdbCacheMessage] = useState('');

  const tmdbFranchiseLabels: Record<FranchiseTmdbKey, string> = {
    marvel: 'Marvel',
    'dc-live-action': 'DC',
    'dc-animated': 'DC Animated',
  };
  const [streamingOnly, setStreamingOnly] = useState(false);
  const [ottAvailability, setOttAvailability] = useState<Record<string, MovieOttCardState>>({});
  const ottAvailabilityRef = useRef<Record<string, MovieOttCardState>>({});
  const ottQueueRef = useRef<string[]>([]);
  const ottQueuedIdsRef = useRef(new Set<string>());
  const ottActiveWorkersRef = useRef(0);
  const ottLookupGenerationRef = useRef(0);
  const ottQueuePumpRef = useRef<() => void>(() => {});
  // Movies seen by the card grid, live lookups already made this session, and
  // failed-lookup retry counts. Missing streaming info is re-checked automatically.
  const ottKnownMoviesRef = useRef(new Map<string, MovieCatalogueItem>());
  const ottLiveCheckedRef = useRef(new Set<string>());
  const ottRetryAttemptsRef = useRef(new Map<string, number>());
  const ottRetryTimerRef = useRef<number | null>(null);

  const setOttState = (movieId: string, state: MovieOttCardState) => {
    const next = { ...ottAvailabilityRef.current, [movieId]: state };
    ottAvailabilityRef.current = next;
    setOttAvailability(next);
  };

  // Failed lookups are retried automatically (bounded) instead of staying on an error forever.
  const scheduleOttRetry = () => {
    if (ottRetryTimerRef.current !== null) return;
    const generation = ottLookupGenerationRef.current;
    ottRetryTimerRef.current = window.setTimeout(() => {
      ottRetryTimerRef.current = null;
      if (generation !== ottLookupGenerationRef.current) return;
      const retry = [...ottKnownMoviesRef.current.values()].filter(movie => {
        const id = String(movie.id);
        const state = ottAvailabilityRef.current[id];
        const hasData = !!state && state.status !== 'error' && state.status !== 'loading';
        return !hasData && (ottRetryAttemptsRef.current.get(id) || 0) < 5;
      });
      if (retry.length) queueOttLookups(retry, true);
    }, 30000);
  };

  // Limit upstream load. The backend caches provider hits for 24 hours and misses for 6 hours,
  // and coalesces simultaneous requests, so repeated visitors reuse the same result.
  const pumpOttQueue = () => {
    while (ottActiveWorkersRef.current < 3 && ottQueueRef.current.length > 0) {
      const movieId = ottQueueRef.current.shift();
      if (!movieId) continue;
      ottActiveWorkersRef.current += 1;
      const lookupGeneration = ottLookupGenerationRef.current;
      void (async () => {
        try {
          const availability = await api.getMovieOttAvailability(Number(movieId), 'IN');
          if (lookupGeneration === ottLookupGenerationRef.current) {
            const current = ottAvailabilityRef.current[movieId];
            const providerCount = (value: any) =>
              (Array.isArray(value?.streamingProviders) ? value.streamingProviders.length : 0) +
              (Array.isArray(value?.rentProviders) ? value.rentProviders.length : 0) +
              (Array.isArray(value?.buyProviders) ? value.buyProviders.length : 0);
            // A page-specific refresh can return an empty/incomplete response.
            // Never let it erase provider names already hydrated from the saved catalogue.
            if (providerCount(availability) === 0 && providerCount(current) > 0) {
              setOttState(movieId, current);
            } else {
              setOttState(movieId, availability);
            }
          }
        } catch (error: any) {
          if (lookupGeneration === ottLookupGenerationRef.current) {
            const current = ottAvailabilityRef.current[movieId];
            // Keep saved/cached provider info on screen if the live re-check fails.
            if (!current || current.status === 'loading' || current.status === 'error') {
              setOttState(movieId, {
                status: 'error',
                message: String(error?.message || 'OTT availability could not be checked.'),
              });
            }
            scheduleOttRetry();
          }
        } finally {
          if (lookupGeneration === ottLookupGenerationRef.current) {
            ottQueuedIdsRef.current.delete(movieId);
          }
          ottActiveWorkersRef.current -= 1;
          ottQueuePumpRef.current();
        }
      })();
    }
  };
  ottQueuePumpRef.current = pumpOttQueue;

  const queueOttLookups = (movies: MovieCatalogueItem[], retryErrors = false) => {
    const next = { ...ottAvailabilityRef.current };
    let changed = false;
    const hasProviders = (state?: MovieOttCardState) => {
      const value = state as any;
      return !!value && (
        (value.streamingProviders?.length || 0) + (value.rentProviders?.length || 0) + (value.buyProviders?.length || 0) > 0
      );
    };
    const recheckAfterMs = 6 * 60 * 60 * 1000;
    for (const movie of movies) {
      const movieId = String(movie.id);
      if (!/^\d+$/.test(movieId)) continue;
      ottKnownMoviesRef.current.set(movieId, movie);
      const existing = next[movieId];
      const isRetry = retryErrors && (!existing || existing.status === 'error' || existing.status === 'loading');

      // Hydrate saved provider data before checking whether a live lookup is
      // already queued. This matters when revisiting a page while a request for
      // the same title is still in flight.
      let state = existing && !isRetry ? existing : undefined;
      const saved = movie.ottAvailability;
      if (!state) {
        state = savedOttAvailabilityForMovie(movie);
      }
      if (state) {
        next[movieId] = state;
        changed = true;
      }

      if (ottQueuedIdsRef.current.has(movieId)) continue;
      if (existing && !isRetry && ottLiveCheckedRef.current.has(movieId)) continue;

      const checkedMs = Date.parse(String((state as any)?.checkedAt || ''));
      const savedIsFreshMiss = !!state && state.status !== 'loading' && state.status !== 'error' &&
        !hasProviders(state) && !(state as any).incomplete && !(saved as any)?.incomplete && Number.isFinite(checkedMs) && Date.now() - checkedMs < recheckAfterMs;
      const needsLive = isRetry || !state || state.status === 'loading' || state.status === 'error' ||
        (!hasProviders(state) && !savedIsFreshMiss);
      if (!needsLive) {
        ottLiveCheckedRef.current.add(movieId);
        continue;
      }
      const attempts = ottRetryAttemptsRef.current.get(movieId) || 0;
      if (isRetry && attempts >= 5) continue;
      ottRetryAttemptsRef.current.set(movieId, attempts + 1);
      ottLiveCheckedRef.current.add(movieId);
      ottQueuedIdsRef.current.add(movieId);
      ottQueueRef.current.push(movieId);
      if (!state) {
        next[movieId] = { status: 'loading' };
        changed = true;
      }
    }
    if (changed) {
      ottAvailabilityRef.current = next;
      setOttAvailability(next);
    }
    ottQueuePumpRef.current();
  };

  const ottAvailabilityForMovie = (movie: MovieCatalogueItem): MovieOttCardState | undefined => {
    const current = ottAvailability[String(movie.id)];
    if (!current || current.status === 'loading' || current.status === 'error') {
      return savedOttAvailabilityForMovie(movie) || current;
    }
    return current;
  };

  // Initialize state used by visibleMovieCatalogueItems before evaluating its memo dependencies.
  type MarvelFilmFilter = 'all' | 'mcu' | 'x-men' | 'sony';
  const [marvelFilmFilter, setMarvelFilmFilter] = useState<MarvelFilmFilter>('all');
  const [isMarvelEnglishResults, setIsMarvelEnglishResults] = useState(false);
  const [showMarvelWebSeries, setShowMarvelWebSeries] = useState(false);
  const [isDcLiveActionEnglishResults, setIsDcLiveActionEnglishResults] = useState(false);

  const visibleMovieCatalogueItems = useMemo(() => {
    let movies = movieCatalogue?.results || [];
    if (isMarvelEnglishResults) {
      movies = movies.filter(movie => {
        const rawTitle = String(movie.title || '');
        const title = rawTitle.replace(/\s*\(?((?:19|20)\d{2})\)?\s*$/, '').trim();
        const year = Number(movie.year || rawTitle.match(/\b(?:19|20)\d{2}\b/)?.[0] || 0);
        return !(year === 1977 && /^(?:the )?amazing spider[- ]?man$/i.test(title));
      });
    }
    if (isMarvelEnglishResults && !showMarvelWebSeries && marvelFilmFilter !== 'all') {
      const titleOf = (movie: MovieCatalogueItem) => String(movie.title || '').replace(/\s*\(?((?:19|20)\d{2})\)?\s*$/, '').trim().toLowerCase();
      const yearOf = (movie: MovieCatalogueItem) => Number(movie.year || String(movie.title || '').match(/\b(?:19|20)\d{2}\b/)?.[0] || 0);
      const mcuTitles = new Set([
        'ant-man','ant-man and the wasp','ant-man and the wasp: quantumania','avengers: age of ultron','avengers: endgame','avengers: infinity war','the avengers',
        'black panther','black panther: wakanda forever','black widow','captain america: brave new world','captain america: civil war',
        'captain america: the first avenger','captain america: the winter soldier','captain marvel','doctor strange',
        'doctor strange in the multiverse of madness','deadpool & wolverine','eternals','the punisher: one last kill','the punisher one last kill','a marvel television special presentation the punisher one last kill','guardians of the galaxy',
        'guardians of the galaxy vol. 2','guardians of the galaxy vol. 3','iron man','iron man 2','iron man 3',
        'shang-chi and the legend of the ten rings','spider-man: brand new day','spider-man: far from home','spider-man: homecoming',
        'spider-man: no way home','the fantastic four: first steps','the incredible hulk','the marvels','thor','thor: love and thunder',
        'thor: ragnarok','thor: the dark world','thunderbolts'
      ]);
      const isSpiderMan = (title: string) => /spider[- ]?man|spider[- ]verse|into the spider|across the spider/i.test(title);
      const isXMen = (title: string) => /x[- ]?men|wolverine|deadpool|new mutants|dark phoenix/i.test(title);
      const isSony = (title: string) => isSpiderMan(title) || /venom|morbius|kraven|madame web/i.test(title);
      movies = movies.filter(movie => {
        const title = titleOf(movie);
        const year = yearOf(movie);
        switch (marvelFilmFilter) {
          case 'mcu': return mcuTitles.has(title) ||
            /^the punisher:?\s+one last kill$/i.test(title) ||
            /^(?:a\s+)?marvel television special presentation:?\s+the punisher:?\s+one last kill$/i.test(title);
          case 'x-men': return isXMen(title);
          case 'sony': return isSony(title);
          default: return true;
        }
      });
    }
    if (isDcLiveActionEnglishResults) {
      movies = movies.filter(movie => !/batman\s*:\s*mask of the phantasm/i.test(String(movie.title || '')));
    }
    // Regional catalogues arrive pre-sorted by popularity, release date, or
    // weekly trend history; re-sorting them by release date would discard that ranking.
    const isRegionalMovieCatalogue = /^(popular|trending|latest)-(bollywood|hollywood)$/.test(
      String(movieCatalogue?.catalogue || ''),
    );
    if (!isRegionalMovieCatalogue) movies = [...movies].sort(compareReleaseDateDescending);
    if (streamingOnly) movies = movies.filter(movie => ottAvailabilityForMovie(movie)?.status === 'streaming');
    return movies;
  }, [movieCatalogue, streamingOnly, ottAvailability, isMarvelEnglishResults, showMarvelWebSeries, marvelFilmFilter, isDcLiveActionEnglishResults]);

  const ottPendingCount = movieCatalogue?.results.filter(movie => {
    const status = ottAvailabilityForMovie(movie)?.status;
    return !status || status === 'loading';
  }).length || 0;

  // Seed count is the default ranking so the strongest swarms appear first.
  // 720p/1080p are mutually exclusive. Size and Time are independent sort toggles.
  const [resolutionFilter, setResolutionFilter] = useState<'720p' | '1080p' | null>(null);
  const [sizeSort, setSizeSort] = useState<'asc' | 'desc' | null>(null);
  const [timeSort, setTimeSort] = useState<'desc' | 'asc' | null>(null);
  const [showDcWebSeries, setShowDcWebSeries] = useState(false);
  const [dcWebSeriesCatalogue, setDcWebSeriesCatalogue] = useState<TvCataloguePage | null>(null);
  const [dcWebSeriesLoading, setDcWebSeriesLoading] = useState(false);
  const [dcWebSeriesError, setDcWebSeriesError] = useState('');
  const [marvelWebSeriesCatalogue, setMarvelWebSeriesCatalogue] = useState<TvCataloguePage | null>(null);
  const [marvelWebSeriesLoading, setMarvelWebSeriesLoading] = useState(false);
  const [marvelWebSeriesError, setMarvelWebSeriesError] = useState('');
  const [showRecentSearches, setShowRecentSearches] = useState(false);
  const [preparingTorrentKey, setPreparingTorrentKey] = useState<string | null>(null);
  const [playingTorrentKey, setPlayingTorrentKey] = useState<string | null>(null);
  const [copiedTorrentKey, setCopiedTorrentKey] = useState<string | null>(null);
  const [prepareWaitTitle, setPrepareWaitTitle] = useState('');
  const [prepareWaitOpen, setPrepareWaitOpen] = useState(false);
  const [prepareError, setPrepareError] = useState('');
  const [fullTorrentTitle, setFullTorrentTitle] = useState<string | null>(null);
  const [fullTorrentTitleKey, setFullTorrentTitleKey] = useState<string | null>(null);
  const [fullTorrentTitleFading, setFullTorrentTitleFading] = useState(false);

  // Metadata is prefetched in small batches so search remains fast while the
  // most likely results are already resolved when the user clicks Add.
  const metadataCacheRef = useRef(new Map<string, {
    name: string;
    hash: string;
    files: { index: number; name: string; size: number; path: string; type: string; priority?: number }[];
    totalSize: number;
  }>());
  const recentSearchRef = useRef<HTMLDivElement | null>(null);
  const searchRequestRef = useRef<AbortController | null>(null);
  const searchGenerationRef = useRef(0);
  const fullTorrentTitleFadeTimerRef = useRef<number | null>(null);
  const fullTorrentTitleHideTimerRef = useRef<number | null>(null);

  // Poster resolution runs independently of torrent search and ranking.
  // Results are queued in seed order, but only two background resolutions run
  // at once so this never becomes a search bottleneck.
  const [posterOverrides, setPosterOverrides] = useState<Record<string, string>>({});
  const [metadataPosterFailed, setMetadataPosterFailed] = useState<Record<string, boolean>>({});
  const posterTitleCacheLoadedRef = useRef(false);
  if (!posterTitleCacheLoadedRef.current) {
    readPosterTitleCache();
    posterTitleCacheLoadedRef.current = true;
  }
  const posterLoadedRef = useRef(new Set<string>());
  const posterMetadataFailedRef = useRef(new Set<string>());
  const posterBackgroundQueueRef = useRef<Array<{ key: string; result: TorrentSearchResult }>>([]);
  const posterBackgroundQueuedRef = useRef(new Set<string>());
  const posterBackgroundActiveRef = useRef(0);
  // Track the keys currently being resolved; queued-only checks miss active jobs.
  const posterBackgroundActiveKeysRef = useRef(new Set<string>());
  const posterBackgroundGenerationRef = useRef(0);
  const posterBackgroundAttemptsRef = useRef(new Map<string, number>());
  // Avoid repeating an identical failed poster request for every quality/variant
  // of the same title. 404s expire after ten minutes so later retries can recover.
  const posterResolveCacheRef = useRef(new Map<string, { url: string; expiresAt: number }>());

  const posterKeyFor = (result: TorrentSearchResult) =>
    String(
      result.guid ||
      result.infoHash ||
      result.magnetUrl ||
      result.downloadUrl ||
      result.title
    );

  const fullTorrentTitleFor = (result: TorrentSearchResult) => {
    // Indexer search pages can truncate the displayed title, while the magnet's
    // dn parameter normally carries the actual full torrent name.
    const magnet = String(result.magnetUrl || result.downloadUrl || '').trim();
    if (magnet) {
      try {
        const query = magnet.includes('?') ? magnet.slice(magnet.indexOf('?') + 1) : '';
        const params = new URLSearchParams(query);
        const displayName = params.get('dn')?.trim();
        if (displayName) return displayName;
      } catch {
        // Fall back to the API title below.
      }
    }

    return String(result.title || result.mediaTitle || '').trim();
  };

  const rawPosterUrlFor = (result: TorrentSearchResult) => {
    const raw = String(result.posterUrl || '').trim();
    if (!raw) return '';
    return raw.startsWith('/') ? API_BASE + raw : raw;
  };

  // Load lightweight w185 thumbnails first; MovieCards upgrades them to w780 only
  // after card metadata, ratings and OTT availability have settled.
  const lowQualityPosterUrl = (value: unknown, size: 'w185' | 'w342' | 'w500' | 'w780' = 'w185') => {
    const url = String(value || '').trim();
    if (!url) return '';
    try {
      const parsed = new URL(url, window.location.origin);
      // Same-origin poster endpoints support explicit low/high variants. Set this
      // before assigning src so cached redirect URLs can never bypass the thumbnail.
      if (parsed.pathname.replace(/\/+$/, '') === '/api/poster') {
        parsed.searchParams.set('size', size === 'w780' ? 'w780' : 'w185');
        return parsed.toString();
      }
      if (parsed.hostname !== 'image.tmdb.org') return url;
      parsed.pathname = parsed.pathname.replace(/\/t\/p\/(?:original|w780|w500|w185|w342|w300|w200|w154|w92)\//, '/t/p/' + size + '/');
      return parsed.toString();
    } catch {
      return url;
    }
  };

  const posterResolveCandidateUrlsFor = (result: TorrentSearchResult) => {
    const candidates: string[] = [];
    const seen = new Set<string>();

    const addCandidate = (title: string, year = '') => {
      const clean = String(title || '')
        .replace(/[._]+/g, ' ')
        .replace(/\s+/g, ' ')
        .replace(/^[-._\s]+|[-._\s]+$/g, '')
        .trim();
      if (!clean) return;

      const yearMatches = [...clean.matchAll(/\b((?:19|20)\d{2})\b/g)];
      const trailingYear = yearMatches[yearMatches.length - 1];
      const inferredYear = year || trailingYear?.[1] || '';
      // Keep years that are part of a title (e.g. Wonder Woman 1984). Strip a
      // year from the query only when it matches the separately known release year.
      const titleOnly = trailingYear && trailingYear[1] === inferredYear
        ? clean.slice(0, trailingYear.index).trim() || clean
        : clean;

      const params = new URLSearchParams({ title: titleOnly });
      if (inferredYear) params.set('year', inferredYear);
      const url = API_BASE + '/api/poster/resolve?' + params.toString();
      if (!seen.has(url)) {
        seen.add(url);
        candidates.push(url);
      }
    };

    // Resolve from the actual title/year only. Indexer-provided poster URLs can
    // be generic thumbnails for a different movie and must not be trusted.

    const explicitYear = result.year ? String(result.year) : '';
    const mediaTitle = String(result.mediaTitle || '').trim();
    const rawTitle = String(result.title || '').trim();
    const baseTitle = mediaTitle || rawTitle;

    addCandidate(mediaTitle, explicitYear);

    const cleaned = baseTitle
      .replace(/[._]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();

    const yearMatch = cleaned.match(/\b((?:19|20)\d{2})\b/);
    const year = explicitYear || yearMatch?.[1] || '';

    if (!mediaTitle && yearMatch?.index != null) {
      addCandidate(cleaned.slice(0, yearMatch.index), year);
    }

    const releaseCut = cleaned.split(
      /\b(?:2160p|1440p|1080p|720p|576p|480p|4k|8k|web[- ]?dl|web[- ]?rip|webrip|bluray|brrip|hdrip|dvdrip|cam|hdcam|x264|x265|h264|h265|hevc|aac|ddp|atmos|proper|repack|remastered|extended|unrated|directors?\s+cut)\b/i
    )[0].trim();
    if (releaseCut) addCandidate(releaseCut, year);

    const softClean = cleaned
      .replace(
        /\b(?:hindi|tamil|telugu|malayalam|kannada|bengali|marathi|punjabi|gujarati|urdu|dual\s+audio|multi\s+audio|dubbed|dub|multi|proper|repack|remastered|extended|unrated|imax|hdr10\+?|dolby\s+vision)\b/gi,
        ' '
      )
      .replace(/\s+/g, ' ')
      .trim();
    if (softClean) addCandidate(softClean, year);

    const aliasPairs: Array<[RegExp, string]> = [
      [/\bspiderman\b/gi, 'Spider Man'],
      [/\bspider\s+man\b/gi, 'Spiderman'],
      [/\bantman\b/gi, 'Ant Man'],
      [/\bant\s+man\b/gi, 'Antman'],
      [/\bironman\b/gi, 'Iron Man'],
      [/\biron\s+man\b/gi, 'Ironman'],
      [/\bblackpanther\b/gi, 'Black Panther'],
      [/\bblack\s+panther\b/gi, 'Blackpanther'],
      [/\bdoctorstrange\b/gi, 'Doctor Strange'],
      [/\bdoctor\s+strange\b/gi, 'Doctorstrange'],
      [/\bcaptainamerica\b/gi, 'Captain America'],
      [/\bcaptain\s+america\b/gi, 'Captainamerica'],
      [/\bguardiansofthegalaxy\b/gi, 'Guardians of the Galaxy'],
    ];

    for (const [pattern, replacement] of aliasPairs) {
      if (pattern.test(cleaned)) {
        addCandidate(cleaned.replace(pattern, replacement), year);
      }
    }

    return candidates;
  };

  const posterImageUrlForResult = (result: TorrentSearchResult): string => {
    const resolveUrl = posterResolveCandidateUrlsFor(result)[0];
    if (!resolveUrl) return '';
    try {
      const url = new URL(resolveUrl, window.location.origin);
      url.pathname = url.pathname.replace('/api/poster/resolve', '/api/poster');
      url.searchParams.set('size', 'w185');
      return url.toString();
    } catch {
      return resolveUrl.replace('/api/poster/resolve?', '/api/poster?') +
        (resolveUrl.includes('?') ? '&' : '?') + 'size=w185';
    }
  };

  const resolvePosterInBackground = async (url: string, generation: number) => {
    if (generation !== posterBackgroundGenerationRef.current) return '';

    const now = Date.now();
    const cached = posterResolveCacheRef.current.get(url);
    if (cached && cached.expiresAt > now) return cached.url;
    if (cached) posterResolveCacheRef.current.delete(url);

    try {
      const response = await fetch(url, {
        method: 'GET',
        credentials: 'include',
        cache: 'force-cache'
      });

      if (!response.ok) {
        if (response.status === 404) {
          posterResolveCacheRef.current.set(url, { url: '', expiresAt: Date.now() + 10 * 60 * 1000 });
        }
        return '';
      }

      const data = await response.json().catch(() => null);
      const resolved = String(data?.url || '').trim();
      if (resolved) {
        posterResolveCacheRef.current.set(url, { url: resolved, expiresAt: Date.now() + 6 * 60 * 60 * 1000 });
      } else {
        posterResolveCacheRef.current.set(url, { url: '', expiresAt: Date.now() + 10 * 60 * 1000 });
      }
      return resolved;
    } catch {
      // Network/server failures are not cached; allow transient failures to retry.
      return '';
    }
  };

  const startPosterBackgroundWorkers = () => {
    const generation = posterBackgroundGenerationRef.current;

    while (
      posterBackgroundActiveRef.current < 2 &&
      posterBackgroundQueueRef.current.length > 0
    ) {
      const job = posterBackgroundQueueRef.current.shift();
      if (!job) break;

      posterBackgroundQueuedRef.current.delete(job.key);
      posterBackgroundActiveRef.current += 1;
      posterBackgroundActiveKeysRef.current.add(job.key);

      void (async () => {
        try {
          if (generation !== posterBackgroundGenerationRef.current) return;

          const key = job.key;

          // A visible/loaded poster wins immediately; don't spend background
          // work resolving something the browser already has.
          if (posterLoadedRef.current.has(key)) return;

          const candidates = posterResolveCandidateUrlsFor(job.result);
          for (const candidate of candidates) {
            if (generation !== posterBackgroundGenerationRef.current) return;

            const resolved = await resolvePosterInBackground(candidate, generation);
            if (resolved) {
              for (const titleCacheKey of posterTitleCacheKeys(job.result)) {
                savePosterTitleCache(titleCacheKey, resolved);
              }
              setPosterOverrides(prev => (
                prev[key] === resolved ? prev : { ...prev, [key]: resolved }
              ));
              return;
            }

            await new Promise(resolve => window.setTimeout(resolve, 250));
          }

          // One delayed retry gives transient provider failures another chance.
          const attempts = posterBackgroundAttemptsRef.current.get(key) || 0;
          if (attempts < 1 && generation === posterBackgroundGenerationRef.current) {
            posterBackgroundAttemptsRef.current.set(key, attempts + 1);
            window.setTimeout(() => {
              if (generation !== posterBackgroundGenerationRef.current) return;
              if (posterLoadedRef.current.has(key)) return;
              if (posterBackgroundQueuedRef.current.has(key)) return;

              posterBackgroundQueuedRef.current.add(key);
              posterBackgroundQueueRef.current.push(job);
              startPosterBackgroundWorkers();
            }, 45000);
          }
        } finally {
          posterBackgroundActiveKeysRef.current.delete(job.key);
          posterBackgroundActiveRef.current = Math.max(
            0,
            posterBackgroundActiveRef.current - 1
          );

          if (generation === posterBackgroundGenerationRef.current) {
            startPosterBackgroundWorkers();
          }
        }
      })();
    }
  };

  const startPosterBackgroundSearch = (items: TorrentSearchResult[]) => {
    posterBackgroundGenerationRef.current += 1;
    posterBackgroundQueueRef.current = [];
    posterBackgroundQueuedRef.current.clear();
    posterBackgroundActiveKeysRef.current.clear();
    posterBackgroundAttemptsRef.current.clear();
    posterLoadedRef.current.clear();

    const generation = posterBackgroundGenerationRef.current;

    for (const result of items) {
      const key = posterKeyFor(result);
      if (!key || posterBackgroundQueuedRef.current.has(key) || posterBackgroundActiveKeysRef.current.has(key)) continue;

      // Reuse title/year artwork across different torrent releases and searches.
      const cachedPoster = cachedPosterForTitle(result);
      if (cachedPoster) {
        setPosterOverrides(prev => (
          prev[key] === cachedPoster ? prev : { ...prev, [key]: cachedPoster }
        ));
        continue;
      }

      posterBackgroundQueuedRef.current.add(key);
      posterBackgroundQueueRef.current.push({ key, result });
    }

    // Start immediately after results arrive, but never await this work.
    if (generation === posterBackgroundGenerationRef.current) {
      startPosterBackgroundWorkers();
    }
  };

  // Append poster jobs without resetting the queue (used when more catalogue pages stream in).
  const enqueuePosterBackgroundJobs = (items: TorrentSearchResult[]) => {
    for (const result of items) {
      const key = posterKeyFor(result);
      if (!key || posterBackgroundQueuedRef.current.has(key)) continue;
      const cachedPoster = cachedPosterForTitle(result);
      if (cachedPoster) {
        setPosterOverrides(prev => (prev[key] === cachedPoster ? prev : { ...prev, [key]: cachedPoster }));
        continue;
      }
      posterBackgroundQueuedRef.current.add(key);
      posterBackgroundQueueRef.current.push({ key, result });
    }
    startPosterBackgroundWorkers();
  };

  const posterUrlFor = (result: TorrentSearchResult) =>
    lowQualityPosterUrl(posterOverrides[posterKeyFor(result)] || '');

  const normalizeMatchText = (value: string) =>
    String(value || '')
      .toLowerCase()
      .replace(/\.[a-z0-9]{2,5}$/i, '')
      .replace(/[^a-z0-9]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();

  const findPreparedFiles = (result: TorrentSearchResult): SeedrSearchFile[] => {
    const title = normalizeMatchText(result.title);
    if (!title || title.length < 4) return [];

    // Only auto-associate an existing Seedr file when the torrent title matches
    // the actual Seedr folder/file name exactly. The previous fuzzy fallback
    // could make one downloaded torrent appear as "Play" on several different
    // search results for the same movie (e.g. different qualities/releases).
    return seedrFiles.filter(file => {
      const folderPath = String(file.folderPath || '');
      const folderName = folderPath.split('/').filter(Boolean).pop() || '';
      const fileName = normalizeMatchText(file.name);
      const folder = normalizeMatchText(folderName);

      return folder === title || fileName === title;
    });
  };

  const preparedForResult = (result: TorrentSearchResult): SeedrSearchFile[] => {
    const key = result.infoHash || result.magnetUrl || result.downloadUrl || result.sourceUrl || result.title;
    const deletedIds = new Set(seedrDeletedFolderIds.map(id => String(id)));
    const local = preparedByKeyRef.current.get(key);
    const localFiles = local?.files?.filter(file => !deletedIds.has(String(file.folderId))) || [];
    return localFiles.length ? localFiles : findPreparedFiles(result);
  };

  const preparedByKeyRef = useRef(new Map<string, { files: SeedrSearchFile[] }>());
  // Recent searches belong to this Home deployment. Use the same-origin API
  // base as every other Home request; a hard-coded Render URL causes browser CORS
  // failures when Home is opened from the Oracle VPS.
  const apiFetchRecent = (input: RequestInfo | URL, init?: RequestInit) => {
    const value = String(input);
    const url = value.startsWith('/') ? API_BASE + value : value;
    return fetch(url, { ...init, credentials: 'include' });
  };

  const [recentSearches, setRecentSearches] = useState<string[]>(() => {
    try {
      const saved = localStorage.getItem('seedflow_recent_searches');
      const parsed = saved ? JSON.parse(saved) : [];
      return Array.isArray(parsed)
        ? parsed.filter((value): value is string => typeof value === 'string' && !isPresetSearchLabel(value)).slice(0, 10)
        : [];
    } catch {
      return [];
    }
  });

  useEffect(() => {
    let cancelled = false;

    apiFetchRecent('/api/search/recent')
      .then(response => response.ok ? response.json() : null)
      .then(data => {
        if (cancelled) return;
        const serverRecents = Array.isArray(data?.searches)
          ? data.searches.filter((value: unknown): value is string => typeof value === 'string' && !isPresetSearchLabel(value)).slice(0, 7)
          : [];
        if (serverRecents.length > 0) {
          setRecentSearches(serverRecents);
          try {
            localStorage.setItem('seedflow_recent_searches', JSON.stringify(serverRecents));
          } catch {}
        }
      })
      .catch(() => {});

    return () => {
      cancelled = true;
    };
  }, []);

  const showFullTorrentTitleBubble = (result: TorrentSearchResult) => {
    if (fullTorrentTitleFadeTimerRef.current !== null) {
      window.clearTimeout(fullTorrentTitleFadeTimerRef.current);
    }
    if (fullTorrentTitleHideTimerRef.current !== null) {
      window.clearTimeout(fullTorrentTitleHideTimerRef.current);
    }

    const key = posterKeyFor(result);
    setFullTorrentTitleKey(key);
    setFullTorrentTitle(String(result.title || result.mediaTitle || '').trim());
    setFullTorrentTitleFading(false);

    fullTorrentTitleFadeTimerRef.current = window.setTimeout(() => {
      setFullTorrentTitleFading(true);
    }, 5550);

    fullTorrentTitleHideTimerRef.current = window.setTimeout(() => {
      setFullTorrentTitle(null);
      setFullTorrentTitleKey(null);
      setFullTorrentTitleFading(false);
    }, 6000);
  };

  useEffect(() => {
    return () => {
      if (fullTorrentTitleFadeTimerRef.current !== null) {
        window.clearTimeout(fullTorrentTitleFadeTimerRef.current);
      }
      if (fullTorrentTitleHideTimerRef.current !== null) {
        window.clearTimeout(fullTorrentTitleHideTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!showRecentSearches) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (target && !recentSearchRef.current?.contains(target)) {
        setShowRecentSearches(false);
      }
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [showRecentSearches]);

  const saveRecentSearch = (value: string) => {
    const normalized = value.trim();
    if (!normalized) return;

    setRecentSearches(prev => {
      const next = [
        normalized,
        ...prev.filter(item => item.toLowerCase() !== normalized.toLowerCase())
      ].slice(0, 7);

      try {
        localStorage.setItem('seedflow_recent_searches', JSON.stringify(next));
      } catch {}

      void apiFetchRecent('/api/search/recent', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ search: normalized })
      }).catch(() => {});

      return next;
    });
  };








  const cancelSearch = () => {
    // Invalidate the active request before aborting so late responses cannot update the UI.
    searchGenerationRef.current += 1;
    searchRequestRef.current?.abort();
    searchRequestRef.current = null;
    movieTorrentSearchGenerationRef.current += 1;
    movieTorrentSearchControllerRef.current?.abort();
    movieTorrentSearchControllerRef.current = null;
    nexStreamSearchGenerationRef.current += 1;
    nexStreamSearchControllerRef.current?.abort();
    nexStreamSearchControllerRef.current = null;
    setNexStreamLoading(false);
    setNexStreamSearchMode(null);
    setNexStreamResults([]);
    setNexStreamError('');
    setIsSearching(false);
    setCatalogueBuildProgress('');
    setError('');
    setShowRecentSearches(false);
    setMovieCatalogue(null);
    setResults([]);
    setSearchAvailabilityByGroup(readPersistentSearchAvailability());
    setSearched(false);
  };

  const searchNexStreamTitles = async (titleQuery: string, mediaType: 'movie' | 'tv' = 'movie') => {
    nexStreamSearchControllerRef.current?.abort();
    const controller = new AbortController();
    nexStreamSearchControllerRef.current = controller;
    const generation = ++nexStreamSearchGenerationRef.current;
    setNexStreamSearchMode(mediaType);
    setNexStreamLoadingTitle(String(titleQuery || '').trim());
    setNexStreamLoading(true);
    setNexStreamError('');
    setNexStreamResults([]);
    setNexStreamPlayer(null);
    try {
      const types: Array<'movie' | 'tv'> = [mediaType];
      const cachedNexResults = readLocalCache<NexStreamTitle[]>(NEXSTREAM_SEARCH_CACHE_KEY, mediaType + ':' + String(titleQuery || '').trim().toLowerCase());
      if (cachedNexResults) {
        setNexStreamResults(cachedNexResults);
        if (mediaType === 'tv') {
          setNexStreamEpisodePrefetchStatus({});
          prefetchNexStreamEpisodeMetadata(cachedNexResults, (id, status) => {
            setNexStreamEpisodePrefetchStatus(previous => ({ ...previous, [String(id)]: status }));
          });
        }
        setNexStreamLoading(false);
        setNexStreamLoadingTitle('');
        return;
      }
      const responses = await Promise.allSettled(types.map(async type => {
        const params = new URLSearchParams({ query: titleQuery, type });
        const response = await fetch('/api/nexstream/search?' + params.toString(), {
          headers: { Accept: 'application/json' },
          credentials: 'same-origin',
          signal: controller.signal,
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.success) {
          throw new Error(data.detail || 'NexStream title search failed.');
        }
        return (Array.isArray(data.results) ? data.results : []) as NexStreamTitle[];
      }));
      if (generation !== nexStreamSearchGenerationRef.current) return;
      const successfulResults = responses
        .filter((response): response is PromiseFulfilledResult<NexStreamTitle[]> => response.status === 'fulfilled')
        .flatMap(response => response.value);
      const failedSearches = responses.filter(response => response.status === 'rejected');
      const unique = new Map<string, NexStreamTitle>();
      for (const item of successfulResults) {
        unique.set(item.type + ':' + item.id, item);
      }
      const resolvedTitles = Array.from(unique.values());
      writeLocalCache(NEXSTREAM_SEARCH_CACHE_KEY, mediaType + ':' + String(titleQuery || '').trim().toLowerCase(), resolvedTitles);
      setNexStreamResults(resolvedTitles);
      // Fetch episode lists and thumbnails in the background, without delaying
      // the result cards. The first few TV cards are ready by the time a user
      // opens the episode picker; remaining cards are prefetched with 3 workers.
      if (mediaType === 'tv') {
        setNexStreamEpisodePrefetchStatus({});
        prefetchNexStreamEpisodeMetadata(resolvedTitles, (id, status) => {
          setNexStreamEpisodePrefetchStatus(previous => ({ ...previous, [String(id)]: status }));
        });
      }
      if (failedSearches.length === types.length) {
        const firstFailure = failedSearches[0] as PromiseRejectedResult;
        throw firstFailure.reason instanceof Error ? firstFailure.reason : new Error('TMDB title search failed.');
      }
      if (failedSearches.length > 0) {
        setNexStreamError('Some NexStream results could not be loaded. Try searching again to refresh them.');
      }
    } catch (error: any) {
      if (generation !== nexStreamSearchGenerationRef.current || error?.name === 'AbortError') return;
      setNexStreamError(String(error?.message || 'NexStream search is temporarily unavailable.'));
    } finally {
      if (generation === nexStreamSearchGenerationRef.current) {
        setNexStreamLoading(false);
        setNexStreamLoadingTitle('');
        if (nexStreamSearchControllerRef.current === controller) nexStreamSearchControllerRef.current = null;
      }
    }
  };

  const [nexStreamEpisodePicker, setNexStreamEpisodePicker] = useState<NexStreamTitle | null>(null);
  const [nexStreamSeasons, setNexStreamSeasons] = useState<Array<{ seasonNumber: number; name: string; episodeCount: number }>>([]);
  const [nexStreamEpisodes, setNexStreamEpisodes] = useState<Array<{ episodeNumber: number; name: string; overview: string; airDate: string; still: string | null }>>([]);
  const [nexStreamSelectedSeason, setNexStreamSelectedSeason] = useState(1);
  const [nexStreamEpisodesLoading, setNexStreamEpisodesLoading] = useState(false);
  const [nexStreamEpisodesError, setNexStreamEpisodesError] = useState('');

  const loadNexStreamEpisodes = async (item: NexStreamTitle, season: number) => {
    const cached = nexStreamEpisodeCache.get(nexStreamEpisodeCacheKey(item.id, season));
    if (cached) {
      setNexStreamSeasons(cached.seasons);
      setNexStreamEpisodes(cached.episodes);
      setNexStreamSelectedSeason(cached.selectedSeason);
      setNexStreamEpisodesError('');
      setNexStreamEpisodesLoading(false);
      return;
    }

    setNexStreamEpisodesLoading(true);
    setNexStreamEpisodesError('');
    try {
      const data = await fetchNexStreamEpisodeMetadata(item.id, season);
      setNexStreamSeasons(data.seasons);
      setNexStreamEpisodes(data.episodes);
      setNexStreamSelectedSeason(data.selectedSeason);
    } catch (error: any) {
      setNexStreamEpisodesError(String(error?.message || 'Could not load episode list.'));
      setNexStreamEpisodes([]);
    } finally {
      setNexStreamEpisodesLoading(false);
    }
  };

  // Warm the episode metadata cache while the franchise TV catalogue is
  // open, so selecting a series usually opens with its season/episodes ready.
  useEffect(() => {
    const catalogue = showDcWebSeries ? dcWebSeriesCatalogue
      : showMarvelWebSeries ? marvelWebSeriesCatalogue
        : null;
    if (!catalogue?.results?.length) return;

    const items = catalogue.results
      .map(show => ({ id: Number(show.id), type: 'tv' as const }))
      .filter(item => Number.isInteger(item.id) && item.id > 0);
    if (!items.length) return;

    // Episode metadata is only fetched when the user opens a series card; prefetching
    // here competed with the initial cards and posters for the page.
    return;
  }, [
    showDcWebSeries,
    showMarvelWebSeries,
    dcWebSeriesCatalogue,
    marvelWebSeriesCatalogue,
  ]);

  const playWithNexStream = async (item: NexStreamTitle, chosenSeason?: number, chosenEpisode?: number, openInNewWindow = false) => {
    const popup = openInNewWindow ? window.open('about:blank', '_blank') : null;
    if (item.type === 'tv' && (chosenSeason === undefined || chosenEpisode === undefined)) {
      setNexStreamEpisodePicker(item);
      setNexStreamEpisodes([]);
      setNexStreamSeasons([]);
      setNexStreamEpisodesError('');
      await loadNexStreamEpisodes(item, 1);
      return;
    }
    const season = chosenSeason;
    const episode = chosenEpisode;

    const resolvingId = item.type + ':' + item.id;
    setNexStreamResolvingId(resolvingId);
    setNexStreamError('');
    try {
      const resolveCacheKey = item.type + ':' + item.id + ':' + String(season ?? '') + ':' + String(episode ?? '');
      const cachedResolve = readLocalCache<{ url: string; name: string }>(
        NEXSTREAM_RESOLVE_CACHE_KEY,
        resolveCacheKey,
        NEXSTREAM_RESOLVE_CACHE_TTL_MS,
      );
      let source: { url: string; name?: string } | undefined;
      if (cachedResolve && /^https:\/\//i.test(cachedResolve.url)) {
        source = cachedResolve;
      } else {
        const params = new URLSearchParams({ type: item.type, tmdb_id: String(item.id) });
        if (item.type === 'tv') {
          params.set('season', String(season));
          params.set('episode', String(episode));
        }
        const response = await fetch('/api/nexstream/resolve?' + params.toString(), {
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.success || !Array.isArray(data.sources) || !data.sources.length) {
        throw new Error(data.detail || 'NexStream could not resolve this title.');
      }
        const resolvedSource = data.sources.find((candidate: any) => {
          try {
            const url = new URL(String(candidate?.url || ''));
            return url.protocol === 'https:' && ['embedmaster.link', 'www.embedmaster.link'].includes(url.hostname);
          } catch {
            return false;
          }
        });
        if (!resolvedSource) throw new Error('NexStream returned no supported player source.');
        source = { url: String(resolvedSource.url), name: String(resolvedSource.name || 'NexStream') };
        writeLocalCache(NEXSTREAM_RESOLVE_CACHE_KEY, resolveCacheKey, source);
      }
      if (!source) throw new Error('NexStream returned no supported player source.');
      if (popup) {
        popup.location.href = String(source.url);
      } else if (openInNewWindow) {
        setNexStreamError('Your browser blocked the new window. Allow pop-ups and try again.');
      } else {
        setNexStreamPlayer({
          title: item.title,
          year: String(item.date || '').slice(0, 4),
          sourceName: String(source.name || 'NexStream'),
          url: String(source.url),
          type: item.type,
          season,
          episode,
        });
      }
      setNexStreamError('');
    } catch (error: any) {
      setNexStreamError(String(error?.message || 'Could not start NexStream playback.'));
    } finally {
      setNexStreamResolvingId(null);
    }
  };

  const playCatalogueTvWithNexStream = (show: {
    id: number | string;
    title: string;
    year?: number | null;
    releaseDate?: string;
    overview?: string;
    posterUrl?: string;
  }) => {
    // Catalogue IDs come directly from the cached TMDB TV catalogue. Do not
    // title-search these shows: pass the exact series ID into the episode flow.
    const tmdbId = Number(show.id);
    if (!Number.isInteger(tmdbId) || tmdbId <= 0) {
      setNexStreamSearchMode('tv');
      setNexStreamError('This TV catalogue entry has no valid TMDB series ID.');
      return;
    }

    // The episode picker is rendered inside the NexStream panel, which is
    // hidden unless a NexStream search mode is active. Activate the TV panel
    // before opening the picker so catalogue buttons work without title search.
    setNexStreamSearchMode('tv');
    setNexStreamResults([]);
    setNexStreamLoading(false);
    setNexStreamLoadingTitle('');
    setNexStreamError('');
    void playWithNexStream({
      id: tmdbId,
      title: show.title,
      date: show.releaseDate || (show.year ? String(show.year) : ''),
      overview: show.overview || '',
      poster: show.posterUrl || null,
      type: 'tv',
    });
  };

  const runSearch = async (event?: React.FormEvent, searchOverride?: string, searchMode?: 'marvel' | 'marvel-hindi' | 'dc-live-action' | 'dc-live-action-hindi' | 'dc-animated' | 'classics') => {
    event?.preventDefault();
    setHighQualityPostersReady(false);
    setFranchiseGridPage(1);
    setActiveFranchiseSearchMode(searchMode || null);
    franchisePageRequestRef.current?.abort();
    franchisePageRequestRef.current = null;
    franchisePageGenerationRef.current += 1;
    franchiseLoadedPagesRef.current.clear();
    posterFirstPageReadyRef.current = false;
    setPosterFirstPageReady(false);
    setIsPresetCatalogueAction(Boolean(searchMode));
    setIsMarvelEnglishResults(searchMode === 'marvel');
    setIsDcLiveActionEnglishResults(searchMode === 'dc-live-action');
    setShowDcWebSeries(false);
    setIsDcLiveActionEnglishResults(searchMode === 'dc-live-action');
    setShowMarvelWebSeries(false);
    setShowDcWebSeries(false);
    const chronologicalFranchiseModes = new Set(['marvel', 'marvel-hindi', 'dc-live-action', 'dc-live-action-hindi', 'dc-animated', 'classics']);
    setMovieTorrentSearchError('');
    setMovieTorrentPreviewCatalogueId(null);
    movieTorrentSearchControllerRef.current?.abort();
    movieTorrentSearchGenerationRef.current += 1;
    setMovieTorrentSearchLoading(false);
    setMovieTorrentPickerGroups(null);
    setMovieTorrentPreviewGroup(null);

    const trimmed = (searchOverride ?? query).trim();
    if (!searchMode) setIsPresetCatalogueAction(false);
    if (trimmed.length < 2) {
      setError('Enter at least 2 characters to search.');
      setResults([]);
      setSearched(false);
      return;
    }

    const generation = ++searchGenerationRef.current;
    searchRequestRef.current?.abort();
    const controller = new AbortController();
    searchRequestRef.current = controller;

    try {
      setIsSearching(true);
      setCatalogueBuildProgress('');
      setCatalogueExpectedTitles(0);
      setMovieCatalogue(null);
      setShowRecentSearches(false);
      setError('');
      setResults([]);
      setSearchMetadata(readPersistentSearchMetadata());
      setSearchMetadataStatus('idle');
      setSearchAvailabilityByGroup(readPersistentSearchAvailability());
      setSelectedQualityByGroup({});
      setExpandedSearchGroupKey(null);
      setSearched(false);
      setPosterOverrides({});
      posterMetadataFailedRef.current.clear();
      posterLoadedRef.current.clear();
      posterBackgroundQueueRef.current = [];
      posterBackgroundQueuedRef.current.clear();
      posterBackgroundAttemptsRef.current.clear();
      posterBackgroundGenerationRef.current += 1;
      // Preset catalogue searches are actions, not user-entered queries.
      // Keep the input empty and never add these labels to local or server history.
      if (searchMode) {
        setQuery('');
      } else {
        saveRecentSearch(trimmed);
      }
      setCatalogueBuildProgress('');

      // Normal search also searches NexStream movies in parallel; neither source blocks cached torrent cards.
      if (!searchMode && trimmed.length >= 2) void searchNexStreamTitles(trimmed, 'movie');
      else {
        nexStreamSearchGenerationRef.current += 1;
        nexStreamSearchControllerRef.current?.abort();
        nexStreamSearchControllerRef.current = null;
        setNexStreamLoading(false);
        setNexStreamSearchMode(null);
        setNexStreamResults([]);
        setNexStreamError('');
        setNexStreamPlayer(null);
      }

      // Start TMDB metadata immediately and let it load alongside cached/live torrent
      // search. The first cards and Seedr preparation do not wait for this request.
      if (!searchMode && trimmed.length >= 2) {
        setSearchMetadataStatus('loading');
        void api.searchMediaMetadata(trimmed, controller.signal).then(metadataResponse => {
          if (generation !== searchGenerationRef.current) return;
          setSearchMetadata(previous => mergeSearchMetadataItems(
            previous,
            Array.isArray(metadataResponse.results) ? metadataResponse.results : [],
          ));
          setSearchMetadataStatus(metadataResponse.status || 'unavailable');
        }).catch(() => {
          if (generation !== searchGenerationRef.current) return;
          // The VPS catalogue lookup may already have supplied saved synopses.
          // A failed live TMDB request must not erase those successful cache hits.
          setSearchMetadataStatus('unavailable');
        });
      }

      // Search the persistent torrent catalogues in parallel with live indexers.
      // Cached matches are shown as soon as they arrive; live results are merged in later.
      const cachedSearchPromise: Promise<TorrentSearchResult[]> = searchMode
        ? Promise.resolve([])
        : Promise.allSettled([
            api.getMarvelCatalogue(controller.signal),
            ...[
              'marvel-hindi',
              'dc-live-action',
              'dc-live-action-hindi',
              'dc-animated',
            ].map(key => api.getCatalogue(key, controller.signal)),
          ].map(request => request.then(response => {
            // Publish each ready catalogue immediately instead of waiting for all seven.
            if (generation !== searchGenerationRef.current || response.status !== 'ready') return response;
            const compactQuery = normalizeGroupTitle(trimmed).replace(/\\s+/g, '');
            const seen = new Set<string>();
            const matches = response.results.filter(row => {
              const titleKey = torrentMediaIdentity(row).normalized.replace(/\\s+/g, '');
              if (!titleKey.includes(compactQuery) && !compactQuery.includes(titleKey)) return false;
              const key = torrentResultKey(row);
              if (seen.has(key)) return false;
              seen.add(key);
              return true;
            });
            if (matches.length) {
              setResults(previous => {
                const byKey = new Map<string, TorrentSearchResult>();
                [...matches, ...previous].forEach(row => {
                  const key = torrentResultKey(row);
                  if (!byKey.has(key)) byKey.set(key, row);
                });
                return Array.from(byKey.values());
              });
              setSearched(true);
              // Keep poster resolution independent of the search response.
            }
            return response;
          }))).then(responses => {
            const compactQuery = normalizeGroupTitle(trimmed).replace(/\\s+/g, '');
            const cachedRows = responses.flatMap(response =>
              response.status === 'fulfilled' && response.value.status === 'ready'
                ? response.value.results
                : []
            );
            const seen = new Set<string>();
            return cachedRows.filter(row => {
              const titleKey = torrentMediaIdentity(row).normalized.replace(/\\s+/g, '');
              if (!titleKey.includes(compactQuery) && !compactQuery.includes(titleKey)) return false;
              const key = torrentResultKey(row);
              if (seen.has(key)) return false;
              seen.add(key);
              return true;
            });
          }).catch(() => []);

      if (!searchMode) {
        void cachedSearchPromise.then(cached => {
          if (generation !== searchGenerationRef.current || !cached.length) return;
          setResults(previous => {
            const byKey = new Map<string, TorrentSearchResult>();
            [...cached, ...previous].forEach(row => byKey.set(torrentResultKey(row), row));
            return Array.from(byKey.values());
          });
          setSearched(true);
        });
      }

      let data: TorrentSearchResult[];
      if (searchMode) {
        const labels: Record<string, string> = {
          marvel: 'Marvel English', 'marvel-hindi': 'Marvel Hindi',
          'dc-live-action': 'DC Live-Action English', 'dc-live-action-hindi': 'DC Live-Action Hindi',
          'dc-animated': 'DC animated',
          classics: 'Classics'
        };
        // Load only the current page initially. Later pages are fetched only when
        // the user navigates to them, so opening a section does not warm the full catalogue.
        const CATALOGUE_PAGE_SIZE = franchiseGridPageSize;
        const fetchCataloguePage = (page: number) => searchMode === 'marvel'
          ? api.getMarvelCatalogue(controller.signal, { page, pageSize: CATALOGUE_PAGE_SIZE })
          : api.getCatalogue(searchMode, controller.signal, { page, pageSize: CATALOGUE_PAGE_SIZE });

        const browserCachedPage = readFranchisePageCache(searchMode, 1);
        let catalogue: MarvelCatalogueResponse = browserCachedPage
          ? {
              status: 'ready' as const,
              results: browserCachedPage.results,
              completed: browserCachedPage.totalMovies,
              total: browserCachedPage.totalMovies,
              totalMovies: browserCachedPage.totalMovies,
              totalPages: browserCachedPage.totalPages,
              refreshing: false,
            }
          : await fetchCataloguePage(1);
        let catalogueAttempts = 0;
        while (
          (catalogue.status === 'building' || catalogue.status === 'idle') &&
          catalogueAttempts < 8
        ) {
          if (generation !== searchGenerationRef.current || controller.signal.aborted) return;
          setCatalogueBuildProgress(
            'Building shared ' + searchMode.replace(/-/g, ' ') + ' catalogue: ' +
            catalogue.completed + ' of ' + catalogue.total + ' searches completed.'
          );
          await new Promise<void>(resolve => window.setTimeout(resolve, 2200));
          catalogueAttempts += 1;
          catalogue = await fetchCataloguePage(1);
        }
        if (generation !== searchGenerationRef.current || controller.signal.aborted) return;
        if (catalogue.status !== 'ready' || !Array.isArray(catalogue.results)) {
          throw new Error(catalogue.error || 'Could not load the first catalogue page.');
        }
        data = catalogue.results;
        const totalCataloguePages = Number(catalogue.totalPages) || 1;
        const expectedTitles = Number(catalogue.totalMovies) || (totalCataloguePages * CATALOGUE_PAGE_SIZE);
        setCatalogueExpectedTitles(totalCataloguePages > 1 ? expectedTitles : 0);
        if (!browserCachedPage) {
          writeFranchisePageCache(searchMode, 1, data, expectedTitles, totalCataloguePages);
        }
        franchiseLoadedPagesRef.current.set(searchMode, new Set([1]));
        setCatalogueBuildProgress(
          catalogue.refreshing
            ? 'Showing the cached first page; newer catalogue data will be used on a later visit.'
            : ''
        );
      } else {
        const livePromise = api.searchTorrents(trimmed, 50, controller.signal);
        // Do not make live torrent results wait for the seven catalogue lookups.
        // Cached catalogue matches are merged independently as each search completes.
        const liveResults = await livePromise;
        data = [...liveResults];
        const unique = new Map<string, TorrentSearchResult>();
        data.forEach(row => {
          const key = torrentResultKey(row);
          if (!unique.has(key)) unique.set(key, row);
        });
        data = Array.from(unique.values());
      }

      // The backend owns low-result TV/season fallback. Keeping that logic
      // server-side avoids launching duplicate season searches from the browser.

      // Keep the cached franchise/favourites catalogues in full release-date
      // order even if an older cache snapshot or API response is unsorted.
      if (searchMode && chronologicalFranchiseModes.has(searchMode)) {
        data = [...data].sort((a, b) => {
          const dateOrder = releaseDateSortValue(b) - releaseDateSortValue(a);
          if (dateOrder !== 0) return dateOrder;

          const aIdentity = torrentMediaIdentity(a);
          const bIdentity = torrentMediaIdentity(b);
          const titleOrder = aIdentity.normalized.localeCompare(
            bIdentity.normalized,
            undefined,
            { numeric: true },
          );
          if (titleOrder !== 0) return titleOrder;

          return (Number(b.seeders) || 0) - (Number(a.seeders) || 0);
        });
      }

      // Never let an older request overwrite a newer search. This matters
      // when a slow 1337x fallback finishes after a later click.
      if (generation !== searchGenerationRef.current) return;

      // Preserve any catalogue matches that arrived while live indexers were running.
      setResults(previous => {
        const byKey = new Map<string, TorrentSearchResult>();
        [...previous, ...data].forEach(row => {
          const key = torrentResultKey(row);
          if (!byKey.has(key)) byKey.set(key, row);
        });
        return Array.from(byKey.values());
      });
      setSearched(true);
      if (searchMode) setIsSearching(false);

      void (async () => {
        if (generation !== searchGenerationRef.current || controller.signal.aborted) return;
        // Prefilled catalogue pages (Marvel/DC/Classics) never run live provider lookups.
        // Saved streaming info is hydrated independently; torrent/NexStream info loads
        // only when a card is clicked.
        if (searchMode) return;
        {
        // Process every catalogue group so saved enrichment is available even when the
        // UI reorders results by quality/seeds. Only live lookups remain capped below.
        const availabilityGroups = groupTorrentResults(data).slice(
          (franchiseGridPage - 1) * franchiseGridPageSize,
          franchiseGridPage * franchiseGridPageSize,
        );
        const pendingLiveLookups: typeof availabilityGroups = [];
        // Titles with no provider info are re-checked live (3 at a time, bounded per search),
        // and the backend stores each answer, so every card ends up with streaming info.
        let liveLookupCount = 0;
        const liveLookupLimit = 150;
        const pendingLiveTmdbIds = new Map<string, number>();
        const recheckAfterMs = 6 * 60 * 60 * 1000;

        // The torrent-search cache and enriched OTT cache are separate files.
        // Fetch saved OTT entries by title once, so normal text searches can use
        // Marvel/DC enrichment too instead of starting a provider scan per card.
        const savedOttByTitle = new Map<string, any>();
        try {
          const savedOttResponse = await api.searchSavedOtt(
            availabilityGroups.map(group => group.title),
            controller.signal
          );
          if (generation === searchGenerationRef.current) {
            const savedProviderCount = (candidate: any): number => {
              const saved = candidate?.ottAvailability || candidate || {};
              return ['streamingProviders', 'rentProviders', 'buyProviders'].reduce(
                (total, key) => total + (Array.isArray(saved[key]) ? saved[key].length : 0),
                0,
              );
            };
            for (const result of savedOttResponse.results || []) {
              for (const entry of result.entries || []) {
                if (!entry.ottAvailability) continue;
                // Index by both the canonical saved title and the exact search
                // query. The latter guarantees a hit even when year/title labels differ.
                const keys = Array.from(new Set(
                  [entry.title, result.query]
                    .map(value => normalizeSavedOttTitle(String(value || '')))
                    .filter(Boolean)
                ));
                for (const key of keys) {
                  const existing = savedOttByTitle.get(key);
                  if (!existing || savedProviderCount(entry) > savedProviderCount(existing)) {
                    savedOttByTitle.set(key, entry);
                  }
                }
              }
            }
          }
        } catch {
          // A saved-cache lookup failure must not block torrent preparation.
        }

        for (const group of availabilityGroups) {
          const cachedTorrentRow = group.variants.find(variant => {
            const row = variant as TorrentSearchResult & {
              ottAvailability?: any;
              streamingProviders?: unknown;
              rentProviders?: unknown;
              buyProviders?: unknown;
            };
            const saved = row.ottAvailability && typeof row.ottAvailability === 'object'
              ? row.ottAvailability
              : row;
            return ['streamingProviders', 'rentProviders', 'buyProviders'].some(key =>
              Array.isArray((saved as any)[key]) && (saved as any)[key].length > 0
            ) || ['streamingProviders', 'rentProviders', 'buyProviders'].some(key =>
              Array.isArray((row as any)[key]) && (row as any)[key].length > 0
            );
          }) as (TorrentSearchResult & {
            tmdbId?: number | string;
            ottAvailability?: any;
            streamingProviders?: unknown;
            rentProviders?: unknown;
            buyProviders?: unknown;
            catalogueTitle?: string;
            marvelCatalogueTitle?: string;
          }) | undefined;
          const savedOttRow = savedOttByTitle.get(normalizeSavedOttTitle(group.title));
          const cachedRow = cachedTorrentRow || (savedOttRow as (TorrentSearchResult & {
            tmdbId?: number | string;
            ottAvailability?: any;
            streamingProviders?: unknown;
            rentProviders?: unknown;
            buyProviders?: unknown;
            catalogueTitle?: string;
            marvelCatalogueTitle?: string;
          }) | undefined);

          if (cachedRow) {
            const saved = cachedRow.ottAvailability && typeof cachedRow.ottAvailability === 'object'
              ? cachedRow.ottAvailability
              : cachedRow;
            const region = String(saved.region || 'IN');
            const providers = [
              ...normalizeSavedOttProviders(saved.streamingProviders, 'streaming', region),
              ...normalizeSavedOttProviders(saved.rentProviders, 'rent', region),
              ...normalizeSavedOttProviders(saved.buyProviders, 'buy', region),
            ];
            const title = String(cachedRow.catalogueTitle || cachedRow.marvelCatalogueTitle || cachedRow.mediaTitle || group.title)
              .replace(/\s*\(?(?:19|20)\d{2}\)?\s*$/, '').trim();
            const hasProviders = providers.length > 0;
            setSearchAvailabilityByGroup(previous => ({
              ...previous,
              [group.key]: {
                status: hasProviders ? 'ready' : 'not_confirmed',
                query: title,
                title,
                year: group.year,
                providers,
                matches: hasProviders ? [{
                  id: String(cachedRow.tmdbId || ''),
                  tmdbId: cachedRow.tmdbId,
                  title,
                  year: group.year,
                  type: 'movie',
                  status: 'ready',
                  providers,
                }] : [],
                message: hasProviders ? undefined : (
                  ['no_providers', 'no_streaming_provider_reported'].includes(String(saved.status || '').toLowerCase())
                    ? 'No providers were reported in the saved enrichment.'
                    : 'Saved enrichment has no provider names.'
                ),
              }
            }));
            if (hasProviders) continue;
            // No provider names saved: re-check live unless the saved miss is recent.
            const checkedMs = Date.parse(String(saved.checkedAt || ''));
            if (!saved.incomplete && Number.isFinite(checkedMs) && Date.now() - checkedMs < recheckAfterMs) continue;
            if (liveLookupCount >= liveLookupLimit) continue;
            liveLookupCount += 1;
            const savedTmdbId = Number(cachedRow.tmdbId);
            if (Number.isInteger(savedTmdbId) && savedTmdbId > 0) pendingLiveTmdbIds.set(group.key, savedTmdbId);
            pendingLiveLookups.push(group);
            continue;
          }

          // Keep live searches bounded so this path cannot fan out
          // provider queries for an unlimited result set.
          if (liveLookupCount >= liveLookupLimit) continue;
          liveLookupCount += 1;
          setSearchAvailabilityByGroup(previous => ({
            ...previous,
            [group.key]: { status: 'loading', query: group.title, providers: [] }
          }));
          pendingLiveLookups.push(group);
        }

        const runLiveLookup = async (group: typeof availabilityGroups[number]) => {
          const tmdbId = pendingLiveTmdbIds.get(group.key);
          const lookup = async () => {
            if (tmdbId) {
              const value = await api.getMovieOttAvailability(tmdbId, 'IN');
              const region = String(value.region || 'IN');
              const providers = [
                ...normalizeSavedOttProviders(value.streamingProviders, 'streaming', region),
                ...normalizeSavedOttProviders(value.rentProviders, 'rent', region),
                ...normalizeSavedOttProviders(value.buyProviders, 'buy', region),
              ];
              const title = group.title.replace(/\s*\(?(?:19|20)\d{2}\)?\s*$/, '').trim();
              return {
                status: providers.length ? 'ready' : 'not_confirmed',
                query: title,
                title,
                year: group.year,
                providers,
                matches: providers.length ? [{
                  id: String(tmdbId), tmdbId, title, year: group.year, type: 'movie', status: 'ready', providers,
                }] : [],
                message: providers.length ? undefined : 'No streaming providers were found when re-checked.',
              } as any;
            }
            const providerLookupTitle = episodeSeriesTitle(group.title) || group.title;
            const availabilityQuery = providerLookupTitle
              .replace(/\s+\(\d{4}\)\s*$/, '')
              .trim();
            return await api.searchAvailability(availabilityQuery);
          };
          for (let attempt = 0; attempt < 2; attempt += 1) {
            if (generation !== searchGenerationRef.current) return;
            try {
              const value = await lookup();
              if (generation === searchGenerationRef.current) {
                setSearchAvailabilityByGroup(previous => {
                  // A re-check that finds nothing must not erase saved info.
                  const existing = previous[group.key] as any;
                  if (existing?.providers?.length && !(value as any)?.providers?.length) return previous;
                  return { ...previous, [group.key]: value };
                });
              }
              return;
            } catch {
              if (attempt === 0) await new Promise<void>(resolve => window.setTimeout(resolve, 8000));
            }
          }
          if (generation === searchGenerationRef.current) {
            setSearchAvailabilityByGroup(previous => {
              const existing = previous[group.key] as any;
              // Keep a saved "no details" card as is; only a pending lookup becomes unavailable.
              if (existing && existing.status !== 'loading') return previous;
              return { ...previous, [group.key]: { status: 'unavailable', query: group.title, providers: [] } };
            });
          }
        };
        const liveQueue = [...pendingLiveLookups];
        void Promise.allSettled(Array.from({ length: Math.min(3, liveQueue.length) }, async () => {
          while (liveQueue.length > 0 && generation === searchGenerationRef.current) {
            const group = liveQueue.shift();
            if (group) await runLiveLookup(group);
          }
        }));
        }
      })();

      if (data.length === 0) {
        setError('No matching torrent results were found.');
      }
    } catch (err: any) {
      if (generation !== searchGenerationRef.current) return;
      if (err?.name === 'AbortError') return;
      setResults([]);
      setSearched(true);
      setCatalogueBuildProgress('');
      setError(err?.message || 'Torrent search failed.');
    } finally {
      if (generation === searchGenerationRef.current) {
        setIsSearching(false);
        if (searchRequestRef.current === controller) {
          searchRequestRef.current = null;
        }
      }
    }
  };

  const searchMovieTorrents = async (movie: MovieCatalogueItem) => {
    // Torrent lookup and Prepare state are user-initiated only. Background cache/metadata
    // work must never be able to open a Prepare card.
    if (!prepareClickIntentRef.current || !prepareTraceIdRef.current || prepareBuildPendingRef.current) {
      console.warn('[PREPARE_TRACE]', JSON.stringify({
        event: 'blocked-prepare-search',
        title: movie.title,
        year: movie.year || null,
        reason: 'missing-explicit-click-intent',
        at: new Date().toISOString(),
      }));
      return;
    }
    const traceId = prepareTraceIdRef.current;
    const previewIdentity = catalogueMovieIdentity(movie, movieCatalogue?.catalogue);
    prepareBuildPendingRef.current = true;
    prepareClickIntentRef.current = false;

    console.info('[PREPARE_TRACE]', JSON.stringify({
      event: 'prepare-search-start',
      traceId,
      title: movie.title,
      year: movie.year || null,
      at: new Date().toISOString(),
    }));
    setExpandedSearchGroupKey(null);
    const titleQuery = movie.title + (movie.year ? ' ' + movie.year : '');
    movieTorrentSearchControllerRef.current?.abort();
    const controller = new AbortController();
    movieTorrentSearchControllerRef.current = controller;
    const generation = ++movieTorrentSearchGenerationRef.current;

    setMovieTorrentSearchTitle(titleQuery);
    setMovieTorrentPreviewCatalogueId(previewIdentity);
    setMovieTorrentPreviewPosterUrl(
      cataloguePosterUrlsRef.current[String(movie.id)] || cataloguePosterSource(movie)
    );
    setMovieTorrentSearchLoading(true);
    setMovieTorrentSearchError('');
    setMovieTorrentPreviewGroup(null);
    setMovieTorrentPickerGroups(null);
    setSelectedMovieTorrentQualityByGroup({});
    setResults([]);
    setSearched(false);
    setQuery(titleQuery);
    setSelectedQualityByGroup({});
    prepareBuildPendingRef.current = false;

    const cacheKey = normalizeGroupTitle(movie.title) + '|' + String(movie.year || '');
    const cachedDetails = movieTorrentDetailsCacheRef.current.get(cacheKey);
    const persistedDetails = cachedDetails || readLocalCache<{ groups: TorrentQualityGroup[]; savedAt: number }>(MOVIE_TORRENT_CACHE_STORAGE_KEY, cacheKey);
    if (persistedDetails && Date.now() - persistedDetails.savedAt < MOVIE_TORRENT_CACHE_TTL_MS) {
      movieTorrentDetailsCacheRef.current.set(cacheKey, persistedDetails);
      const groups = persistedDetails.groups.map(group => ({
        ...group,
        posterResult: {
          ...group.posterResult,
          posterUrl: group.posterResult?.posterUrl || movie.posterUrl || null,
          mediaTitle: group.posterResult?.mediaTitle || movie.title,
          tmdbId: group.posterResult?.tmdbId || movie.tmdbId || movie.id,
          id: group.posterResult?.id || movie.tmdbId || movie.id,
          year: group.posterResult?.year || movie.year || group.year,
        },
      }));
      if (groups.length > 0) {
        const expectedTitle = normalizeGroupTitle(movie.title);
        const expectedYear = String(movie.year || '');
        const selectedGroup = [...groups].sort((a, b) => {
          const score = (group: TorrentQualityGroup) => {
            const title = normalizeGroupTitle(group.title.replace(/\s*\((?:19|20)\d{2}\)\s*$/, ''));
            let value = title === expectedTitle ? 100 : title.includes(expectedTitle) ? 20 : 0;
            if (expectedYear && torrentGroupReleaseYear(group) === Number(expectedYear)) value += 25;
            if (/\b(?:making of|behind the scenes|soundtrack|interview|trailer)\b/i.test(group.title)) value -= 100;
            return value;
          };
          return score(b) - score(a);
        })[0];
        console.info('[PREPARE_TRACE]', JSON.stringify({
          event: 'prepare-card-open',
          traceId,
          source: 'prebuilt-cache',
          title: movie.title,
          year: movie.year || null,
          variants: selectedGroup.variants.length,
          at: new Date().toISOString(),
        }));
        setSelectedMovieTorrentQualityByGroup({ [selectedGroup.key]: torrentResultKey(selectedGroup.variants[0]) });
        setMovieTorrentPreviewGroup(selectedGroup);
        setMovieTorrentPreviewCatalogueId(previewIdentity);
        if (!/^(popular|trending|latest)-(bollywood|hollywood)$/.test(String(movieCatalogue?.catalogue || ''))) {
          startPosterBackgroundSearch(selectedGroup.variants);
        }
      } else {
        setMovieTorrentSearchError('No usable torrent releases were found for ' + titleQuery + '. Try again later or use a shorter title.');
      }
      setMovieTorrentSearchLoading(false);
      return;
    }

    try {
      const embeddedMatches = [
        ...(Array.isArray(movie.torrentMatches) ? movie.torrentMatches : []),
        ...(movie.torrentMatch ? [movie.torrentMatch] : []),
      ];
      let data: TorrentSearchResult[] | null = embeddedMatches
        .filter(match => Boolean(match && (match.magnetUrl || match.infoHash || match.infoUrl)))
        .map((match, index): TorrentSearchResult => ({
          guid: String(match.infoHash || match.magnetUrl || match.infoUrl || movie.title + '-' + index),
          title: String(match.title || movie.title),
          mediaTitle: movie.title,
          size: Number(match.size) || 0,
          seeders: Number(match.seeders) || 0,
          leechers: Number(match.leechers) || 0,
          indexer: match.indexer,
          infoHash: match.infoHash,
          magnetUrl: match.magnetUrl,
          infoUrl: match.infoUrl,
          sourceUrl: match.infoUrl,
          year: movie.year,
          posterUrl: movie.posterUrl,
          quality: '',
        }));
      if (data.length === 0) data = null;

      // Prefer the movie's bundled torrent matches; query the shared VPS snapshot
      // only when this catalogue row does not already include a release.
      if (!data) {
        try {
          const params = new URLSearchParams({ title: movie.title, year: String(movie.year || '') });
          console.info('[PREPARE_TRACE]', JSON.stringify({
            event: 'prebuilt-request',
            traceId,
            title: movie.title,
            year: movie.year || null,
            at: new Date().toISOString(),
          }));
          const response = await fetch(API_BASE + '/api/regional-movies/prebuilt?' + params.toString(), {
            signal: controller.signal,
            credentials: 'include',
            cache: 'no-store',
            headers: {
              'X-Prepare-Intent': 'explicit-card-click',
              'X-Prepare-Trace': traceId,
            },
          });
          console.info('[PREPARE_TRACE]', JSON.stringify({
            event: 'prebuilt-response',
            traceId,
            status: response.status,
            ok: response.ok,
            at: new Date().toISOString(),
          }));
          if (response.ok) {
            const cached = await response.json();
            const rows = cached?.torrentDetails?.results;
            if (cached?.status === 'ready' && Array.isArray(rows) && rows.length > 0) data = rows as TorrentSearchResult[];
          }
        } catch { /* The bundled catalogue details remain the fallback. */ }
      }
      if (generation !== movieTorrentSearchGenerationRef.current) return;
      // Prefilled catalogues are cache-only. A missing VPS/GitHub-seeded
      // cache entry must never trigger a live provider/indexer search, even
      // after an explicit Get click. Cache refreshes happen in the separate
      // VPS-to-GitHub workflow, not in the user's request path.
      if (!data) data = [];
      if (generation !== movieTorrentSearchGenerationRef.current) return;

      // Prefer active swarms, but don't let missing seeder metadata hide every
      // release. The regular search can display results from indexers that do
      // not report seed counts reliably.
      const minSeedrFriendlySize = searchLimits.minSizeMb * 1024 * 1024;
      const maxSeedrFriendlySize = searchLimits.maxSizeGb * 1024 * 1024 * 1024;
      const lowQualityRelease = /(?:^|[\s._()[\]-])(?:cam(?:rip)?|hdcam|hd[ ._-]?cam|telesync|tele[ ._-]?sync|ts[ ._-]?(?:md|ac3|hd)?|telecine|dvdscr|dvd[ ._-]?scr|screener|workprint)(?:$|[\s._()[\]-])/i;
      // Indexers often return loose keyword matches. Keep a result only when the
      // actual release title contains the selected movie title as a phrase, so a
      // movie called "Only Me" won't show unrelated releases containing "Only"
      // and "Me" far apart in their titles.
      const expectedTitle = normalizeGroupTitle(movie.title);
      const groupUsableResults = (items: TorrentSearchResult[], requireActiveSeeders: boolean) => {
        const usable = items.filter(result => {
          const releaseTitle = normalizeGroupTitle(String(result.title || ''));
          const mediaTitle = normalizeGroupTitle(String(result.mediaTitle || ''));
          const titleMatches = Boolean(expectedTitle) &&
            (releaseTitle.includes(expectedTitle) || mediaTitle === expectedTitle || mediaTitle.includes(expectedTitle));
          if (!titleMatches) return false;
          const rawSize = Number(result.size);
          const size = Number.isFinite(rawSize) && rawSize > 0 ? rawSize : 0;
          const seeders = Number(result.seeders);
          const hasSource = Boolean(
            String(result.magnetUrl || '').trim() ||
            String(result.downloadUrl || '').trim() ||
            String(result.sourceUrl || '').trim()
          );
          if (!hasSource || size < minSeedrFriendlySize || size > maxSeedrFriendlySize) return false;
          if (requireActiveSeeders && (!Number.isFinite(seeders) || seeders <= 0)) return false;
          return !lowQualityRelease.test(String(result.title || '') + ' ' + String(result.quality || ''));
        });
        return groupTorrentResults(usable);
      };

      let groups = groupUsableResults(data, true);

      // Year tags aren't always present in torrent filenames. When the strict
      // title+year search yields nothing, retry the movie's plain title before
      // concluding that the provider has no releases.
      const titleOnlyQuery = String(movie.title || '').trim();
      // Do not retry a cache miss with a title-only live search. This UI is
      // deliberately cache-only; refreshes belong to the external cache builder.

      // Some indexers omit seeder counts. If no actively seeded releases
      // survived, show linked, non-CAM releases within the size limit rather
      // than reporting a false "no releases" failure.
      if (groups.length === 0) {
        groups = groupUsableResults(data, false);
      }

      // Cache the complete grouped release details (quality variants, sources,
      // seed/leech counts and file sizes) so a later Prepare-card open is instant.
      // Cache empty results briefly too, avoiding repeated indexer scans for titles
      // that currently have no usable releases.
      movieTorrentDetailsCacheRef.current.set(cacheKey, { groups, savedAt: Date.now() });
      persistMovieTorrentCache();
      if (movieTorrentDetailsCacheRef.current.size > 250) {
        const oldestKey = movieTorrentDetailsCacheRef.current.keys().next().value;
        if (oldestKey) movieTorrentDetailsCacheRef.current.delete(oldestKey);
      }

      if (groups.length === 0) {
        if (movieCatalogue?.catalogue === 'popular-bollywood') {
          setMovieTorrentSearchError('Coming soon on OTT — this title is not available to prepare right now. Check back later for a streaming release.');
        } else {
          setMovieTorrentSearchError('No usable torrent releases were found for ' + titleQuery + '. Try again later or use a shorter title.');
        }
        return;
      }

      // Regional catalogue posters are authoritative; avoid separate torrent-title poster lookups.
      const isRegionalMovieCatalogue = /^(popular|trending|latest)-(bollywood|hollywood)$/.test(String(movieCatalogue?.catalogue || ''));
      if (!isRegionalMovieCatalogue) {
        startPosterBackgroundSearch(groups.flatMap(group => group.variants));
      }
      if (groups.length > 0) {
        // Open the normal Prepare card immediately instead of showing a
        // separate release-selection modal. Prefer an exact movie title/year
        // match so extras such as "The Making of..." are not selected first.
        const catalogueGroups = groups.map(group => ({
          ...group,
          posterResult: {
            ...group.posterResult,
            posterUrl: group.posterResult?.posterUrl || movie.posterUrl || null,
            mediaTitle: group.posterResult?.mediaTitle || movie.title,
            tmdbId: group.posterResult?.tmdbId || movie.tmdbId || movie.id,
            id: group.posterResult?.id || movie.tmdbId || movie.id,
            year: group.posterResult?.year || movie.year || group.year,
          },
        }));
        const expectedTitle = normalizeGroupTitle(movie.title);
        const expectedYear = String(movie.year || '');
        const groupScore = (group: TorrentQualityGroup) => {
          const title = normalizeGroupTitle(group.title.replace(/\s*\((?:19|20)\d{2}\)\s*$/, ''));
          let score = title === expectedTitle ? 100 : title.includes(expectedTitle) ? 20 : 0;
          if (expectedYear && torrentGroupReleaseYear(group) === Number(expectedYear)) score += 25;
          if (/\b(?:making of|behind the scenes|soundtrack|interview|trailer)\b/i.test(group.title)) score -= 100;
          return score;
        };
        const group = [...catalogueGroups].sort((a, b) => groupScore(b) - groupScore(a))[0];
        console.info('[PREPARE_TRACE]', JSON.stringify({
          event: 'prepare-card-open',
          traceId,
          source: 'live-torrent-search',
          title: movie.title,
          year: movie.year || null,
          variants: group.variants.length,
          at: new Date().toISOString(),
        }));
        setSelectedMovieTorrentQualityByGroup({ [group.key]: torrentResultKey(group.variants[0]) });
        setMovieTorrentPreviewGroup(group);
        setMovieTorrentPreviewCatalogueId(previewIdentity);
        setMovieTorrentPickerGroups(null);
      }
    } catch (error: any) {
      if (generation !== movieTorrentSearchGenerationRef.current || error?.name === 'AbortError') return;
      setMovieTorrentSearchError(String(error?.message || 'Torrent search failed for ' + titleQuery + '.'));
    } finally {
      if (generation === movieTorrentSearchGenerationRef.current) {
        setMovieTorrentSearchLoading(false);
        if (movieTorrentSearchControllerRef.current === controller) {
          movieTorrentSearchControllerRef.current = null;
        }
      }
    }
  };

  const selectMovieTorrentGroup = (group: TorrentQualityGroup) => {
    setSelectedMovieTorrentQualityByGroup({ [group.key]: torrentResultKey(group.variants[0]) });
    setMovieTorrentPreviewGroup(group);
    setMovieTorrentPreviewCatalogueId(null);
    setMovieTorrentPickerGroups(null);
    setMovieTorrentSearchError('');
    if (!/^(popular|trending|latest)-(bollywood|hollywood)$/.test(String(movieCatalogue?.catalogue || ''))) {
      startPosterBackgroundSearch(group.variants);
    }
  };

  const prepareMovieTorrent = async (result: TorrentSearchResult) => {
    const source = result.magnetUrl || result.downloadUrl || result.sourceUrl;
    const torrentKey = result.infoHash || source || result.title;
    if (!source || preparingTorrentKey === torrentKey) return;

    setPrepareError('');
    setMovieTorrentSearchError('');
    setPreparingTorrentKey(torrentKey);
    setPrepareWaitTitle(result.title);
    setPrepareWaitOpen(false);
    const longWaitTimer = window.setTimeout(() => setPrepareWaitOpen(true), 30000);

    try {
      const metadata = metadataCacheRef.current.get(torrentKey);
      const prepared = await onPrepare(result, metadata);
      const deletedFolderIds = new Set(
        (prepared?.deletedFolderIds || []).map(id => String(id).trim()).filter(Boolean)
      );
      if (deletedFolderIds.size > 0) {
        for (const [key, entry] of preparedByKeyRef.current.entries()) {
          if (entry.files.some(file => deletedFolderIds.has(String(file.folderId)))) {
            preparedByKeyRef.current.delete(key);
          }
        }
      }
      if (prepared?.files?.length) {
        preparedByKeyRef.current.set(torrentKey, { files: prepared.files });
      }
      setPrepareWaitOpen(false);
    } catch (error: any) {
      const message = String(error?.message || 'Could not prepare this torrent.');
      if (!message.toLowerCase().includes('cancelled by another selection')) {
        setPrepareError(message);
      }
    } finally {
      window.clearTimeout(longWaitTimer);
      setPreparingTorrentKey(current => current === torrentKey ? null : current);
    }
  };

  // Everything the shared movie cards need to know about one torrent result.
  // Used by every movie section so Prepare / Play / Download / Copy behave the same.
  const prepareStateFor = (result: TorrentSearchResult) => {
    const source = result.magnetUrl || result.downloadUrl || result.sourceUrl;
    const torrentKey = result.infoHash || source || result.title;
    const preparedFiles = preparedForResult(result);
    const primaryFile = preparedFiles.find(file => PLAYABLE_FILE_PATTERN.test(file.name)) || preparedFiles[0];
    return { source, torrentKey, isPreparing: preparingTorrentKey === torrentKey, primaryFile };
  };

  const preparedActionsFor = (torrentKey: string, primaryFile: SeedrSearchFile) => ({
    isPlaying: playingTorrentKey === torrentKey,
    isCopied: copiedTorrentKey === torrentKey,
    onPlay: async () => {
      if (playingTorrentKey === torrentKey) return;
      setPlayingTorrentKey(torrentKey);
      try {
        await onPlaySeedrFile?.(primaryFile);
      } finally {
        setPlayingTorrentKey(current => current === torrentKey ? null : current);
      }
    },
    onDownload: () => api.openSeedrFileDownload(primaryFile.id, primaryFile.name),
    onCopy: async () => {
      try {
        const data = await api.getSeedrFileDownload(primaryFile.id);
        await navigator.clipboard.writeText(data.url);
        setCopiedTorrentKey(torrentKey);
        window.setTimeout(() => {
          setCopiedTorrentKey(current => current === torrentKey ? null : current);
        }, 2000);
      } catch {
        setPrepareError('Could not copy the download link.');
      }
    },
  });

  // Returns a selected catalogue card from its prepare card back to the compact card.
  const closeMoviePrepareCard = () => {
    movieTorrentSearchControllerRef.current?.abort();
    movieTorrentSearchGenerationRef.current += 1;
    setMovieTorrentSearchLoading(false);
    setMovieTorrentPreviewGroup(null);
    setMovieTorrentPreviewCatalogueId(null);
    setMovieTorrentPreviewPosterUrl('');
    setMovieTorrentSearchError('');
  };

  const loadDcWebSeries = async () => {
    setShowDcWebSeries(true);
    setShowMarvelWebSeries(false);
    if (dcWebSeriesCatalogue || dcWebSeriesLoading) return;
    setDcWebSeriesLoading(true);
    setDcWebSeriesError('');
    try {
      setDcWebSeriesCatalogue(await api.getTvCatalogue('dc-live-action'));
    } catch (error: any) {
      setDcWebSeriesError(String(error?.message || 'Could not load DC web series.'));
    } finally {
      setDcWebSeriesLoading(false);
    }
  };

  const loadClassicsTvCatalogue = async (catalogueKey: 'classics-web-series' | 'classics-anime', tab: 'web-series' | 'anime') => {
    setClassicsContentTab(tab);
    setShowDcWebSeries(false);
    setShowMarvelWebSeries(true);
    if (marvelWebSeriesCatalogue?.catalogue === catalogueKey) return;
    setMarvelWebSeriesCatalogue(null);
    setMarvelWebSeriesLoading(true);
    setMarvelWebSeriesError('');
    try {
      setMarvelWebSeriesCatalogue(await api.getTvCatalogue(catalogueKey));
    } catch (error: any) {
      setMarvelWebSeriesError(String(error?.message || 'Could not load Classics catalogue.'));
    } finally {
      setMarvelWebSeriesLoading(false);
    }
  };

  const loadMarvelWebSeries = async () => {
    setShowMarvelWebSeries(true);
    if (marvelWebSeriesCatalogue?.catalogue === 'marvel-web-series' || marvelWebSeriesLoading) return;
    setMarvelWebSeriesLoading(true);
    setMarvelWebSeriesError('');
    try {
      // Fetch every page up front so the Marvel section is complete rather than
      // silently showing only the first page until the user finds Load more.
      const firstPage = await api.getTvCatalogue('marvel', 1);
      const allResults = [...firstPage.results];
      for (let page = 2; page <= Math.min(firstPage.totalPages, 500); page += 1) {
        const nextPage = await api.getTvCatalogue('marvel', page);
        for (const show of nextPage.results) {
          if (!allResults.some(existing => String(existing.id) === String(show.id))) allResults.push(show);
        }
      }
      setMarvelWebSeriesCatalogue({ ...firstPage, page: firstPage.totalPages, results: allResults });
    } catch (error: any) {
      setMarvelWebSeriesError(String(error?.message || 'Could not load Marvel web series.'));
    } finally {
      setMarvelWebSeriesLoading(false);
    }
  };

  const openMovieCatalogue = async (
    key: MovieCatalogueKey,
    searchText: string,
    page = 1,
    onlyReleased = releasedOnly,
  ) => {
    setHighQualityPostersReady(false);
    setFranchiseGridPage(1);
    setActiveFranchiseSearchMode(null);
    franchisePageRequestRef.current?.abort();
    franchisePageRequestRef.current = null;
    franchisePageGenerationRef.current += 1;
    posterFirstPageReadyRef.current = false;
    setPosterFirstPageReady(false);
    movieTorrentSearchControllerRef.current?.abort();
    movieTorrentSearchGenerationRef.current += 1;
    setMovieTorrentSearchLoading(false);
    setMovieTorrentPickerGroups(null);
    setMovieTorrentPreviewGroup(null);
    setMovieTorrentPreviewCatalogueId(null);
    setMovieTorrentSearchError('');
    const generation = ++searchGenerationRef.current;
    searchRequestRef.current?.abort();
    const controller = new AbortController();
    searchRequestRef.current = controller;

    try {
      setIsPresetCatalogueAction(true);
      setIsSearching(true);
      setCatalogueBuildProgress('Loading the ' + searchText.replace(/Movies?$/i, '').trim() + ' movie catalogue…');
      setMovieCatalogueSearchText(searchText);
      setShowRecentSearches(false);
      setQuery('');
      setError('');
      setResults([]);
      setSelectedQualityByGroup({});
      setExpandedSearchGroupKey(null);
      setSearched(false);
      setPosterOverrides({});
      posterMetadataFailedRef.current.clear();
      posterLoadedRef.current.clear();
      posterBackgroundQueueRef.current = [];
      posterBackgroundQueuedRef.current.clear();
      posterBackgroundAttemptsRef.current.clear();
      posterBackgroundGenerationRef.current += 1;

      if (page > 3) {
        // Deep pages paint quickly with source metadata and lightweight posters.
        // The full eligibility/provider/details response replaces this in the background.
        const basic = await api.getMovieCatalogue(key, page, onlyReleased, controller.signal, 5, true);
        if (generation !== searchGenerationRef.current) return;
        const basicPage = {
          ...basic,
          results: (basic.results || []).map(movie => ({
            ...movie,
            posterUrl: lowQualityPosterUrl(movie.posterUrl || '', 'w185'),
          })),
        };
        setMovieCatalogue(basicPage);
        setCatalogueBuildProgress('');
        void api.getMovieCatalogue(key, page, onlyReleased, undefined, 5, false).then(fullPage => {
          if (generation !== searchGenerationRef.current) return;
          setMovieCatalogue(fullPage);
        }).catch(error => {
          if (generation === searchGenerationRef.current) {
            setError(String(error?.message || 'Could not load full catalogue details.'));
          }
        });
        return;
      }
      const data = await api.getMovieCatalogue(key, page, onlyReleased, controller.signal);
      if (generation !== searchGenerationRef.current) return;
      setMovieCatalogue(data);
      setCatalogueBuildProgress('');
    } catch (err: any) {
      if (generation !== searchGenerationRef.current) return;
      if (err?.name === 'AbortError') return;

      const message = String(err?.message || 'Movie catalogue failed.');
      setMovieCatalogue(null);
      setError(message);
    } finally {
      if (generation === searchGenerationRef.current) {
        setIsSearching(false);
        setCatalogueBuildProgress('');
        if (searchRequestRef.current === controller) {
          searchRequestRef.current = null;
        }
      }
    }
  };

  async function loadCachedFranchisePage(mode: CachedFranchiseSearchMode, page: number): Promise<void> {
    franchisePageRequestRef.current?.abort();
    franchisePageRequestRef.current = null;
    franchisePageGenerationRef.current += 1;
    const loaded = franchiseLoadedPagesRef.current.get(mode);
    if (loaded?.has(page)) {
      setIsSearching(false);
      setCatalogueBuildProgress('');
      return;
    }
    const browserCachedPage = readFranchisePageCache(mode, page);
    if (browserCachedPage) {
      setResults(previous => {
        const byKey = new Map<string, TorrentSearchResult>();
        [...previous, ...browserCachedPage.results].forEach(row => {
          const key = torrentResultKey(row);
          if (!byKey.has(key)) byKey.set(key, row);
        });
        return Array.from(byKey.values());
      });
      const nowLoaded = loaded || new Set<number>();
      nowLoaded.add(page);
      franchiseLoadedPagesRef.current.set(mode, nowLoaded);
      if (browserCachedPage.totalMovies > 0) setCatalogueExpectedTitles(browserCachedPage.totalMovies);
      setSearched(true);
      setError('');
      setCatalogueBuildProgress('');
      setIsSearching(false);
      return;
    }
    const controller = new AbortController();
    franchisePageRequestRef.current = controller;
    const generation = ++franchisePageGenerationRef.current;
    setIsSearching(true);
    setError('');
    setCatalogueBuildProgress(`Loading page ${page} of the ${mode.replace(/-/g, ' ')} catalogue…`);
    try {
      const fetchPage = () => mode === 'marvel'
        ? api.getMarvelCatalogue(controller.signal, { page, pageSize: franchiseGridPageSize })
        : api.getCatalogue(mode, controller.signal, { page, pageSize: franchiseGridPageSize });
      let catalogue = await fetchPage();
      let attempts = 0;
      while ((catalogue.status === 'building' || catalogue.status === 'idle') && attempts < 8) {
        if (generation !== franchisePageGenerationRef.current || controller.signal.aborted) return;
        setCatalogueBuildProgress(
          'Building shared ' + mode.replace(/-/g, ' ') + ' catalogue: ' +
          catalogue.completed + ' of ' + catalogue.total + ' searches completed.'
        );
        await new Promise<void>(resolve => window.setTimeout(resolve, 2200));
        attempts += 1;
        catalogue = await fetchPage();
      }
      if (generation !== franchisePageGenerationRef.current || controller.signal.aborted) return;
      if (catalogue.status !== 'ready' || !Array.isArray(catalogue.results)) {
        throw new Error(catalogue.error || ('Could not load page ' + page + ' of the catalogue.'));
      }
      const pageRows = catalogue.results;
      setResults(previous => {
        const byKey = new Map<string, TorrentSearchResult>();
        [...previous, ...pageRows].forEach(row => {
          const key = torrentResultKey(row);
          if (!byKey.has(key)) byKey.set(key, row);
        });
        return Array.from(byKey.values());
      });
      const nowLoaded = franchiseLoadedPagesRef.current.get(mode) || new Set<number>();
      nowLoaded.add(page);
      franchiseLoadedPagesRef.current.set(mode, nowLoaded);
      const totalTitles = Number(catalogue.totalMovies) || Number(catalogue.total) || 0;
      const totalPages = Number(catalogue.totalPages) || Math.max(1, Math.ceil(totalTitles / franchiseGridPageSize));
      writeFranchisePageCache(mode, page, pageRows, totalTitles, totalPages);
      if (totalTitles > 0) setCatalogueExpectedTitles(totalTitles);
      setSearched(true);
      setCatalogueBuildProgress(
        catalogue.refreshing
          ? 'Showing cached catalogue results; a refreshed page will appear on a later visit.'
          : ''
      );
    } catch (error: any) {
      if (controller.signal.aborted || generation !== franchisePageGenerationRef.current) return;
      setError(String(error?.message || 'Could not load this catalogue page.'));
      setCatalogueBuildProgress('');
    } finally {
      if (generation === franchisePageGenerationRef.current) {
        setIsSearching(false);
        if (franchisePageRequestRef.current === controller) franchisePageRequestRef.current = null;
      }
    }
  }

  function navigateFranchisePage(page: number): void {
    setHighQualityPostersReady(false);
    setFranchiseGridPage(page);
    if (activeFranchiseSearchMode) void loadCachedFranchisePage(activeFranchiseSearchMode, page);
  }

  const sortedResults = useMemo(() => {
    // Use the shared root search_limits.json settings. Keep both bounds local
    // to this memo: the manual-search handler has its own separate scope.
    const minSeedrFriendlySize = searchLimits.minSizeMb * 1024 * 1024;
    const maxSeedrFriendlySize = searchLimits.maxSizeGb * 1024 * 1024 * 1024;
    // Exclude theatrical/unfinished releases regardless of provider. Keep the
    // tokens bounded so words such as "timestamp" are not mistaken for TS.
    const lowQualityRelease = /(?:^|[\s._()[\]-])(?:cam(?:rip)?|hdcam|hd[ ._-]?cam|telesync|tele[ ._-]?sync|ts[ ._-]?(?:md|ac3|hd)?|telecine|dvdscr|dvd[ ._-]?scr|screener|workprint)(?:$|[\s._()[\]-])/i;
    const sorted = results.filter(result => {
      const size = Number(result.size) || 0;
      const seeders = Number(result.seeders);
      if (!Number.isFinite(seeders) || seeders <= 0) return false;
      if (size < minSeedrFriendlySize || size > maxSeedrFriendlySize) return false;
      const releaseText = `${String(result.title || '')} ${String(result.quality || '')}`;
      const qualityText = `${String(result.quality || '')} ${String(result.title || '')}`;
      if (/(?:^|[^a-z0-9])3[ ._-]*d(?:$|[^a-z0-9])/i.test(qualityText)) return false;
      if (lowQualityRelease.test(releaseText)) return false;
      if (resolutionFilter) {
        const quality = torrentQualityDetails(result).label;
        const pattern = resolutionFilter === '720p' ? /(?:^|[^0-9])720p(?:[^0-9]|$)/i : /(?:^|[^0-9])1080p(?:[^0-9]|$)/i;
        if (!pattern.test(quality)) return false;
      }
      return true;
    });
    sorted.sort((a, b) => {
      const aSize = Number(a.size) || 0;
      const bSize = Number(b.size) || 0;
      const aTime = a.publishDate ? new Date(a.publishDate).getTime() : 0;
      const bTime = b.publishDate ? new Date(b.publishDate).getTime() : 0;
      if (sizeSort) {
        const comparison = sizeSort === 'asc' ? aSize - bSize : bSize - aSize;
        if (comparison !== 0) return comparison;
      }
      if (timeSort) {
        const comparison = timeSort === 'asc' ? aTime - bTime : bTime - aTime;
        if (comparison !== 0) return comparison;
      }
      // Keep the backend's relevance-first ranking by default. Re-sorting here
      // by quality/seeders would undo its title-match scoring and let popular
      // but less relevant releases appear above better matches.
      return 0;
    });
    return sorted;
  }, [results, resolutionFilter, sizeSort, timeSort]);

  const groupedResults = useMemo(() => {
    let groups = groupTorrentResults(sortedResults);

    // The Marvel buttons are shown on torrent-search results as well as
    // catalogue cards, so apply the selected franchise to the actual groups.
    if (isMarvelEnglishResults) {
      groups = groups.filter(group => {
        const rawTitle = String(group.title || '');
        const title = normalizeGroupTitle(rawTitle.replace(/\s*\(?((?:19|20)\d{2})\)?\s*$/, ''));
        const year = Number(group.year || rawTitle.match(/\b(?:19|20)\d{2}\b/)?.[0] || 0);
        return !(year === 1977 && /^(?:the )?amazing spider man$/i.test(title));
      });
    }
    if (isMarvelEnglishResults && !showMarvelWebSeries && marvelFilmFilter !== 'all') {
      const normalize = (value: string) => normalizeGroupTitle(String(value || '').replace(/\s*\(?((?:19|20)\d{2})\)?\s*$/, ''));
      const mcuTitles = new Set([
        'ant man', 'ant man and the wasp', 'ant man and the wasp quantumania',
        'avengers', 'the avengers', 'avengers age of ultron', 'avengers endgame', 'avengers infinity war',
        'black panther', 'black panther wakanda forever', 'black widow', 'captain america brave new world',
        'captain america civil war', 'captain america the first avenger', 'captain america the winter soldier',
        'captain marvel', 'doctor strange', 'doctor strange in the multiverse of madness',
        'deadpool and wolverine', 'eternals', 'guardians of the galaxy', 'guardians of the galaxy vol 2',
        'guardians of the galaxy vol 3', 'iron man', 'iron man 2', 'iron man 3',
        'shang chi and the legend of the ten rings', 'spider man brand new day', 'spider man far from home',
        'spider man homecoming', 'spider man no way home', 'the fantastic four first steps',
        'the incredible hulk', 'the marvels', 'the punisher one last kill', 'the punisher: one last kill', 'thor', 'thor love and thunder', 'thor ragnarok',
        'thor the dark world', 'thunderbolts'
      ]);
      const isSpiderMan = (title: string) => /spider\s*man|spider\s*verse|into the spider|across the spider/i.test(title);
      const isXMen = (title: string) => /x\s*men|wolverine|deadpool|new mutants|dark phoenix/i.test(title);
      const isSony = (title: string) => isSpiderMan(title) || /venom|morbius|kraven|madame web/i.test(title);
      groups = groups.filter(group => {
        const title = normalize(group.title);
        switch (marvelFilmFilter) {
          case 'mcu': return mcuTitles.has(title) || [...mcuTitles].some(item => title.startsWith(item + ' ')) ||
            /^(?:marvel television special presentation )?(?:the )?punisher one last kill$/.test(title);
          case 'x-men': return isXMen(title);
          case 'sony': return isSony(title);
          default: return true;
        }
      });
    }

    // Sort whole movie groups, never individual torrent variants.
    return [...groups].sort((a, b) => {
      if (activeFranchiseSearchMode) {
        const dateOrder = releaseDateSortValue(b.posterResult) - releaseDateSortValue(a.posterResult);
        if (dateOrder !== 0) return dateOrder;
      }
      const yearA = torrentGroupReleaseYear(a);
      const yearB = torrentGroupReleaseYear(b);
      if (!yearA && !yearB) return normalizeGroupTitle(a.title).localeCompare(normalizeGroupTitle(b.title), undefined, { numeric: true });
      if (!yearA) return 1;
      if (!yearB) return -1;
      if (yearA !== yearB) return yearB - yearA;
      return normalizeGroupTitle(a.title).localeCompare(normalizeGroupTitle(b.title), undefined, { numeric: true });
    });
  }, [sortedResults, activeFranchiseSearchMode, isMarvelEnglishResults, showMarvelWebSeries, marvelFilmFilter]);

  // Every cached catalogue page needs its own saved streaming-provider hydration.
  // The initial search only enriches page 1; without this page-scoped pass, later
  // pages render cards before their persisted OTT data is ever attached.
  useEffect(() => {
    if (!activeFranchiseSearchMode) return;
    const start = (franchiseGridPage - 1) * franchiseGridPageSize;
    const pageGroups = groupedResults
      .slice(start, start + franchiseGridPageSize)
      .filter(group => !searchAvailabilityByGroup[group.key]);
    if (!pageGroups.length) return;
    const titles = Array.from(new Set(pageGroups.map(group => String(group.title || '').trim()).filter(Boolean)));
    if (!titles.length) return;

    const controller = new AbortController();
    void api.searchSavedOtt(titles, controller.signal).then(response => {
      if (controller.signal.aborted) return;
      const savedByTitle = new Map<string, any>();
      for (const result of response.results || []) {
        for (const entry of result.entries || []) {
          const keys = [entry.title, result.query]
            .map(value => normalizeSavedOttTitle(String(value || '')))
            .filter(Boolean);
          for (const key of keys) {
            const current = savedByTitle.get(key);
            const count = (value: any) => ['streamingProviders', 'rentProviders', 'buyProviders']
              .reduce((total, field) => total + (Array.isArray(value?.ottAvailability?.[field])
                ? value.ottAvailability[field].length
                : Array.isArray(value?.[field]) ? value[field].length : 0), 0);
            if (!current || count(entry) > count(current)) savedByTitle.set(key, entry);
          }
        }
      }
      setSearchAvailabilityByGroup(previous => {
        const next = { ...previous };
        for (const group of pageGroups) {
          const titleKey = normalizeSavedOttTitle(episodeSeriesTitle(group.title) || group.title);
          const entry = savedByTitle.get(titleKey) || savedByTitle.get(normalizeSavedOttTitle(group.title));
          if (!entry) {
            const hasTmdbId = group.variants.some(variant => {
              const id = Number((variant as TorrentSearchResult & { tmdbId?: number | string }).tmdbId);
              return Number.isInteger(id) && id > 0;
            });
            if (!hasTmdbId) {
              next[group.key] = {
                status: 'not_found',
                query: group.title,
                providers: [],
                message: 'No saved streaming details or TMDB ID for this title.',
              };
            }
            continue;
          }
          const saved = entry.ottAvailability && typeof entry.ottAvailability === 'object' ? entry.ottAvailability : entry;
          const region = String(saved.region || 'IN');
          const providers = uniqueOttProviders([
            ...normalizeSavedOttProviders(saved.streamingProviders, 'streaming', region),
            ...normalizeSavedOttProviders(saved.rentProviders, 'rent', region),
            ...normalizeSavedOttProviders(saved.buyProviders, 'buy', region),
          ]);
          // Preserve richer provider data already visible for this group.
          const existing = next[group.key];
          if ((existing?.providers?.length || 0) > providers.length) continue;
          next[group.key] = {
            status: providers.length ? 'ready' : 'not_confirmed',
            query: group.title,
            title: String(entry.catalogueTitle || entry.marvelCatalogueTitle || entry.title || group.title),
            year: group.year,
            providers,
            matches: providers.length ? [{
              id: String(entry.tmdbId || ''),
              tmdbId: entry.tmdbId,
              title: String(entry.catalogueTitle || entry.marvelCatalogueTitle || entry.title || group.title),
              year: group.year,
              type: 'movie',
              status: 'ready',
              providers,
            }] : [],
            message: providers.length ? undefined : 'No saved streaming providers for this title.',
          };
        }
        return next;
      });
      const groupsNeedingLookup = pageGroups.filter(group => {
        const titleKey = normalizeSavedOttTitle(episodeSeriesTitle(group.title) || group.title);
        const entry = savedByTitle.get(titleKey) || savedByTitle.get(normalizeSavedOttTitle(group.title));
        const hasSavedProviders = entry && [
          'streamingProviders',
          'rentProviders',
          'buyProviders',
        ].some(field => {
          const availability = entry.ottAvailability && typeof entry.ottAvailability === 'object'
            ? entry.ottAvailability
            : entry;
          return Array.isArray(availability[field]) && availability[field].length > 0;
        });
        const hasTmdbId = group.variants.some(variant => {
          const id = Number((variant as TorrentSearchResult & { tmdbId?: number | string }).tmdbId);
          return Number.isInteger(id) && id > 0;
        });
        return !hasSavedProviders && hasTmdbId;
      });

      setSearchAvailabilityByGroup(previous => {
        const next = { ...previous };
        for (const group of groupsNeedingLookup) {
          if ((next[group.key]?.providers.length || 0) === 0) {
            next[group.key] = { status: 'loading', query: group.title, providers: [] };
          }
        }
        return next;
      });

      let nextGroupIndex = 0;
      const lookupWorker = async () => {
        while (!controller.signal.aborted) {
          const group = groupsNeedingLookup[nextGroupIndex++];
          if (!group) return;
          const variantWithId = group.variants.find(variant => {
            const id = Number((variant as TorrentSearchResult & { tmdbId?: number | string }).tmdbId);
            return Number.isInteger(id) && id > 0;
          });
          if (!variantWithId) continue;
          const tmdbId = Number((variantWithId as TorrentSearchResult & { tmdbId?: number | string }).tmdbId);
          try {
            const availability = await api.getMovieOttAvailability(tmdbId, 'IN', controller.signal);
            if (controller.signal.aborted) return;
            const region = String(availability.region || 'IN');
            const providers = uniqueOttProviders([
              ...normalizeSavedOttProviders(availability.streamingProviders, 'streaming', region),
              ...normalizeSavedOttProviders(availability.rentProviders, 'rent', region),
              ...normalizeSavedOttProviders(availability.buyProviders, 'buy', region),
            ].map(provider => ({
              name: provider.name,
              region: provider.region,
              type: provider.type,
              webUrl: provider.webUrl,
            })));
            const title = group.title.replace(/\s*\(?(?:19|20)\d{2}\)?\s*$/, '').trim();
            setSearchAvailabilityByGroup(previous => {
              const existing = previous[group.key];
              if ((existing?.providers.length || 0) > providers.length) return previous;
              return {
                ...previous,
                [group.key]: {
                  status: providers.length ? 'ready' : 'not_confirmed',
                  query: title,
                  title,
                  year: group.year,
                  providers,
                  matches: providers.length ? [{
                    id: String(tmdbId),
                    tmdbId,
                    title,
                    year: group.year,
                    type: 'movie',
                    status: 'ready',
                    providers,
                  }] : [],
                  message: providers.length ? undefined : 'No streaming providers reported.',
                },
              };
            });
          } catch (error) {
            if (controller.signal.aborted) return;
            setSearchAvailabilityByGroup(previous => {
              const existing = previous[group.key];
              if ((existing?.providers.length || 0) > 0) return previous;
              return {
                ...previous,
                [group.key]: {
                  status: 'unavailable',
                  query: group.title,
                  providers: [],
                  message: error instanceof Error ? error.message : 'Streaming availability could not be checked.',
                },
              };
            });
          }
        }
      };
      void Promise.all(
        Array.from({ length: Math.min(3, groupsNeedingLookup.length) }, () => lookupWorker()),
      );
    }).catch(error => {
      if (controller.signal.aborted) return;
      setSearchAvailabilityByGroup(previous => {
        const next = { ...previous };
        for (const group of pageGroups) {
          if ((next[group.key]?.providers.length || 0) > 0) continue;
          next[group.key] = {
            status: 'unavailable',
            query: group.title,
            providers: [],
            message: error instanceof Error ? error.message : 'Saved streaming information could not be loaded.',
          };
        }
        return next;
      });
    });
    return () => controller.abort();
  }, [activeFranchiseSearchMode, franchiseGridPage, groupedResults]);

  // Search cards can come from the prebuilt Marvel/DC/Bollywood/Hollywood
  // catalogues and therefore aren't necessarily related to the text the user
  // typed. Merge their per-title metadata from the persistent VPS JSON rather
  // than relying only on a single broad TMDB search for the original query.
  useEffect(() => {
    // Synopsis/IMDb are cheap cached reads: load them with the initial cards, not after posters.
    const pageStart = (franchiseGridPage - 1) * franchiseGridPageSize;
    const visibleGroups = groupedResults.slice(pageStart, pageStart + franchiseGridPageSize);
    const titles = Array.from(new Set(visibleGroups
      .map(group => {
        if (searchMetadataForGroup(group, searchMetadata)) return '';
        const title = String(group.title || '').trim();
        const year = String(group.year || '').trim();
        // group.title can already include its release year (parsed from the
        // torrent title), so never append the same year twice.
        const alreadyHasYear = /\(?((?:19|20)\d{2})\)?\s*$/.test(title);
        return title ? title + (year && !alreadyHasYear ? ' (' + year + ')' : '') : '';
      })
      .filter(Boolean)));
    if (!titles.length) return;

    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void api.searchMediaMetadataBatch(titles, controller.signal).then(response => {
        if (controller.signal.aborted) return;
        const additions = Array.isArray(response.results) ? response.results : [];
        if (additions.length) {
          setSearchMetadata(previous => mergeSearchMetadataItems(previous, additions));
        }
      }).catch(() => {
        // This is a supplementary local-cache lookup; keep the live TMDB metadata
        // response and torrent results working if a VPS cache file is unavailable.
      });
    }, 250);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [groupedResults, franchiseGridPage, searchMetadata]);

  // Search TMDB by show name for episode-shaped torrent titles. A broad search for
  // the episode name (e.g. "Escape Plan") cannot resolve "The Rookie" or "NCIS: New Orleans".
  useEffect(() => {
    // Synopsis/IMDb are cheap cached reads: load them with the initial cards, not after posters.
    const pageStart = (franchiseGridPage - 1) * franchiseGridPageSize;
    const visibleGroups = groupedResults.slice(pageStart, pageStart + franchiseGridPageSize);
    const discoveredSeriesTitles = Array.from(new Set(visibleGroups
      .map(group => episodeSeriesTitle(group.title))
      .filter((title): title is string => Boolean(title))))
      .slice(0, 12);
    const seriesTitles = discoveredSeriesTitles.filter(title => {
      const key = normalizeSavedOttTitle(title);
      const hasCachedMetadata = searchMetadata.some(item =>
        item.mediaType === 'tv' && normalizeSavedOttTitle(item.title) === key
      );
      if (!key || hasCachedMetadata || tvMetadataRequestedRef.current.has(key)) return false;
      tvMetadataRequestedRef.current.add(key);
      return true;
    });
    if (!seriesTitles.length) return;

    const controller = new AbortController();
    void Promise.all(seriesTitles.map(async title => {
      try {
        const response = await api.searchMediaMetadata(title, controller.signal);
        const normalized = normalizeSavedOttTitle(title);
        return (response.results || []).filter(item =>
          item.mediaType === 'tv' && normalizeSavedOttTitle(item.title) === normalized
        ).slice(0, 1);
      } catch {
        // Optional TV metadata must never block or fail torrent search.
        return [];
      }
    })).then(groups => {
      if (controller.signal.aborted) return;
      const additions = groups.flat();
      if (additions.length) {
        setSearchMetadata(previous => mergeSearchMetadataItems(previous, additions));
      }
    });

    return () => controller.abort();
  }, [groupedResults, franchiseGridPage, searchMetadata]);

  // Staged loading: every new result set starts with low-bandwidth thumbnails.
  // High-quality upgrades remain paused until the visible page's initial details settle.
  // Page-by-page staging restarts low-resolution posters before background upgrades.
  const currentPagePosterKeys = () => new Set(
    groupedResults
      .slice((franchiseGridPage - 1) * franchiseGridPageSize, franchiseGridPage * franchiseGridPageSize)
      .map(group => posterKeyFor(group.posterResult))
  );

  useLayoutEffect(() => {
    // Close high-resolution upgrades before the next page paints. Cancel queued
    // artwork work for the old page so only the newly visible page keeps loading.
    posterFirstPageReadyRef.current = false;
    setPosterFirstPageReady(false);
    setHighQualityPostersReady(false);
    if (previousPosterPageRef.current !== franchiseGridPage) {
      previousPosterPageRef.current = franchiseGridPage;
      posterBackgroundGenerationRef.current += 1;
      posterBackgroundQueueRef.current = [];
      posterBackgroundQueuedRef.current.clear();
      posterBackgroundAttemptsRef.current.clear();
      posterBackgroundActiveKeysRef.current.clear();
    }
    const visibleKeys = currentPagePosterKeys();
    posterBackgroundQueueRef.current = posterBackgroundQueueRef.current.filter(job => visibleKeys.has(job.key));
  }, [movieCatalogue, groupedResults, franchiseGridPage]);

  useEffect(() => {
    // Resolve poster sources only for cards on the page currently being viewed.
    // Page 2+ artwork waits until the user navigates to that page.
    const start = (franchiseGridPage - 1) * franchiseGridPageSize;
    const visibleItems = groupedResults
      .slice(start, start + franchiseGridPageSize)
      .flatMap(group => group.variants);
    if (visibleItems.length > 0) enqueuePosterBackgroundJobs(visibleItems);
  }, [groupedResults, franchiseGridPage]);

  useEffect(() => {
    let posterCheckTimer: number | undefined;
    let cancelled = false;
    const startedAt = Date.now();
    const checkVisiblePosterLoads = () => {
      if (cancelled) return;
      const slots = Array.from(document.querySelectorAll<HTMLElement>('[data-movie-poster-slot="true"]'))
        .filter(slot => slot.isConnected && slot.getClientRects().length > 0);
      const settled = slots.length === 0 || slots.every(slot => {
        const image = slot.querySelector<HTMLImageElement>('img[data-movie-poster-stage="true"]');
        if (!image) return slot.dataset.posterWaiting !== 'true';
        return ['low-loaded', 'high-loaded', 'low-failed'].includes(String(image.dataset.posterQuality || ''));
      });
      // A broken/unreachable poster must not block metadata forever; give the
      // visible thumbnails up to eight seconds before continuing with fallbacks.
      if (settled || Date.now() - startedAt >= 8000) {
        posterFirstPageReadyRef.current = true;
        setPosterFirstPageReady(true);
        return;
      }
      posterCheckTimer = window.setTimeout(checkVisiblePosterLoads, 80);
    };
    posterCheckTimer = window.setTimeout(checkVisiblePosterLoads, 0);
    return () => {
      cancelled = true;
      if (posterCheckTimer !== undefined) window.clearTimeout(posterCheckTimer);
    };
  }, [movieCatalogue, groupedResults, franchiseGridPage]);

  useEffect(() => {
    if (!pendingPosterAction) return;
    if (pendingPosterAction.kind === 'catalogue') {
      if (
        !movieCatalogue ||
        movieCatalogue.catalogue !== pendingPosterAction.catalogue ||
        movieCatalogue.page !== pendingPosterAction.page
      ) {
        setPendingPosterAction(null);
        prepareClickIntentRef.current = false;
        prepareTraceIdRef.current = null;
        return;
      }
    } else if (
      franchiseGridPage !== pendingPosterAction.page ||
      !groupedResults.some(group => group.key === pendingPosterAction.groupKey)
    ) {
      setPendingPosterAction(null);
      return;
    }
    if (!posterFirstPageReady) return;

    const action = pendingPosterAction;
    setPendingPosterAction(null);
    if (action.kind === 'catalogue') {
      void searchMovieTorrents(action.movie);
    } else {
      setExpandedSearchGroupKey(action.groupKey);
    }
  }, [pendingPosterAction, posterFirstPageReady, movieCatalogue, groupedResults, franchiseGridPage]);

  useEffect(() => {
    if (!movieCatalogue?.results?.length) return;
    queueOttLookups(movieCatalogue.results.slice(0, franchiseGridPageSize));
  }, [movieCatalogue?.results]);

  useEffect(() => {
    let timer: number | undefined;
    let cancelled = false;
    const startedAt = Date.now();
    let settledSince: number | null = null;

    // Keep every card on the low-resolution poster until the visible page has
    // loaded its thumbnails; only then let the idle queue consider upgrades.
    if (!posterFirstPageReady) return;

    const checkPageDetails = () => {
      if (cancelled) return;
      // OTT availability is useful card content, but it must not hold back idle
      // poster upgrades while the provider service is slow.
      const metadataSettled = activeFranchiseSearchMode || imdbInFlightRef.current === 0;
      const now = Date.now();

      if (metadataSettled) {
        if (settledSince == null) settledSince = now;
        // Allow state updates from the last metadata response to paint before
        // opening the shared high-resolution poster queue.
        if (now - settledSince >= 650) {
          setHighQualityPostersReady(true);
          return;
        }
      } else {
        settledSince = null;
      }

      // IMDb can be unavailable upstream. Do not leave thumbnails permanently
      // low-res: after 25 seconds, release the page as a whole.
      if (now - startedAt >= 25000) {
        setHighQualityPostersReady(true);
        return;
      }
      timer = window.setTimeout(checkPageDetails, 200);
    };

    timer = window.setTimeout(checkPageDetails, 200);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [posterFirstPageReady, activeFranchiseSearchMode]);

  // IMDb ratings are fetched independently after cards have rendered.
  // Movies use OMDb's movie type; show metadata uses OMDb's series type.
  useEffect(() => {
    // Synopsis/IMDb are cheap cached reads: load them with the initial cards, not after posters.
    const visibleStart = (franchiseGridPage - 1) * franchiseGridPageSize;
    const visibleGroupTitles = new Set(groupedResults
      .slice(visibleStart, visibleStart + franchiseGridPageSize)
      .map(group => normalizeSavedOttTitle(episodeSeriesTitle(group.title) || group.title)));
    const visibleSearchMetadata = searchMetadata.filter(item =>
      visibleGroupTitles.has(normalizeSavedOttTitle(item.title))
    );
    const visibleCatalogueItems = visibleMovieCatalogueItems.slice(
      (franchiseGridPage - 1) * franchiseGridPageSize,
      franchiseGridPage * franchiseGridPageSize,
    );
    const cachedCatalogueRatingKeys = new Set(
      visibleCatalogueItems
        .filter(item => item.imdbRatingStatus === 'found' || item.imdbRatingStatus === 'not_found')
        .map(item => imdbRatingKey(item.title, item.year)),
    );
    const candidates = [
      ...visibleCatalogueItems.flatMap(item => {
        const candidate = {
          title: String(item.title || '').trim(),
          year: item.year,
          tmdbId: Number(item.tmdbId || item.id) || undefined,
          mediaType: 'movie' as const,
        };
        // IMDb classifies One Last Kill as a TV special, while TMDB serves it
        // in the movie catalogue. Query both OMDb types so its IMDb rating is found.
        return /punisher.*one last kill|one last kill.*punisher/i.test(candidate.title)
          ? [candidate, { ...candidate, mediaType: 'tv' as const }]
          : [candidate];
      }),
      ...visibleSearchMetadata.map(item => ({
        title: String(item.title || '').trim(),
        year: item.year,
        tmdbId: Number(item.tmdbId || item.id) || undefined,
        mediaType: item.mediaType === 'tv' ? 'tv' as const : 'movie' as const,
      })),
      // Cards whose title has no saved metadata match still get a rating: look them up by
      // their own title/year, plus the TMDB id the catalogue row already carries.
      ...groupedResults
        .slice((franchiseGridPage - 1) * franchiseGridPageSize, franchiseGridPage * franchiseGridPageSize)
        .filter(group => !episodeSeriesTitle(group.title))
        .map(group => {
          const withId = group.variants.find(variant => Number((variant as any).tmdbId) > 0) as any;
          return {
            title: group.title.replace(/\s*\(?(?:19|20)\d{2}\)?\s*$/, '').trim(),
            year: group.year,
            tmdbId: Number(withId?.tmdbId) || undefined,
            mediaType: 'movie' as const,
          };
        }),
    ].filter(item => item.title);

    const pending = new Map<string, { title: string; mediaType: 'movie' | 'tv'; tmdbId?: number }>();
    for (const item of candidates) {
      const key = imdbRatingKey(item.title, item.year, item.mediaType);
      const cachedRating = imdbRatingsByKey[key];
      if (cachedCatalogueRatingKeys.has(key)) {
        imdbRatingRequestedRef.current.add(key);
        continue;
      }
      if (cachedRating && ['found', 'not_found'].includes(cachedRating.status)) {
        imdbRatingRequestedRef.current.add(key);
        continue;
      }
      if (!key.startsWith('movie||') && !key.startsWith('tv||') && !imdbRatingRequestedRef.current.has(key)) {
        pending.set(key, {
          title: imdbRatingQueryTitle(item.title, item.year),
          mediaType: item.mediaType,
          tmdbId: item.tmdbId,
        });
      }
    }
    const requests = [...pending.entries()].slice(0, 80);
    if (!requests.length) return;
    const hasMorePending = pending.size > requests.length;
    requests.forEach(([key]) => imdbRatingRequestedRef.current.add(key));

    const requestItems = requests.map(([, item]) => item);
    const scheduleImdbRetry = () => {
      // Bounded so a missing OMDb key doesn't poll forever.
      if (imdbRetryCountRef.current >= 5) return;
      imdbRetryCountRef.current += 1;
      window.setTimeout(() => setImdbRetryTick(tick => tick + 1), 20000);
    };
    imdbInFlightRef.current += 1;
    void api.getImdbRatings(
      requestItems.map(item => item.title),
      requestItems.map(item => item.mediaType),
      requestItems.map(item => item.tmdbId),
    ).then(response => {
      const ratings = Array.isArray(response.results) ? response.results : [];
      let needsRetry = false;
      // More than one batch of titles is waiting: continue with the next batch right away.
      if (hasMorePending) setImdbRetryTick(tick => tick + 1);
      setImdbRatingsByKey(previous => {
        const next = { ...previous };
        for (const rating of ratings) {
          // Transient failures (rate limit / upstream error) are not answers:
          // don't store them and let the lookup be retried below.
          if (rating.status === 'rate_limited' || rating.status === 'unavailable') {
            needsRetry = true;
            continue;
          }
          const mediaType = rating.mediaType === 'tv' ? 'tv' : 'movie';
          const queryKey = imdbRatingKey(rating.query || rating.title, undefined, mediaType);
          const canonicalKey = imdbRatingKey(rating.title, rating.year, mediaType);
          next[queryKey] = rating;
          next[canonicalKey] = rating;
        }
        return next;
      });
      if (needsRetry) {
        // Forget keys that never got a real answer, then re-run this effect later.
        requests.forEach(([key]) => {
          const done = ratings.some(rating => {
            if (rating.status === 'rate_limited' || rating.status === 'unavailable') return false;
            const mediaType = rating.mediaType === 'tv' ? 'tv' : 'movie';
            return imdbRatingKey(rating.query || rating.title, undefined, mediaType) === key;
          });
          if (!done) imdbRatingRequestedRef.current.delete(key);
        });
        scheduleImdbRetry();
      }
    }).catch(() => {
      // Ratings are optional metadata; don't interrupt torrent/catalogue rendering.
      requests.forEach(([key]) => imdbRatingRequestedRef.current.delete(key));
      scheduleImdbRetry();
    }).finally(() => {
      imdbInFlightRef.current = Math.max(0, imdbInFlightRef.current - 1);
    });
  }, [movieCatalogue?.results, searchMetadata, groupedResults, imdbRatingsByKey, imdbRetryTick, franchiseGridPage]);

  const clearTmdbCacheAndReload = async () => {
    const currentCatalogue = movieCatalogue;
    const key = currentCatalogue?.catalogue || tmdbFranchiseKey;
    const searchText = currentCatalogue ? movieCatalogueSearchText : tmdbFranchiseLabels[tmdbFranchiseKey];
    const page = currentCatalogue?.page || 1;
    const onlyReleased = currentCatalogue?.releasedOnly ?? releasedOnly;

    setIsClearingTmdbCache(true);
    setTmdbCacheMessage('');
    try {
      await api.clearTmdbMovieCaches();
      ottLookupGenerationRef.current += 1;
      ottQueueRef.current = [];
      ottQueuedIdsRef.current.clear();
      ottLiveCheckedRef.current.clear();
      ottRetryAttemptsRef.current.clear();
      ottAvailabilityRef.current = {};
      setOttAvailability({});
      setTmdbCacheMessage('TMDB catalogue and OTT caches cleared. Reloading the selected catalogue…');
      await openMovieCatalogue(key, searchText, page, onlyReleased);
      setTmdbCacheMessage('TMDB caches cleared and a fresh catalogue request was sent.');
    } catch (error: any) {
      setTmdbCacheMessage(String(error?.message || 'Could not clear TMDB caches.'));
    } finally {
      setIsClearingTmdbCache(false);
    }
  };

  const selectCachedFranchise = (franchise: CachedFranchiseKey) => {
    setOpenCachedLanguageMenu(null);
    setResolutionFilter(null);
    setSizeSort(null);
    setTimeSort(null);
    setTmdbFranchiseKey(franchise);
    const mode = franchise === 'marvel' ? 'marvel' : 'dc-live-action';
    const category = franchise === 'marvel' ? 'Marvel' : 'DC';
    void runSearch(undefined, category, mode);
  };

  const extractedQuality = (result: TorrentSearchResult) => {
    const details = torrentQualityDetails(result);
    return details.label === 'Other release' ? '' : details.label;
  };

  // A prepared movie is an inline view, not persistent catalogue selection.
  // Clear it before changing sections, subsections, filters or numbered pages;
  // cancel any stale lookup so its late result cannot reopen the old card.
  useLayoutEffect(() => {
    prepareClickIntentRef.current = false;
    prepareTraceIdRef.current = null;
    prepareBuildPendingRef.current = false;
    setExpandedSearchGroupKey(null);
    movieTorrentSearchControllerRef.current?.abort();
    movieTorrentSearchControllerRef.current = null;
    movieTorrentSearchGenerationRef.current += 1;
    setMovieTorrentSearchLoading(false);
    setMovieTorrentPickerGroups(null);
    setMovieTorrentPreviewGroup(null);
    setMovieTorrentPreviewCatalogueId(null);
    setMovieTorrentSearchError('');
  }, [
    activeTopSection,
    marvelContentTab,
    dcContentTab,
    classicsContentTab,
    regionalContentTabs,
    regionalCatalogueModes,
    franchiseGridPage,
    marvelFilmFilter,
    showMarvelWebSeries,
    showDcWebSeries,
  ]);

  return (
    <div className="space-y-2.5 sm:space-y-4">
      <div className="p-2.5 sm:p-5 rounded-xl sm:rounded-2xl bg-slate-900 border border-slate-800">
        <div className="flex flex-col gap-1">
          <h2 className="text-base font-bold text-slate-100 flex items-center gap-2">
            <Search className="w-5 h-5 text-cyan-400" />
            Search Torrents
          </h2>
        </div>

        <form data-torrent-search="true" onSubmit={runSearch} className="mt-2.5 sm:mt-4 flex flex-row gap-1.5 sm:gap-2">
          <div ref={recentSearchRef} className="relative flex-1">
            <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
            <input
              value={query}
              onFocus={() => {
                if (recentSearches.length > 0) setShowRecentSearches(true);
              }}
              onChange={(e) => {
                setQuery(e.target.value);
                if (error) setError('');
                if (recentSearches.length > 0) setShowRecentSearches(true);
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  setShowRecentSearches(false);
                }
              }}
              placeholder="Search movies, TV, music, software..."
              className="w-full pl-9 pr-10 py-2 sm:py-2.5 rounded-lg sm:rounded-xl bg-slate-950 border border-slate-800 text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500/20"
            />
            {query && (
              <button
                type="button"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => {
                  setQuery('');
                  setError('');
                  setMovieCatalogue(null);
                  setSearched(false);
                  setResults([]);
                  setShowRecentSearches(recentSearches.length > 0);
                }}
                className="absolute right-2 top-1/2 -translate-y-1/2 p-1 rounded-lg text-slate-500 hover:text-slate-200 hover:bg-slate-800 transition"
                title="Clear search"
                aria-label="Clear search"
              >
                <X className="w-4 h-4" />
              </button>
            )}

            {showRecentSearches && recentSearches.length > 0 && (
              <div className="absolute left-0 right-0 top-full mt-2 z-30 rounded-xl border border-slate-700 bg-slate-900 shadow-2xl overflow-hidden">
                <div className="px-3 py-2 border-b border-slate-800">
                  <span className="text-[11px] font-semibold text-slate-400">Recent Searches</span>
                </div>
                <div className="max-h-72 overflow-y-auto">
                  {recentSearches.slice(0, 7).map((search, index) => (
                    <div
                      key={search}
                      className="flex items-center gap-1 border-b border-slate-800/70 last:border-b-0 hover:bg-slate-800 transition"
                    >
                      <button
                        type="button"
                        onMouseDown={(e) => e.preventDefault()}
                        onClick={() => {
                          setQuery(search);
                          setError('');
                          setShowRecentSearches(false);
                        }}
                        className="min-w-0 flex-1 px-3 py-2.5 text-left flex items-center gap-2.5 active:bg-slate-700 transition"
                      >
                        <span className="w-5 h-5 shrink-0 rounded-md bg-slate-800 text-slate-500 text-[10px] font-bold flex items-center justify-center">
                          {index + 1}
                        </span>
                        <span className="truncate text-xs text-slate-200">{search}</span>
                      </button>

                      <button
                        type="button"
                        onMouseDown={(e) => e.preventDefault()}
                        onClick={() => {
                          setRecentSearches(prev => {
                            const next = prev.filter(item => item !== search);
                            try {
                              localStorage.setItem('seedflow_recent_searches', JSON.stringify(next));
                            } catch {}
                            return next;
                          });
                        }}
                        className="mr-2 p-1.5 rounded-lg text-slate-500 hover:text-slate-200 hover:bg-slate-700 transition shrink-0"
                        title="Delete recent search"
                        aria-label={`Delete recent search: ${search}`}
                      >
                        <X className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          <button
            type={isSearching || nexStreamLoading ? 'button' : 'submit'}
            onClick={isSearching || nexStreamLoading ? cancelSearch : undefined}
            aria-label={isSearching || nexStreamLoading ? 'Cancel search' : 'Search'}
            title={isSearching || nexStreamLoading ? 'Cancel search' : 'Search'}
            className={[
              'group relative isolate overflow-hidden w-10 sm:w-auto min-h-10 px-2.5 sm:px-5 py-2 rounded-xl text-xs font-bold inline-flex items-center justify-center gap-2 transition-all duration-200 ease-out focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950 active:scale-[0.97]',
              isSearching || nexStreamLoading
                ? 'border border-rose-300/70 bg-rose-500 text-white shadow-[0_0_0_1px_rgba(244,63,94,0.18),0_0_18px_rgba(244,63,94,0.20)] hover:bg-rose-400 hover:shadow-[0_0_24px_rgba(244,63,94,0.34)] focus-visible:ring-rose-300'
                : 'border border-cyan-300/60 bg-cyan-400 text-slate-950 shadow-[0_0_0_1px_rgba(34,211,238,0.12)] hover:-translate-y-0.5 hover:border-cyan-200 hover:bg-cyan-300 hover:shadow-[0_0_22px_rgba(34,211,238,0.36)] focus-visible:ring-cyan-300'
            ].join(' ')}
          >
            <span className="pointer-events-none absolute inset-0 -z-10 rounded-xl opacity-0 transition-opacity duration-200 group-hover:opacity-100"
              style={{ background: (isSearching || nexStreamLoading) ? 'linear-gradient(110deg, rgba(255,255,255,0.10), transparent 55%)' : 'linear-gradient(110deg, rgba(255,255,255,0.24), transparent 55%)' }}
            />
            {isSearching || nexStreamLoading ? (
              <>
                <X className="h-4 w-4 shrink-0 transition-transform duration-200 group-hover:rotate-90" />
                <span className="hidden sm:inline">Cancel</span>
              </>
            ) : (
              <>
                <Search className="h-4 w-4 shrink-0 transition-transform duration-200 group-hover:scale-110" />
                <span className="hidden sm:inline">Search</span>
              </>
            )}
          </button>
          <button type="button" onClick={() => { const trimmed = query.trim(); if (trimmed.length < 2) { setNexStreamError('Enter at least 2 characters to search.'); return; } void searchNexStreamTitles(trimmed, 'tv'); }} className="shrink-0 rounded-xl border border-violet-400/60 bg-violet-500/15 px-3 py-2 text-xs font-bold text-violet-200 hover:bg-violet-500/25">NexStream TV</button>
        </form>

        <div className="mt-2 flex flex-wrap gap-2" aria-label="Movie catalogues">
          <button type="button" onClick={() => { setFranchiseGridPage(1); setActiveTopSection('marvel'); setMarvelContentTab('movies'); setShowMarvelWebSeries(false); setShowDcWebSeries(false); setMarvelFilmFilter('all'); selectCachedFranchise('marvel'); }} className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs font-semibold text-slate-300 hover:border-cyan-500 hover:text-cyan-300">🦸 Marvel</button>
          <button type="button" onClick={() => { setFranchiseGridPage(1); setActiveTopSection('dc'); setDcContentTab('movies'); setShowDcWebSeries(false); selectCachedFranchise('dc-live-action'); }} className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs font-semibold text-slate-300 hover:border-cyan-500 hover:text-cyan-300">🦇 DC</button>
          <button type="button" onClick={() => { setFranchiseGridPage(1); setActiveTopSection('hollywood'); setRegionalContentTabs(previous => ({ ...previous, hollywood: 'movies' })); openRegionalCatalogue('hollywood', regionalCatalogueModes.hollywood); }} className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs font-semibold text-slate-300 hover:border-cyan-500 hover:text-cyan-300">🎥 Hollywood</button>
          <button type="button" onClick={() => { setFranchiseGridPage(1); setActiveTopSection('bollywood'); setRegionalContentTabs(previous => ({ ...previous, bollywood: 'movies' })); openRegionalCatalogue('bollywood', regionalCatalogueModes.bollywood); }} className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs font-semibold text-slate-300 hover:border-cyan-500 hover:text-cyan-300">🎬 Bollywood</button>
          <button type="button" onClick={() => { setFranchiseGridPage(1); setActiveTopSection('classics'); setClassicsContentTab('movies'); setResolutionFilter(null); setSizeSort(null); setTimeSort(null); void runSearch(undefined, 'Classics', 'classics'); }} className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs font-semibold text-slate-300 hover:border-cyan-500 hover:text-cyan-300">⭐ Favourites</button>
        </div>
        {activeTopSection !== null && <>
          <div className="mt-2 flex flex-wrap gap-2 rounded-lg border border-slate-800 bg-slate-900 px-2.5 py-2.5" aria-label={activeTopSection + ' subsections'}>
            {activeTopSection === 'marvel' && <>
              <button type="button" onClick={() => { setFranchiseGridPage(1); setMarvelContentTab('movies'); setShowMarvelWebSeries(false); setMarvelFilmFilter('all'); selectCachedFranchise('marvel'); }} className={marvelContentTab === 'movies' ? 'rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950' : 'rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800 hover:text-slate-200'}>🎬 Marvel Movies</button>
              <button type="button" onClick={() => { setFranchiseGridPage(1); setMarvelContentTab('web-series'); if (!showMarvelWebSeries || marvelWebSeriesCatalogue?.catalogue !== 'marvel-web-series') void loadMarvelWebSeries(); }} className={marvelContentTab === 'web-series' ? 'rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950' : 'rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800 hover:text-slate-200'}>📺 Marvel Web Series</button>
            </>}
            {activeTopSection === 'dc' && <>
              <button type="button" onClick={() => { setFranchiseGridPage(1); setDcContentTab('movies'); setShowDcWebSeries(false); selectCachedFranchise('dc-live-action'); }} className={dcContentTab === 'movies' ? 'rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950' : 'rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800 hover:text-slate-200'} aria-pressed={dcContentTab === 'movies'}>DC Movies</button>
              <button type="button" onClick={() => { setFranchiseGridPage(1); setDcContentTab('animated'); setResolutionFilter(null); setSizeSort(null); setTimeSort(null); setShowDcWebSeries(false); void runSearch(undefined, 'DC Animated Movies', 'dc-animated'); }} className={dcContentTab === 'animated' ? 'rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950' : 'rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800 hover:text-slate-200'} aria-pressed={dcContentTab === 'animated'}>DC Animated</button>
              <button type="button" onClick={() => { setFranchiseGridPage(1); setDcContentTab('web-series'); if (showDcWebSeries) setShowDcWebSeries(false); else void loadDcWebSeries(); }} className={dcContentTab === 'web-series' ? 'rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950' : 'rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800 hover:text-slate-200'} aria-pressed={dcContentTab === 'web-series'}>📺 DC Web Series</button>
            </>}
            {(activeTopSection === 'hollywood' || activeTopSection === 'bollywood') && <>
              <button type="button" onClick={() => { setRegionalContentTabs(previous => ({ ...previous, [activeTopSection]: 'movies' })); openRegionalCatalogue(activeTopSection, regionalCatalogueModes[activeTopSection]); }} className={regionalContentTabs[activeTopSection] === 'movies' ? 'rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950' : 'rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800'}>{activeTopSection === 'hollywood' ? '🎬 Hollywood Movies' : '🎬 Bollywood Movies'}</button>
              <button type="button" onClick={() => { const group = activeTopSection === 'bollywood' ? 'indian' : 'hollywood'; setRegionalContentTabs(previous => ({ ...previous, [activeTopSection]: 'web-series' })); void openRegionalTvCatalogue(group, regionalTvModes[group]); }} className={regionalContentTabs[activeTopSection] === 'web-series' ? 'rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950' : 'rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800'}>{activeTopSection === 'hollywood' ? '📺 English Web Series' : '📺 Indian Web Series'}</button>
            </>}
            {activeTopSection === 'classics' && <>
              <button type="button" onClick={() => { setFranchiseGridPage(1); setClassicsContentTab('movies'); setShowMarvelWebSeries(false); setShowDcWebSeries(false); setResolutionFilter(null); setSizeSort(null); setTimeSort(null); void runSearch(undefined, 'Classics', 'classics'); }} className={classicsContentTab === 'movies' ? 'rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950' : 'rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800 hover:text-slate-200'}>🎬 Movies</button>
              <button type="button" onClick={() => { setFranchiseGridPage(1); setNexStreamError(''); setNexStreamSearchMode(null); void loadClassicsTvCatalogue('classics-web-series', 'web-series'); }} className={classicsContentTab === 'web-series' ? 'rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950' : 'rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800 hover:text-slate-200'}>📺 Web Series</button>
              <button type="button" onClick={() => { setFranchiseGridPage(1); setNexStreamError(''); setNexStreamSearchMode(null); void loadClassicsTvCatalogue('classics-anime', 'anime'); }} className={classicsContentTab === 'anime' ? 'rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950' : 'rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800 hover:text-slate-200'}>⛩️ Anime</button>
            </>}
          </div>
          {activeTopSection === 'marvel' && marvelContentTab === 'movies' && <div className="mt-2 flex flex-wrap gap-2 rounded-lg border border-slate-800 bg-slate-950/70 px-2.5 py-2.5" aria-label="Marvel movie filters">
            {([['all','All'],['mcu','MCU'],['sony','Spiderman'],['x-men','X-Men']] as [MarvelFilmFilter,string][]).map(([filter,label]) => <button key={filter} type="button" onClick={() => { setFranchiseGridPage(1); setMarvelFilmFilter(filter); }} className={marvelFilmFilter === filter ? 'rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950' : 'rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800 hover:text-slate-200'} aria-pressed={marvelFilmFilter === filter}>{label}</button>)}
          </div>}
          {(activeTopSection === 'hollywood' || activeTopSection === 'bollywood') && regionalContentTabs[activeTopSection] !== null && <div className="mt-2 flex flex-wrap gap-2 rounded-lg border border-slate-800 bg-slate-950/70 px-2.5 py-2.5" aria-label={activeTopSection + ' ' + regionalContentTabs[activeTopSection] + ' filters'}>
            {(['popular','trending','latest'] as RegionalCatalogueMode[]).map(mode => regionalContentTabs[activeTopSection] === 'movies'
              ? <button key={mode} type="button" onClick={() => openRegionalCatalogue(activeTopSection, mode)} className={regionalCatalogueModes[activeTopSection] === mode ? 'rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950' : 'rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800'} aria-pressed={regionalCatalogueModes[activeTopSection] === mode}>{mode.charAt(0).toUpperCase()+mode.slice(1)}</button>
              : <button key={mode} type="button" onClick={() => { const group = activeTopSection === 'bollywood' ? 'indian' : 'hollywood'; void openRegionalTvCatalogue(group, mode); }} className={regionalTvModes[activeTopSection === 'bollywood' ? 'indian' : 'hollywood'] === mode ? 'rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950' : 'rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800'} aria-pressed={regionalTvModes[activeTopSection === 'bollywood' ? 'indian' : 'hollywood'] === mode}>{mode.charAt(0).toUpperCase()+mode.slice(1)}</button>)}
          </div>}
        </>}
        <div className="mt-2 flex flex-col gap-2 rounded-lg border border-slate-800 bg-slate-950/70 p-2.5 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <div className="text-xs font-semibold text-slate-200">TMDB movie discovery</div>
            <div className="text-[11px] text-slate-500">Load fresh Marvel/DC movie metadata instead of the cached torrent list.</div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <select
              aria-label="Franchise to browse using TMDB"
              value={tmdbFranchiseKey}
              onChange={(event) => setTmdbFranchiseKey(event.target.value as FranchiseTmdbKey)}
              className="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-900 px-2 py-2 text-xs text-slate-200 sm:flex-none"
            >
              <option value="marvel">Marvel</option>
              <option value="dc-live-action">DC</option>
              <option value="dc-animated">DC Animated</option>
            </select>
            <button
              type="button"
              disabled={isSearching}
              onClick={() => {
                setResolutionFilter(null);
                setSizeSort(null);
                setTimeSort(null);
                            void openMovieCatalogue(tmdbFranchiseKey, tmdbFranchiseLabels[tmdbFranchiseKey], 1);
              }}
              className="shrink-0 rounded-lg bg-cyan-500 px-3 py-2 text-xs font-bold text-slate-950 hover:bg-cyan-400 disabled:opacity-50"
            >
              Browse with TMDB
            </button>
            <button
              type="button"
              disabled={isSearching || isClearingTmdbCache}
              onClick={() => void clearTmdbCacheAndReload()}
              className="shrink-0 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs font-semibold text-amber-200 hover:bg-amber-500/20 disabled:opacity-50"
              title="Clears all TMDB movie-catalogue and OTT caches, then refetches the current catalogue."
            >
              {isClearingTmdbCache
                ? <><Loader2 className="mr-1.5 inline h-3.5 w-3.5 animate-spin" />Clearing…</>
                : 'Clear TMDB cache & reload'}
            </button>
          </div>
          {tmdbCacheMessage && (
            <div className="text-[11px] leading-relaxed text-slate-400">{tmdbCacheMessage}</div>
          )}
        </div>
      </div>

      {isSearching && (
        <div className="p-5 sm:p-7 rounded-xl sm:rounded-2xl bg-slate-900 border border-slate-800 flex flex-col items-center justify-center gap-3">
          <Loader2 className="w-8 h-8 text-cyan-400 animate-spin" />
          <div className="text-sm font-semibold text-slate-200">{catalogueBuildProgress ? 'Loading movie catalogue...' : isPresetCatalogueAction ? 'Loading catalogue...' : 'Searching torrents...'}</div>
          <div className="text-xs text-slate-500 text-center">{catalogueBuildProgress || 'Loading the selected catalogue. Please wait for the results.'}</div>
        </div>
      )}

      {prepareError && (
        <div className="p-2.5 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-200 text-xs flex items-center justify-between gap-2">
          <span>{prepareError}</span>
          <button type="button" onClick={() => setPrepareError('')} className="shrink-0 p-1 rounded text-slate-400 hover:text-slate-200" aria-label="Dismiss preparation error">
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      )}

      {prepareWaitOpen && (
        <div className="fixed inset-0 z-[200] flex items-center justify-center bg-slate-950/45 backdrop-blur-sm p-4">
          <div className="w-full max-w-sm rounded-2xl border border-cyan-400/20 bg-slate-900/95 shadow-2xl p-5">
            <div className="flex items-start gap-3">
              <div className="shrink-0 rounded-xl bg-cyan-500/10 border border-cyan-500/20 p-2">
                <Loader2 className="w-5 h-5 text-cyan-300 animate-spin" />
              </div>
              <div className="min-w-0">
                <div className="font-bold text-slate-100 text-sm">Loading is taking a little longer</div>
                <div className="mt-1 text-xs leading-5 text-slate-400">
                  <span className="text-slate-200">{prepareWaitTitle || 'This torrent'}</span> is still being prepared by Seedr. You can explore the app or wait for it to finish.
                </div>
              </div>
            </div>
            <div className="mt-4 flex gap-2">
              <button
                type="button"
                onClick={() => setPrepareWaitOpen(false)}
                className="flex-1 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 px-3 py-2 text-xs font-semibold transition"
              >
                Explore
              </button>
              <button
                type="button"
                onClick={() => {
                  setPrepareWaitOpen(false);
                  onOpenProgress?.();
                }}
                className="flex-1 rounded-xl bg-cyan-500 hover:bg-cyan-400 text-slate-950 px-3 py-2 text-xs font-bold transition"
              >
                View loading progress
              </button>
            </div>
          </div>
        </div>
      )}

      {error && (
        <div className="p-3.5 rounded-2xl bg-amber-500/10 border border-amber-500/30 text-amber-300 text-xs flex items-start gap-2.5">
          <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
          <div>
            <div className="font-semibold">{error}</div>
            {error.toLowerCase().includes('configured') ? (
              <div className="text-amber-400/80 mt-1">
                Try the full movie or series title, and include a year or season/episode when needed.
              </div>
            ) : null}
          </div>
        </div>
      )}

      {(nexStreamPlayer || nexStreamSearchMode === 'tv' || nexStreamSearchMode === 'movie') && (
        <section className="space-y-3 rounded-2xl border border-violet-500/30 bg-slate-900/70 p-3 sm:p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h3 className="text-sm font-bold text-violet-200">{nexStreamSearchMode === 'tv' ? 'NexStream TV Search' : 'NexStream movie matches'}</h3>
              <p className="mt-1 text-[11px] text-slate-400">{nexStreamSearchMode === 'tv' ? 'TV-show results are independent of torrent results. Choose a show to play an episode.' : 'Movie playback options are separate from your existing torrent and Seedr results.'}</p>
            </div>
            {nexStreamLoading && <span className="inline-flex items-center gap-1.5 text-xs text-violet-200"><Loader2 className="h-3.5 w-3.5 animate-spin" />Searching titles…</span>}
          </div>

          {nexStreamError && (
            <div role="status" className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">{nexStreamError}</div>
          )}

          {!nexStreamLoading && nexStreamResults.length > 0 && nexStreamSearchMode === 'movie' && (
            <p className="text-xs text-slate-400">NexStream movie playback options are attached to matching torrent title cards below.</p>
          )}

          {nexStreamSearchMode === 'tv' && nexStreamResults.length > 0 && (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6">
              {nexStreamResults.filter(item => item.type === 'tv').map(item => (
                <article key={'nexstream-tv-' + item.id} className="min-w-0 overflow-hidden rounded-xl border border-violet-500/25 bg-slate-950">
                  <div className="aspect-[2/3] bg-slate-900">
                    {item.poster ? <img src={lowQualityPosterUrl(item.poster)} alt={item.title} loading="eager" onLoad={event => {
                            const image = event.currentTarget;
                            if (!image.dataset.highQualityTried && image.currentSrc.includes('/t/p/w185/')) {
                              image.dataset.highQualityTried = 'true';
                              image.src = image.currentSrc.replace('/t/p/w185/', '/t/p/w780/');
                            }
                          }} className="h-full w-full object-cover" onError={event => {
                            const image = event.currentTarget;
                            if (image.dataset.highQualityTried === 'true' && image.src.includes('/t/p/w780/')) {
                              image.src = image.src.replace('/t/p/w780/', '/t/p/w185/');
                              return;
                            }
                            image.style.display = 'none';
                          }} /> : <div className="flex h-full items-center justify-center text-slate-600"><Film className="h-8 w-8" /></div>}
                  </div>
                  <div className="flex flex-col gap-2 p-2.5">
                    <h4 className="line-clamp-2 text-sm font-bold text-slate-100">{item.title}</h4>
                    <p className="text-[11px] text-slate-500">{item.date ? item.date.slice(0, 4) : 'Release year unknown'} · TV series</p>
                    {item.overview && <p className="line-clamp-3 text-[11px] leading-relaxed text-slate-400">{item.overview}</p>}
                    <button
                      type="button"
                      disabled={nexStreamResolvingId === 'tv:' + item.id || nexStreamEpisodePrefetchStatus[String(item.id)] === 'loading'}
                      onClick={() => void playWithNexStream(item)}
                      className="mt-auto w-full rounded-lg bg-violet-500 px-3 py-2 text-xs font-bold text-white hover:bg-violet-400 disabled:cursor-wait disabled:opacity-60"
                    >
                      {nexStreamResolvingId === 'tv:' + item.id ? <><Loader2 className="mr-1.5 inline h-3.5 w-3.5 animate-spin" />Resolving…</>
                        : nexStreamEpisodePrefetchStatus[String(item.id)] === 'loading' ? <><Loader2 className="mr-1.5 inline h-3.5 w-3.5 animate-spin" />Preparing episodes…</>
                        : nexStreamEpisodePrefetchStatus[String(item.id)] === 'ready' ? <><Play className="mr-1.5 inline h-3.5 w-3.5" />Choose episode</>
                        : <><Play className="mr-1.5 inline h-3.5 w-3.5" />{nexStreamEpisodePrefetchStatus[String(item.id)] === 'error' ? 'Retry episode data' : 'Choose episode'}</>}
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}

          {nexStreamSearchMode === 'tv' && !nexStreamLoading && !nexStreamError && nexStreamResults.length === 0 && (
            <p className="text-xs text-slate-400">No TV shows found. Try a different title.</p>
          )}

          {nexStreamEpisodePicker && (
            <div className="fixed inset-0 z-[10000] flex items-center justify-center bg-black/80 p-3 sm:p-6" role="dialog" aria-modal="true" aria-labelledby="nexstream-episode-picker-title">
              <div className="flex max-h-[92vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-violet-400/30 bg-slate-950 shadow-2xl">
                <div className="flex items-start justify-between gap-3 border-b border-slate-800 p-4">
                  <div className="min-w-0">
                    <h3 id="nexstream-episode-picker-title" className="text-lg font-bold text-white">Choose an episode</h3>
                    <p className="mt-1 truncate text-sm text-slate-400">{nexStreamEpisodePicker.title}</p>
                  </div>
                  <button type="button" onClick={() => setNexStreamEpisodePicker(null)} className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800">Close</button>
                </div>
                <div className="flex items-center gap-3 border-b border-slate-800 p-4">
                  <label htmlFor="nexstream-season-select" className="shrink-0 text-sm font-semibold text-slate-300">Season</label>
                  <select id="nexstream-season-select" value={nexStreamSelectedSeason} onChange={event => { const season = Number(event.target.value); void loadNexStreamEpisodes(nexStreamEpisodePicker, season); }} className="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-white">
                    {nexStreamSeasons.map(season => <option key={season.seasonNumber} value={season.seasonNumber}>{season.name || 'Season ' + season.seasonNumber}{season.episodeCount ? ' (' + season.episodeCount + ' episodes)' : ''}</option>)}
                  </select>
                </div>
                <div className="min-h-0 flex-1 overflow-y-auto p-3 sm:p-4">
                  {nexStreamEpisodesLoading ? (
                    <div className="flex items-center justify-center gap-2 py-12 text-sm text-slate-300"><Loader2 className="h-4 w-4 animate-spin" />Loading episodes…</div>
                  ) : nexStreamEpisodesError ? (
                    <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">
                      <p>{nexStreamEpisodesError}</p>
                      <div className="mt-2 flex flex-wrap items-center gap-2">
                        <button type="button" onClick={() => { if (nexStreamEpisodePicker) void loadNexStreamEpisodes(nexStreamEpisodePicker, nexStreamSelectedSeason); }} className="rounded-lg border border-amber-300/40 px-3 py-1.5 font-semibold hover:bg-amber-400/10">Retry episode list</button>
                        <span className="text-xs text-amber-100/70">You can also enter an episode number below.</span>
                      </div>
                    </div>
                  ) : nexStreamEpisodes.length > 0 ? (
                    <div className="space-y-2">
                      {nexStreamEpisodes.map(episode => (
                        <button key={episode.episodeNumber} type="button" onClick={() => { const selected = nexStreamEpisodePicker; setNexStreamEpisodePicker(null); if (selected) void playWithNexStream(selected, nexStreamSelectedSeason, episode.episodeNumber); }} className="flex w-full items-start gap-3 rounded-xl border border-slate-800 bg-slate-900/70 p-3 text-left transition hover:border-violet-400/60 hover:bg-violet-500/10">
                          {episode.still ? <img src={episode.still} alt="" loading="lazy" className="hidden h-20 w-32 shrink-0 rounded-lg bg-slate-800 object-cover sm:block" onError={event => { event.currentTarget.style.display = 'none'; }} /> : null}
                          <div className="min-w-0 flex-1">
                            <div className="text-sm font-bold text-slate-100">E{String(episode.episodeNumber).padStart(2, '0')} · {episode.name || 'Episode ' + episode.episodeNumber}</div>
                            {episode.airDate && <div className="mt-1 text-[11px] text-slate-500">{episode.airDate}</div>}
                            {episode.overview && <p className="mt-1 line-clamp-3 text-xs leading-relaxed text-slate-400">{episode.overview}</p>}
                          </div>
                          <Play className="mt-1 h-4 w-4 shrink-0 text-violet-300" />
                        </button>
                      ))}
                    </div>
                  ) : !nexStreamEpisodesLoading && !nexStreamEpisodesError ? (
                    <div className="py-8 text-center text-sm text-slate-400">No episode metadata was returned for this season.</div>
                  ) : null}
                  {(nexStreamEpisodesError || (!nexStreamEpisodesLoading && nexStreamEpisodes.length === 0)) && (
                    <form className="mt-4 flex flex-wrap items-end gap-3" onSubmit={event => { event.preventDefault(); const form = event.currentTarget; const input = form.elements.namedItem('episodeNumber') as HTMLInputElement; const number = Number(input.value); if (Number.isInteger(number) && number > 0 && nexStreamEpisodePicker) { const selected = nexStreamEpisodePicker; setNexStreamEpisodePicker(null); void playWithNexStream(selected, nexStreamSelectedSeason, number); } }}>
                      <label className="flex-1 text-xs text-slate-400">Episode number<input name="episodeNumber" type="number" min="1" defaultValue="1" className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-white" /></label>
                      <button type="submit" className="rounded-lg bg-violet-500 px-4 py-2 text-sm font-bold text-white">Play episode</button>
                    </form>
                  )}
                </div>
              </div>
            </div>
          )}

          {nexStreamPlayer && (
            <div className="fixed inset-0 z-[10000] flex items-center justify-center bg-black/85 p-2 sm:p-5">
              <div className="relative flex max-h-[96vh] w-full max-w-6xl flex-col overflow-hidden rounded-2xl border border-violet-400/40 bg-slate-950 shadow-2xl">
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 px-4 py-3 pr-14">
                <div className="min-w-0">
                  <div className="truncate text-xs font-bold text-slate-100">{nexStreamPlayer.title}{nexStreamPlayer.year ? ' (' + nexStreamPlayer.year + ')' : ''}</div>
                </div>
                <button
                  type="button"
                  onClick={() => window.open(nexStreamPlayer.url, '_blank', 'noopener,noreferrer')}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-violet-400/40 px-3 py-2 text-xs font-semibold text-violet-200 hover:bg-violet-500/15"
                  title="Open fullscreen playback in a new tab"
                >
                  <Maximize2 className="h-4 w-4" /> Full screen / New tab
                </button>
              </div>
              <button
                type="button"
                onClick={() => setNexStreamPlayer(null)}
                className="absolute right-2 top-2 z-10 flex h-9 w-9 items-center justify-center rounded-full border border-slate-600 bg-slate-900/95 text-lg font-bold text-slate-200 shadow-lg hover:bg-slate-700"
                aria-label="Close NexStream player"
                title="Close player"
              >
                ×
              </button>
              <div
                id="nexstream-player-container"
                className="min-h-0 flex-1 aspect-video overflow-hidden bg-black max-sm:aspect-video max-sm:max-h-[calc(96vh-90px)]"
              >
                <iframe
                  id="nexstream-player-frame"
                  key={nexStreamPlayer.url}
                  src={nexStreamPlayer.url}
                  title={'NexStream player for ' + nexStreamPlayer.title}
                  allow="autoplay; fullscreen; encrypted-media; picture-in-picture"
                  allowFullScreen
                  referrerPolicy="strict-origin-when-cross-origin"
                  className="h-full w-full border-0"
                />
              </div>
              </div>
            </div>
          )}
        </section>
      )}

      {!movieCatalogue && !nexStreamPlayer && nexStreamSearchMode !== 'tv' && (nexStreamLoading || nexStreamError) && (
        <div className="rounded-xl border border-violet-500/20 bg-slate-900/60 px-3 py-2 text-xs text-slate-300">
          {nexStreamLoading && <span className="inline-flex items-center gap-2"><Loader2 className="h-3.5 w-3.5 animate-spin" />Looking for NexStream playback options…</span>}
          {nexStreamError && <span className="ml-2 text-amber-200">{nexStreamError}</span>}
        </div>
      )}

      {movieCatalogue && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2 px-1">
            <div className="text-xs text-slate-400">
              {movieCatalogue.catalogue.startsWith('trending-')
                ? movieCatalogue.results.length + ' trending titles on this page · Page ' + movieCatalogue.page + ' of ' + movieCatalogue.totalPages
                : movieCatalogue.totalResults.toLocaleString() + ' movies · Page ' + movieCatalogue.page + ' of ' + movieCatalogue.totalPages.toLocaleString()
              } · Metadata by {movieCatalogue.provider}
            </div>
            <div className="text-[11px] text-slate-500">Select a movie to search torrent providers</div>
          </div>

          <div className="flex items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-900 px-3 py-2">
            <button
              type="button"
              disabled={isSearching || movieCatalogue.page <= 1}
              onClick={() => void openMovieCatalogue(movieCatalogue.catalogue, movieCatalogueSearchText, movieCatalogue.page - 1)}
              className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs font-semibold text-slate-300 hover:border-cyan-500 disabled:opacity-40"
            >
              Previous
            </button>
            <span className="text-xs text-slate-500">Page {movieCatalogue.page} of {movieCatalogue.totalPages.toLocaleString()}</span>
            <button
              type="button"
              disabled={isSearching || movieCatalogue.page >= movieCatalogue.totalPages}
              onClick={() => void openMovieCatalogue(movieCatalogue.catalogue, movieCatalogueSearchText, movieCatalogue.page + 1)}
              className="rounded-lg bg-cyan-500 px-3 py-2 text-xs font-bold text-slate-950 hover:bg-cyan-400 disabled:opacity-40"
            >
              Next
            </button>
          </div>

          {movieTorrentSearchLoading && (
            <div className="flex items-center gap-3 rounded-xl border border-cyan-500/30 bg-slate-950 px-3 py-3 text-sm text-slate-200">
              <Loader2 className="h-5 w-5 shrink-0 animate-spin text-cyan-400" />
              <div>
                <div className="font-semibold">Finding torrent releases for {movieTorrentSearchTitle}…</div>
                <div className="mt-0.5 text-xs text-slate-500">Combining matching qualities in the background. The movie catalogue stays open.</div>
              </div>
            </div>
          )}
          {pendingPosterAction && (
            <div className="rounded-xl border border-slate-800 bg-slate-950 px-3 py-2 text-xs text-slate-400">
              Loading the visible posters before opening this card…
            </div>
          )}
          {movieTorrentSearchError && (
            <div className={'flex items-start justify-between gap-3 rounded-xl border px-3 py-3 text-xs ' + (movieTorrentSearchError.startsWith('Coming soon on OTT') ? 'border-cyan-500/30 bg-cyan-500/10 text-cyan-100' : 'border-amber-500/30 bg-amber-500/10 text-amber-200')}>
              <span>{movieTorrentSearchError}</span>
              <button type="button" onClick={() => setMovieTorrentSearchError('')} className="shrink-0 underline">Dismiss</button>
            </div>
          )}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 lg:gap-4">
            {visibleMovieCatalogueItems.map(movie => {
              // Keep the selected torrent preview attached only to the exact
              // catalogue item the user clicked. Never reuse it by title alone.
              // A title match alone can leak an old Prepare card into another section
              // where the same title appears under a different catalogue record.
              const previewMatchesMovie =
                movieTorrentPreviewCatalogueId !== null &&
                movieTorrentPreviewCatalogueId === catalogueMovieIdentity(movie, movieCatalogue.catalogue);
              const previewGroup = previewMatchesMovie ? movieTorrentPreviewGroup : null;
              const isPunisherSpecial = /punisher.*one last kill|one last kill.*punisher/i.test(String(movie.title || ''));
              const movieImdb = imdbRatingsByKey[imdbRatingKey(movie.title, movie.year)] ||
                (isPunisherSpecial ? imdbRatingsByKey[imdbRatingKey(movie.title, movie.year, 'tv')] : undefined) ||
                imdbRatingForCatalogueMovie(movie) ||
                imdbFallbackForCatalogueMovie(movie);
              const movieTitleHref = imdbTitleLink(movieImdb, movie.title, movie.year);

              if (previewGroup) {
                const selectedKey = selectedMovieTorrentQualityByGroup[previewGroup.key] || torrentResultKey(previewGroup.variants[0]);
                const result = previewGroup.variants.find(variant => torrentResultKey(variant) === selectedKey) || previewGroup.variants[0];
                const prepareState = prepareStateFor(result);
                // Keep the catalogue poster as the source of truth for the
                // inline Prepare card; torrent-result poster metadata is often absent
                // or delayed and must not replace the movie's known poster.
                const isRegionalMoviePreview = /^(popular|trending|latest)-(bollywood|hollywood)$/.test(String(movieCatalogue?.catalogue || ''));
                const posterOverride = cataloguePosterOverride(movie);
                const loadedCataloguePosterUrl = cataloguePosterUrlsRef.current[String(movie.id)] || '';
                const cataloguePosterUrl = loadedCataloguePosterUrl ||
                  (isTheCrow2024(movie) ? posterOverride : movieTorrentPreviewPosterUrl) ||
                  posterOverride || cataloguePosterSource(movie);
                const posterSrc = loadedCataloguePosterUrl || lowQualityPosterUrl(
                  cataloguePosterUrl ||
                  (isRegionalMoviePreview ? '' : posterUrlFor(previewGroup.posterResult) || previewGroup.posterResult?.posterUrl || '')
                );
                const nexStreamMovieId = 'movie:' + String(movie.tmdbId || movie.id);
                return (
                  <MoviePrepareCard
                    key={'movie-prepare-' + String(movie.id) + '-' + posterSrc}
                    posterSrc={posterSrc}
                    posterAlt={previewGroup.title}
                    providers={(() => {
                      const availability = ottAvailabilityForMovie(movie);
                      return availability && availability.status !== 'loading' && availability.status !== 'error'
                        ? toCardProviders(uniqueOttProviders([
                            ...(availability.streamingProviders || []),
                            ...(availability.rentProviders || []),
                            ...(availability.buyProviders || []),
                          ]))
                        : [];
                    })()}
                    onPosterClick={closeMoviePrepareCard}
                    onPosterLoad={event => {
                      posterLoadedRef.current.add(posterKeyFor(previewGroup.posterResult));
                      event.currentTarget.style.display = '';
                      event.currentTarget.parentElement?.querySelector('[data-poster-placeholder="true"]')?.classList.add('hidden');
                    }}
                    onPosterError={event => {
                      const image = event.currentTarget;
                      const originalUrl = lowQualityPosterUrl(String(
                        movieTorrentPreviewPosterUrl ||
                        cataloguePosterUrlsRef.current[String(movie.id)] ||
                        cataloguePosterSource(movie) ||
                        (isRegionalMoviePreview ? '' : previewGroup.posterResult?.posterUrl || '')
                      ).trim(), 'w185');
                      // The compact card retries the unmodified poster when the resized
                      // URL fails. Do the same here so both cards show the same artwork.
                      if (originalUrl && image.dataset.originalFallbackTried !== 'true') {
                        const fallbackUrl = originalUrl.startsWith('/') ? API_BASE + originalUrl : originalUrl;
                        image.dataset.originalFallbackTried = 'true';
                        if (image.src !== new URL(fallbackUrl, window.location.origin).toString()) {
                          image.src = fallbackUrl;
                          return;
                        }
                      }
                      const key = posterKeyFor(previewGroup.posterResult);
                      posterLoadedRef.current.delete(key);
                      image.style.display = 'none';
                      image.parentElement?.querySelector('[data-poster-placeholder="true"]')?.classList.remove('hidden');
                      if (!posterBackgroundQueuedRef.current.has(key) && !posterOverrides[key]) {
                        posterBackgroundQueuedRef.current.add(key);
                        posterBackgroundQueueRef.current.unshift({ key, result: previewGroup.posterResult });
                        startPosterBackgroundWorkers();
                      }
                    }}
                    imdb={movieImdb}
                    seeders={result.seeders}
                    indexer={result.indexer}
                    quality={extractedQuality(result)}
                    title={movie.title}
                    titleHref={movieTitleHref}
                    year={movie.year || previewGroup.year}
                    onClose={closeMoviePrepareCard}
                    variants={previewGroup.variants.map(variant => {
                      const quality = torrentQualityDetails(variant);
                      const provider = String(variant.indexer || '').trim();
                      const size = formatBytes(Number(variant.size) || 0);
                      const seeds = Number(variant.seeders) || 0;
                      return {
                        key: torrentResultKey(variant),
                        label: `${quality.label} · ${size} · ▲${seeds}${provider ? ` · ${provider}` : ''}`,
                      };
                    })}
                    selectedVariantKey={torrentResultKey(result)}
                    onSelectVariant={key => setSelectedMovieTorrentQualityByGroup(previous => ({ ...previous, [previewGroup.key]: key }))}
                    variantsAriaLabel={`Choose quality for ${previewGroup.title}`}
                    prepared={prepareState.primaryFile ? preparedActionsFor(prepareState.torrentKey, prepareState.primaryFile) : null}
                    directStream={{
                      disabled: !Number(movie.tmdbId || movie.id) || nexStreamResolvingId === nexStreamMovieId,
                      label: nexStreamResolvingId === nexStreamMovieId ? 'Resolving…' : 'Direct Stream',
                      onClick: () => {
                        const tmdbId = Number(movie.tmdbId || movie.id);
                        if (!Number.isInteger(tmdbId) || tmdbId <= 0) {
                          setNexStreamError('This catalogue card has no valid TMDB ID for NexStream.');
                          return;
                        }
                        void playWithNexStream({
                          id: tmdbId,
                          title: movie.title,
                          date: movie.releaseDate || (movie.year ? String(movie.year) : ''),
                          overview: movie.overview || '',
                          poster: movie.posterUrl || movieTorrentPreviewPosterUrl || null,
                          type: 'movie',
                        });
                      },
                    }}
                    prepare={{
                      isPreparing: prepareState.isPreparing,
                      disabled: !prepareState.source || prepareState.isPreparing,
                      onClick: () => void prepareMovieTorrent(result),
                    }}
                  />
                );
              }

              const ott = ottAvailabilityForMovie(movie);
              const ottProviders = ott && ott.status !== 'loading' && ott.status !== 'error'
                ? toCardProviders(uniqueOttProviders([
                    ...(ott.streamingProviders || []),
                    ...(ott.rentProviders || []),
                    ...(ott.buyProviders || []),
                  ]))
                : [];
              return (
                <MovieCompactCard
                  key={String(movie.id)}
                  title={movie.title}
                  titleHref={movieTitleHref}
                  year={movie.year}
                  imdb={movieImdb}
                  tmdbRating={movie.rating}
                  trendWeekLabel={movieCatalogue.catalogue.startsWith('trending-') && movie.trendingWeek
                    ? 'Week of ' + new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' }).format(new Date(movie.trendingWeek + 'T00:00:00Z')) + (movie.trendingRank ? ' · #' + movie.trendingRank : '')
                    : undefined}
                  overview={movie.overview || 'No synopsis available from TMDB for this title.'}
                  posterSrc={lowQualityPosterUrl(cataloguePosterSource(movie), 'w185')}
                  posterAlt={movie.title}
                  onPosterClick={() => {
                    if (isSearching || movieTorrentSearchLoading || pendingPosterAction || prepareBuildPendingRef.current) return;
                    const traceId = `prep-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`;
                    prepareClickIntentRef.current = true;
                    prepareTraceIdRef.current = traceId;
                    console.info('[PREPARE_TRACE]', JSON.stringify({
                      event: 'explicit-card-click',
                      traceId,
                      title: movie.title,
                      year: movie.year || null,
                      at: new Date().toISOString(),
                    }));
                    const isHollywoodCatalogue = /^(popular|trending|latest)-hollywood$/.test(movieCatalogue.catalogue);
                    if (!isHollywoodCatalogue && !posterFirstPageReadyRef.current) {
                      setPendingPosterAction({
                        kind: 'catalogue',
                        movie,
                        catalogue: movieCatalogue.catalogue,
                        page: movieCatalogue.page,
                      });
                      return;
                    }
                    void searchMovieTorrents(movie);
                  }}
                  onPosterLoad={event => {
                    // Preserve the exact URL that made this catalogue poster visible.
                    cataloguePosterUrlsRef.current[String(movie.id)] = event.currentTarget.currentSrc || event.currentTarget.src;
                    event.currentTarget.style.display = '';
                    event.currentTarget.parentElement?.querySelector('[data-poster-placeholder="true"]')?.classList.add('hidden');
                  }}
                  onPosterError={event => {
                    const image = event.currentTarget;
                    const originalUrl = lowQualityPosterUrl(cataloguePosterSource(movie), 'w185');
                    // If the optimized w500 URL fails, retry the catalogue's
                    // original poster URL once before showing the placeholder.
                    if (originalUrl && image.dataset.originalFallbackTried !== 'true' && image.src !== new URL(originalUrl, window.location.origin).toString()) {
                      image.dataset.originalFallbackTried = 'true';
                      image.src = originalUrl.startsWith('/') ? API_BASE + originalUrl : originalUrl;
                      return;
                    }
                    image.style.display = 'none';
                    image.parentElement?.querySelector('[data-poster-placeholder="true"]')?.classList.remove('hidden');
                  }}
                  providers={ottProviders}
                  providersAriaLabel={'Streaming services for ' + movie.title}
                  providersFallbackText={
                    !ott || ott.status === 'loading'
                      ? 'Checking streaming services…'
                      : ott.status === 'error'
                        ? 'Streaming availability temporarily unavailable'
                        : ott.status === 'not_confirmed'
                          ? 'Streaming providers not confirmed'
                          : 'No streaming providers reported'
                  }
                />
              );
            })}
          </div>

          {streamingOnly && ottPendingCount > 0 && (
            <div className="rounded-lg border border-slate-800 bg-slate-900 px-3 py-2 text-xs text-slate-400">
              Checking OTT availability in India for {ottPendingCount} more title{ottPendingCount === 1 ? '' : 's'}…
            </div>
          )}
          {streamingOnly && visibleMovieCatalogueItems.length === 0 && ottPendingCount === 0 && (
            <div className="rounded-xl border border-slate-800 bg-slate-900 px-4 py-5 text-sm text-slate-400">
              No titles on this page have confirmed streaming availability in India. Provider data can be incomplete; try another page or turn off this filter.
            </div>
          )}

          <div className="flex items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-900 px-3 py-2">
            <button
              type="button"
              disabled={isSearching || movieCatalogue.page <= 1}
              onClick={() => void openMovieCatalogue(movieCatalogue.catalogue, movieCatalogueSearchText, movieCatalogue.page - 1)}
              className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs font-semibold text-slate-300 hover:border-cyan-500 disabled:opacity-40"
            >
              Previous
            </button>
            <span className="text-xs text-slate-500">Browse all pages to explore older releases too</span>
            <button
              type="button"
              disabled={isSearching || movieCatalogue.page >= movieCatalogue.totalPages}
              onClick={() => void openMovieCatalogue(movieCatalogue.catalogue, movieCatalogueSearchText, movieCatalogue.page + 1)}
              className="rounded-lg bg-cyan-500 px-3 py-2 text-xs font-bold text-slate-950 hover:bg-cyan-400 disabled:opacity-40"
            >
              Next
            </button>
          </div>

          <div className="flex flex-col items-start gap-2 rounded-xl border border-slate-800 bg-slate-900/70 px-3 py-3 sm:flex-row sm:items-center">
            <a href="https://www.themoviedb.org/" target="_blank" rel="noreferrer" className="inline-flex shrink-0 items-center gap-2">
              <img
                src="https://www.themoviedb.org/assets/2/v4/logos/v2/blue_square_1-5bdc75aaebeb75dc7ae79426ddd9be3b2be1e342510f8202baf6bffa71d7f5c4.svg"
                alt="The Movie Database (TMDB)"
                className="h-7 w-7 object-contain"
              />
              <span className="text-xs font-bold text-slate-300">The Movie Database (TMDB)</span>
            </a>
            <p className="text-[10px] leading-relaxed text-slate-500">
              This product uses the TMDB API but is not endorsed or certified by TMDB.
            </p>
          </div>
        </div>
      )}

      {movieTorrentPickerGroups && (
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center bg-black/75 p-3 sm:p-5"
          onMouseDown={event => { if (event.target === event.currentTarget) setMovieTorrentPickerGroups(null); }}
        >
          <section role="dialog" aria-modal="true" aria-labelledby="movie-torrent-picker-title" className="flex max-h-[84vh] w-full max-w-xl flex-col overflow-hidden rounded-2xl border border-slate-700 bg-slate-950 shadow-2xl">
            <header className="flex items-start justify-between gap-3 border-b border-slate-800 px-4 py-3">
              <div className="min-w-0">
                <h2 id="movie-torrent-picker-title" className="text-sm font-bold text-slate-100">Choose a torrent release</h2>
                <p className="mt-1 truncate text-xs text-slate-400">{movieTorrentSearchTitle} · {movieTorrentPickerGroups.length} matching releases</p>
                <p className="mt-1 text-[10px] text-slate-500">List view · qualities are combined within each release</p>
              </div>
              <button type="button" onClick={() => setMovieTorrentPickerGroups(null)} className="shrink-0 rounded-lg p-1.5 text-slate-400 hover:bg-slate-800 hover:text-white" aria-label="Close torrent picker"><X className="h-4 w-4" /></button>
            </header>
            <div className="min-h-0 flex-1 divide-y divide-slate-800 overflow-y-auto">
              {movieTorrentPickerGroups.map(group => {
                const preferred = group.variants[0];
                const qualityNames = group.variants.slice(0, 4).map(variant => torrentQualityDetails(variant).label);
                return (
                  <div key={group.key} className="flex items-center gap-3 px-4 py-3 hover:bg-slate-900/80">
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-semibold leading-snug text-slate-100">{group.title}</div>
                      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px]">
                        <span className="font-semibold text-emerald-400">▲ {Number(preferred.seeders) || 0} seeders</span>
                        <span className="font-semibold text-amber-400">▼ {Number(preferred.leechers) || 0} peers</span>
                        <span className="text-slate-300">{formatBytes(Number(preferred.size) || 0)}</span>
                        {preferred.indexer && <span className="text-slate-500">{preferred.indexer}</span>}
                      </div>
                      <div className="mt-1 text-[10px] text-slate-500">
                        {group.variants.length} quality option{group.variants.length === 1 ? '' : 's'} · {qualityNames.join(', ')}{group.variants.length > 4 ? ', …' : ''}
                      </div>
                    </div>
                    <button type="button" onClick={() => selectMovieTorrentGroup(group)} className="shrink-0 rounded-lg bg-cyan-500 px-3 py-2 text-xs font-bold text-slate-950 hover:bg-cyan-400">
                      Select
                    </button>
                  </div>
                );
              })}
            </div>
            <footer className="border-t border-slate-800 px-4 py-2.5 text-[10px] text-slate-500">
              Select a release to see its full card and quality picker without leaving the movie catalogue.
            </footer>
          </section>
        </div>
      )}

      {(results.length > 0 || regionalWebSeriesLoading || Boolean(regionalWebSeriesError) || Boolean(regionalWebSeriesCatalogue) || (activeTopSection === 'classics' && classicsContentTab !== null)) && (
        <div className="space-y-2">
          <div className="px-1 text-[11px] text-slate-500">Use the subsection buttons above to switch catalogue views.</div>

          <div className="flex items-center justify-between gap-2 px-1">
            <div className="text-xs text-slate-400">
              {groupedResults.length} titles · {sortedResults.length} torrent options
            </div>
          </div>
          {!isSearching && renderFranchisePagination(Math.max(groupedResults.length, catalogueExpectedTitles))}

          {pendingPosterAction?.kind === 'search' && (
            <div className="rounded-xl border border-slate-800 bg-slate-950 px-3 py-2 text-xs text-slate-400">
              Loading the visible posters before opening this card…
            </div>
          )}

          {activeTopSection === 'classics' && classicsContentTab !== 'movies' ? (
            <section className="space-y-3">
              <div className="px-1 text-xs text-slate-400">
                {classicsContentTab === 'web-series'
                  ? 'Classic live-action series · full TMDB details and India OTT availability · playback through NexStream'
                  : 'Classic anime series · full TMDB details and India OTT availability · playback through NexStream'}
              </div>
              {marvelWebSeriesLoading && <div className="rounded-xl border border-slate-800 bg-slate-900 p-4 text-sm text-slate-300">Loading {classicsContentTab === 'anime' ? 'Classics anime' : 'Classics web series'}…</div>}
              {marvelWebSeriesError && <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">{marvelWebSeriesError}</div>}
              {marvelWebSeriesCatalogue && marvelWebSeriesCatalogue.catalogue === (classicsContentTab === 'anime' ? 'classics-anime' : 'classics-web-series') && (
                <>
                  <div className="px-1 text-xs text-slate-400">{marvelWebSeriesCatalogue.totalResults.toLocaleString()} {classicsContentTab === 'anime' ? 'anime titles' : 'web series'} · Metadata by TMDB</div>
                  {renderFranchisePagination(marvelWebSeriesCatalogue.results.length)}
                  <div className="grid grid-cols-2 items-stretch gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 lg:gap-4">
                    {[...marvelWebSeriesCatalogue.results].sort(compareReleaseDateDescending).slice((franchiseGridPage - 1) * franchiseGridPageSize, franchiseGridPage * franchiseGridPageSize).map(show => (
                      <article key={String(show.id)} className="min-w-0 overflow-hidden rounded-xl border border-slate-800 bg-slate-900 transition hover:border-violet-400/50 hover:bg-slate-900/95 flex flex-col">
                        <button type="button" onClick={() => playCatalogueTvWithNexStream(show)} aria-label={`Play ${show.title} with NexStream`} className="relative aspect-[2/3] w-full overflow-hidden bg-slate-950 text-left cursor-pointer">
                          {show.posterUrl ? <img src={lowQualityPosterUrl(show.posterUrl)} alt={show.title} loading="eager" className="h-full w-full object-cover" onError={event => {
                            const image = event.currentTarget;
                            if (image.src.includes('/t/p/w780/')) {
                              image.src = image.src.replace('/t/p/w780/', '/t/p/w185/');
                              return;
                            }
                            // TMDB can return a stale/missing poster path. Retry through
                            // the same-origin title/year poster resolver before showing a blank card.
                            if (image.dataset.posterFallbackTried !== 'true') {
                              image.dataset.posterFallbackTried = 'true';
                              const params = new URLSearchParams({ title: show.title, size: 'w185' });
                              if (show.year) params.set('year', String(show.year));
                              image.src = '/api/poster?' + params.toString();
                              return;
                            }
                            image.style.display = 'none';
                          }} /> : <div className="absolute inset-0 flex items-center justify-center text-slate-600"><Film className="h-8 w-8" /></div>}
                          {show.rating ? <span className="absolute left-2 top-2 rounded-md border border-amber-300/60 bg-slate-950/95 px-2 py-1 text-[10px] font-bold text-amber-300 shadow">⭐ {show.rating.toFixed(1)}/10</span> : null}
                          <span className="absolute bottom-2 right-2 rounded-md border border-violet-300/50 bg-slate-950/90 px-2 py-1 text-[10px] font-bold text-violet-200">▶ NexStream</span>
                        </button>
                        <div className="flex flex-1 flex-col gap-2 p-2.5">
                          <div><h3 className="line-clamp-2 text-sm font-bold leading-snug text-slate-100">{show.title}</h3>
                            <div className="mt-0.5 text-xs text-slate-500">{show.year || 'Year unknown'} · OTT Web Series</div></div>
                          {show.overview && <p className="line-clamp-3 text-[11px] leading-relaxed text-slate-400">{show.overview}</p>}
                          {show.streamingProviders?.length ? <div className="flex flex-wrap gap-1">{uniqueOttProviders(show.streamingProviders.filter(provider => ['subscription','free','ads'].includes(provider.type))).map(provider => <span key={normalizeOttProviderName(provider.name)} className="rounded bg-slate-800 px-1.5 py-1 text-[10px] text-cyan-200">{displayOttProviderName(provider.name)}</span>)}</div> : null}
                        </div>
                      </article>
                    ))}
                  </div>
                  {renderFranchisePagination(marvelWebSeriesCatalogue.results.length)}
                </>
              )}
            </section>
          ) : regionalWebSeriesLoading ? (
            <div className="rounded-xl border border-slate-800 bg-slate-900 p-4 text-sm text-slate-300">Loading web series catalogue…</div>
          ) : regionalWebSeriesError ? (
            <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">{regionalWebSeriesError}</div>
          ) : regionalWebSeriesCatalogue ? (
            <div className="space-y-3">
              <div className="px-1 text-xs text-slate-400">{regionalWebSeriesCatalogue.totalResults.toLocaleString()} web series · Page {regionalWebSeriesCatalogue.page} of {regionalWebSeriesCatalogue.totalPages.toLocaleString()} · Metadata by TMDB</div>
              {(() => {
                const goToRegionalPage = async (targetPage: number) => {
                  if (regionalWebSeriesLoading || targetPage < 1 || targetPage === regionalWebSeriesCatalogue.page) return;
                  setRegionalWebSeriesLoading(true);
                  setRegionalWebSeriesError('');
                  try {
                    const nextPage = await api.getTvCatalogue(regionalWebSeriesCatalogue.catalogue, targetPage);
                    setRegionalWebSeriesCatalogue(nextPage);
                  } catch (error: any) {
                    setRegionalWebSeriesError(String(error?.message || 'Could not load this page.'));
                  } finally {
                    setRegionalWebSeriesLoading(false);
                  }
                };
                return (
                  <div className="flex items-center justify-between gap-3">
                    <button type="button" disabled={regionalWebSeriesLoading || regionalWebSeriesCatalogue.page <= 1} onClick={() => void goToRegionalPage(regionalWebSeriesCatalogue.page - 1)} className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs font-semibold text-slate-200 hover:border-cyan-500 disabled:cursor-not-allowed disabled:opacity-40">← Previous</button>
                    <span className="text-xs text-slate-500">Page {regionalWebSeriesCatalogue.page} of {regionalWebSeriesCatalogue.totalPages}</span>
                    <button type="button" disabled={regionalWebSeriesLoading} onClick={() => void goToRegionalPage(regionalWebSeriesCatalogue.page + 1)} className="rounded-lg border border-cyan-500/50 bg-cyan-500/10 px-3 py-2 text-xs font-semibold text-cyan-200 hover:bg-cyan-500/20 disabled:cursor-not-allowed disabled:opacity-40">Next →</button>
                  </div>
                );
              })()}
              <div className="grid grid-cols-2 items-stretch gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 lg:gap-4">
                {regionalWebSeriesCatalogue.results.map(show => (
                  <article key={String(show.id)} className="min-w-0 overflow-hidden rounded-xl border border-slate-800 bg-slate-900 flex flex-col">
                    <button type="button" onClick={() => playCatalogueTvWithNexStream(show)} aria-label={`Find ${show.title} on NexStream`} className="relative aspect-[2/3] w-full overflow-hidden bg-slate-950 text-left cursor-pointer">
                      {show.posterUrl ? <img src={lowQualityPosterUrl(show.posterUrl)} alt={show.title} loading="eager" className="h-full w-full object-cover" onError={event => {
                            const image = event.currentTarget;
                            if (image.src.includes('/t/p/w780/')) {
                              image.src = image.src.replace('/t/p/w780/', '/t/p/w185/');
                              return;
                            }
                            // TMDB can return a stale/missing poster path. Retry through
                            // the same-origin title/year poster resolver before showing a blank card.
                            if (image.dataset.posterFallbackTried !== 'true') {
                              image.dataset.posterFallbackTried = 'true';
                              const params = new URLSearchParams({ title: show.title, size: 'w185' });
                              if (show.year) params.set('year', String(show.year));
                              image.src = '/api/poster?' + params.toString();
                              return;
                            }
                            image.style.display = 'none';
                          }} /> : <div className="absolute inset-0 flex items-center justify-center text-slate-600"><Film className="h-8 w-8" /></div>}
                      {show.rating ? <span className="absolute left-2 top-2 rounded-md border border-amber-300/60 bg-slate-950/95 px-2 py-1 text-[10px] font-bold text-amber-300">⭐ {show.rating.toFixed(1)}/10</span> : null}
                    </button>
                    <div className="flex flex-1 flex-col gap-2 p-2.5">
                      <div><h3 className="line-clamp-2 text-sm font-bold leading-snug text-slate-100">{show.title}</h3>
                        <div className="mt-0.5 text-xs text-slate-500">{show.year || 'Year unknown'} · OTT Web Series</div></div>
                      {show.overview && <p className="line-clamp-3 text-[11px] leading-relaxed text-slate-400">{show.overview}</p>}
                      {show.streamingProviders?.length ? <div className="flex flex-wrap gap-1">{uniqueOttProviders(show.streamingProviders.filter(provider => ['subscription','free','ads'].includes(provider.type))).map(provider => <span key={normalizeOttProviderName(provider.name)} className="rounded bg-slate-800 px-1.5 py-1 text-[10px] text-cyan-200">{displayOttProviderName(provider.name)}</span>)}</div> : null}
                    </div>
                  </article>
                ))}
              </div>
              {(() => {
                const goToRegionalPage = async (targetPage: number) => {
                  if (regionalWebSeriesLoading || targetPage < 1 || targetPage === regionalWebSeriesCatalogue.page) return;
                  setRegionalWebSeriesLoading(true);
                  setRegionalWebSeriesError('');
                  try {
                    const nextPage = await api.getTvCatalogue(regionalWebSeriesCatalogue.catalogue, targetPage);
                    setRegionalWebSeriesCatalogue(nextPage);
                  } catch (error: any) {
                    setRegionalWebSeriesError(String(error?.message || 'Could not load this page.'));
                  } finally {
                    setRegionalWebSeriesLoading(false);
                  }
                };
                return (
                  <div className="flex items-center justify-between gap-3">
                    <button type="button" disabled={regionalWebSeriesLoading || regionalWebSeriesCatalogue.page <= 1} onClick={() => void goToRegionalPage(regionalWebSeriesCatalogue.page - 1)} className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs font-semibold text-slate-200 hover:border-cyan-500 disabled:cursor-not-allowed disabled:opacity-40">← Previous</button>
                    <span className="text-xs text-slate-500">Page {regionalWebSeriesCatalogue.page} of {regionalWebSeriesCatalogue.totalPages}</span>
                    <button type="button" disabled={regionalWebSeriesLoading} onClick={() => void goToRegionalPage(regionalWebSeriesCatalogue.page + 1)} className="rounded-lg border border-cyan-500/50 bg-cyan-500/10 px-3 py-2 text-xs font-semibold text-cyan-200 hover:bg-cyan-500/20 disabled:cursor-not-allowed disabled:opacity-40">Next →</button>
                  </div>
                );
              })()}
            </div>
          ) : showDcWebSeries ? (
            <div className="space-y-3">
              {dcWebSeriesLoading && <div className="rounded-xl border border-slate-800 bg-slate-900 p-4 text-sm text-slate-300">Loading DC web series…</div>}
              {dcWebSeriesError && <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">{dcWebSeriesError}</div>}
              {dcWebSeriesCatalogue && (
                <>
                  <div className="px-1 text-xs text-slate-400">{dcWebSeriesCatalogue.totalResults.toLocaleString()} DC web series · Page {dcWebSeriesCatalogue.page} of {dcWebSeriesCatalogue.totalPages.toLocaleString()} · Metadata by TMDB</div>
                  {renderFranchisePagination(dcWebSeriesCatalogue.results.length)}
                  <div className="grid grid-cols-2 items-stretch gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 lg:gap-4">
                    {[...dcWebSeriesCatalogue.results].sort(compareReleaseDateDescending).slice((franchiseGridPage - 1) * franchiseGridPageSize, franchiseGridPage * franchiseGridPageSize).map(show => (
                      <article key={String(show.id)} className="min-w-0 overflow-hidden rounded-xl border border-slate-800 bg-slate-900 transition hover:border-cyan-500/40 hover:bg-slate-900/95 flex flex-col">
                        <button type="button" onClick={() => playCatalogueTvWithNexStream(show)} aria-label={`Find ${show.title} on NexStream`} className="relative aspect-[2/3] w-full overflow-hidden bg-slate-950 text-left cursor-pointer">
                          {show.posterUrl ? <img src={lowQualityPosterUrl(show.posterUrl)} alt={show.title} loading="eager" className="h-full w-full object-cover" onError={event => {
                            const image = event.currentTarget;
                            if (image.src.includes('/t/p/w780/')) {
                              image.src = image.src.replace('/t/p/w780/', '/t/p/w185/');
                              return;
                            }
                            // TMDB can return a stale/missing poster path. Retry through
                            // the same-origin title/year poster resolver before showing a blank card.
                            if (image.dataset.posterFallbackTried !== 'true') {
                              image.dataset.posterFallbackTried = 'true';
                              const params = new URLSearchParams({ title: show.title, size: 'w185' });
                              if (show.year) params.set('year', String(show.year));
                              image.src = '/api/poster?' + params.toString();
                              return;
                            }
                            image.style.display = 'none';
                          }} /> : <div className="absolute inset-0 flex items-center justify-center text-slate-600"><Film className="h-8 w-8" /></div>}
                          {show.rating ? <span className="absolute left-2 top-2 rounded-md border border-amber-300/60 bg-slate-950/95 px-2 py-1 text-[10px] font-bold text-amber-300 shadow">⭐ {show.rating.toFixed(1)}/10</span> : null}
                        </button>
                        <div className="flex flex-1 flex-col gap-2 p-2.5">
                          <div><h3 className="line-clamp-2 text-sm font-bold leading-snug text-slate-100">{show.title}</h3>
                            <div className="mt-0.5 text-xs text-slate-500">{show.year || 'Year unknown'} · OTT Web Series</div></div>
                          {show.overview && <p className="line-clamp-3 text-[11px] leading-relaxed text-slate-400">{show.overview}</p>}
                          {show.streamingProviders?.length ? <div className="flex flex-wrap gap-1">{uniqueOttProviders(show.streamingProviders.filter(provider => ['subscription','free','ads'].includes(provider.type))).map(provider => <span key={normalizeOttProviderName(provider.name)} className="rounded bg-slate-800 px-1.5 py-1 text-[10px] text-cyan-200">{displayOttProviderName(provider.name)}</span>)}</div> : null}
                        </div>
                      </article>
                    ))}
                  </div>
                  {renderFranchisePagination(dcWebSeriesCatalogue.results.length)}
                  {dcWebSeriesCatalogue.page < dcWebSeriesCatalogue.totalPages && <button type="button" disabled={dcWebSeriesLoading} onClick={async () => { setDcWebSeriesLoading(true); setDcWebSeriesError(''); try { const nextPage = await api.getTvCatalogue('dc-live-action', dcWebSeriesCatalogue.page + 1); setDcWebSeriesCatalogue(current => current ? { ...nextPage, results: [...current.results, ...nextPage.results.filter(item => !current.results.some(existing => String(existing.id) === String(item.id)))].sort(compareReleaseDateDescending) } : { ...nextPage, results: [...nextPage.results].sort(compareReleaseDateDescending) }); } catch (error: any) { setDcWebSeriesError(String(error?.message || 'Could not load more series.')); } finally { setDcWebSeriesLoading(false); } }} className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs font-semibold text-slate-200 hover:border-cyan-500 disabled:opacity-50">Load more</button>}
                </>
              )}
            </div>
          ) : showMarvelWebSeries ? (
            <div className="space-y-3">
              {marvelWebSeriesLoading && <div className="rounded-xl border border-slate-800 bg-slate-900 p-4 text-sm text-slate-300">Loading {activeTopSection === 'classics' ? (classicsContentTab === 'anime' ? 'Classics anime' : 'Classics web series') : 'Marvel web series'}…</div>}
              {marvelWebSeriesError && <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">{marvelWebSeriesError}</div>}
              {marvelWebSeriesCatalogue && (
                <>
                  <div className="px-1 text-xs text-slate-400">{marvelWebSeriesCatalogue.totalResults.toLocaleString()} {activeTopSection === 'classics' ? (classicsContentTab === 'anime' ? 'Classics anime' : 'Classics web series') : 'Marvel web series'} · Page {marvelWebSeriesCatalogue.page} of {marvelWebSeriesCatalogue.totalPages.toLocaleString()} · Metadata by TMDB</div>
                  <div className="grid grid-cols-2 items-stretch gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 lg:gap-4">
                    {[...marvelWebSeriesCatalogue.results]
                      .filter(show => activeTopSection !== 'marvel' || !['iron man and his awesome friends', 'moon girl and devil dinosaur', 'moon girl and the devil dinosaur', 'spidey and his amazing friends'].includes(show.title.trim().toLowerCase()))
                      .sort(compareReleaseDateDescending)
                      .slice((franchiseGridPage - 1) * franchiseGridPageSize, franchiseGridPage * franchiseGridPageSize)
                      .map(show => (
                      <article key={String(show.id)} className="min-w-0 overflow-hidden rounded-xl border border-slate-800 bg-slate-900 transition hover:border-cyan-500/40 hover:bg-slate-900/95 flex flex-col">
                        <button type="button" onClick={() => playCatalogueTvWithNexStream(show)} aria-label={`Find ${show.title} on NexStream`} className="relative aspect-[2/3] w-full overflow-hidden bg-slate-950 text-left cursor-pointer">
                          {show.posterUrl ? <img src={lowQualityPosterUrl(show.posterUrl)} alt={show.title} loading="eager" className="h-full w-full object-cover" onError={event => {
                            const image = event.currentTarget;
                            if (image.src.includes('/t/p/w780/')) {
                              image.src = image.src.replace('/t/p/w780/', '/t/p/w185/');
                              return;
                            }
                            // TMDB can return a stale/missing poster path. Retry through
                            // the same-origin title/year poster resolver before showing a blank card.
                            if (image.dataset.posterFallbackTried !== 'true') {
                              image.dataset.posterFallbackTried = 'true';
                              const params = new URLSearchParams({ title: show.title, size: 'w185' });
                              if (show.year) params.set('year', String(show.year));
                              image.src = '/api/poster?' + params.toString();
                              return;
                            }
                            image.style.display = 'none';
                          }} /> : <div className="absolute inset-0 flex items-center justify-center text-slate-600"><Film className="h-8 w-8" /></div>}
                          {show.rating ? <span className="absolute left-2 top-2 rounded-md border border-amber-300/60 bg-slate-950/95 px-2 py-1 text-[10px] font-bold text-amber-300 shadow">⭐ {show.rating.toFixed(1)}/10</span> : null}
                        </button>
                        <div className="flex flex-1 flex-col gap-2 p-2.5">
                          <div><h3 className="line-clamp-2 text-sm font-bold leading-snug text-slate-100">{show.title}</h3>
                            <div className="mt-0.5 text-xs text-slate-500">{show.year || 'Year unknown'} · OTT Web Series</div></div>
                          {show.overview && <p className="line-clamp-3 text-[11px] leading-relaxed text-slate-400">{show.overview}</p>}
                          {show.streamingProviders?.length ? <div className="flex flex-wrap gap-1">{uniqueOttProviders(show.streamingProviders.filter(provider => ['subscription','free','ads'].includes(provider.type))).map(provider => <span key={normalizeOttProviderName(provider.name)} className="rounded bg-slate-800 px-1.5 py-1 text-[10px] text-cyan-200">{displayOttProviderName(provider.name)}</span>)}</div> : null}
                        </div>
                      </article>
                    ))}
                  </div>
                  {renderFranchisePagination(marvelWebSeriesCatalogue.results.length)}
                  {marvelWebSeriesCatalogue.page < marvelWebSeriesCatalogue.totalPages && <button type="button" disabled={marvelWebSeriesLoading} onClick={async () => { setMarvelWebSeriesLoading(true); setMarvelWebSeriesError(''); try { const nextPage = await api.getTvCatalogue('marvel', marvelWebSeriesCatalogue.page + 1); setMarvelWebSeriesCatalogue(current => current ? { ...nextPage, page: nextPage.page, results: [...current.results, ...nextPage.results.filter(item => !current.results.some(existing => String(existing.id) === String(item.id)))].sort(compareReleaseDateDescending) } : nextPage); } catch (error: any) { setMarvelWebSeriesError(String(error?.message || 'Could not load more series.')); } finally { setMarvelWebSeriesLoading(false); } }} className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs font-semibold text-slate-200 hover:border-cyan-500 disabled:opacity-50">Load more</button>}
                </>
              )}
            </div>
          ) : (
          <>
          {!movieCatalogue && movieTorrentPreviewGroup && (
            <section className="mb-3 overflow-hidden rounded-2xl border border-cyan-500/40 bg-slate-950 shadow-lg">
              <div className="flex items-start justify-between gap-3 border-b border-slate-800 px-4 py-3">
                <div className="min-w-0">
                  <div className="text-[10px] font-bold uppercase tracking-wider text-cyan-300">Prepare card</div>
                  <h3 className="mt-1 truncate text-sm font-bold text-slate-100">{movieTorrentPreviewGroup.title}</h3>
                  <p className="mt-1 text-[11px] text-slate-400">Choose a release quality, then prepare it or try NexStream.</p>
                </div>
                <button type="button" onClick={() => { setMovieTorrentPreviewGroup(null); setMovieTorrentPreviewCatalogueId(null); setMovieTorrentSearchError(''); }} className="shrink-0 rounded-lg px-2 py-1 text-xs text-slate-400 hover:bg-slate-800 hover:text-white" aria-label="Close Prepare card">✕</button>
              </div>
              {(() => {
                const group = movieTorrentPreviewGroup;
                const selectedKey = selectedMovieTorrentQualityByGroup[group.key] || torrentResultKey(group.variants[0]);
                const result = group.variants.find(variant => torrentResultKey(variant) === selectedKey) || group.variants[0];
                const source = result.magnetUrl || result.downloadUrl || result.sourceUrl;
                const torrentKey = result.infoHash || source || result.title;
                const isPreparing = preparingTorrentKey === torrentKey;
                const preparedFiles = preparedForResult(result);
                const primaryFile = preparedFiles.find(file => /\\.(mkv|mp4|m4v|webm|mov|avi|m3u8|ts|mp3|wav|flac|aac|ogg|m4a)$/i.test(file.name)) || preparedFiles[0];
                const qualityLabel = torrentQualityDetails(result).label;
                return (
                  <div className="grid gap-3 p-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                    <div className="min-w-0">
                      {group.variants.length > 1 ? (
                        <select aria-label={'Choose quality for ' + group.title} value={torrentResultKey(result)} onChange={event => setSelectedMovieTorrentQualityByGroup(previous => ({ ...previous, [group.key]: event.target.value }))} className="w-full rounded-lg border border-slate-800 bg-slate-900 px-3 py-2 text-xs font-medium text-slate-200 outline-none focus:border-cyan-500">
                          {group.variants.map(variant => <option key={torrentResultKey(variant)} value={torrentResultKey(variant)}>{torrentQualityDetails(variant).label} · {formatBytes(Number(variant.size) || 0)} · ▲{Number(variant.seeders) || 0}{variant.indexer ? ' · ' + variant.indexer : ''}</option>)}
                        </select>
                      ) : <div className="text-xs font-semibold text-slate-200">{qualityLabel}</div>}
                      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-slate-400">
                        <span>{formatBytes(Number(result.size) || 0)}</span><span className="text-emerald-400">▲ {Number(result.seeders) || 0} seeders</span><span className="text-amber-400">▼ {Number(result.leechers) || 0} peers</span>{result.indexer && <span>{result.indexer}</span>}
                      </div>
                    </div>
                    <div className="flex flex-wrap items-center gap-2 sm:justify-end">
                      {preparedFiles.length > 0 && primaryFile ? (
                        <button type="button" onClick={async () => { setPlayingTorrentKey(torrentKey); try { await onPlaySeedrFile?.(primaryFile); } finally { setPlayingTorrentKey(current => current === torrentKey ? null : current); } }} disabled={playingTorrentKey === torrentKey} className="rounded-lg bg-emerald-400 px-4 py-2 text-xs font-bold text-slate-950 disabled:opacity-60">{playingTorrentKey === torrentKey ? 'Opening…' : 'Play'}</button>
                      ) : (
                        <button type="button" onClick={() => void prepareMovieTorrent(result)} disabled={!source || isPreparing} className="rounded-lg bg-cyan-500 px-4 py-2 text-xs font-bold text-slate-950 hover:bg-cyan-400 disabled:opacity-50">{isPreparing ? 'Preparing…' : 'Prepare'}</button>
                      )}
                      <button type="button" disabled={!Number(movieTorrentPreviewGroup.posterResult?.tmdbId || movieTorrentPreviewGroup.posterResult?.id) || nexStreamResolvingId === 'movie:' + String(movieTorrentPreviewGroup.posterResult?.tmdbId || movieTorrentPreviewGroup.posterResult?.id)} onClick={() => { const item = movieTorrentPreviewGroup.posterResult; const tmdbId = Number(item?.tmdbId || item?.id); if (!Number.isInteger(tmdbId) || tmdbId <= 0) { setNexStreamError('This catalogue card has no valid TMDB ID for NexStream.'); return; } void playWithNexStream({ id: tmdbId, title: item.title || movieTorrentPreviewGroup.title, date: item.releaseDate || (item.year ? String(item.year) : ''), overview: item.overview || '', poster: item.posterUrl || null, type: 'movie' }); }} className="rounded-lg border border-violet-400/50 bg-violet-500/15 px-4 py-2 text-xs font-bold text-violet-200 hover:bg-violet-500/25 disabled:opacity-50">{nexStreamResolvingId.startsWith('movie:') ? 'Resolving…' : 'NexStream'}</button>
                    </div>
                  </div>
                );
              })()}
            </section>
          )}
          <div
            className="grid grid-cols-2 items-stretch gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 lg:rounded-2xl lg:border-0 lg:bg-transparent lg:gap-4 lg:w-full lg:max-w-none lg:mx-0 lg:p-0 lg:divide-y-0 lg:overflow-visible"
          >
            {groupedResults.slice((franchiseGridPage - 1) * franchiseGridPageSize, franchiseGridPage * franchiseGridPageSize).map((group, index) => {
              const selectedKey = selectedQualityByGroup[group.key];
              const result = group.variants.find(variant => torrentResultKey(variant) === selectedKey) || group.variants[0];
              const posterResult = group.posterResult;
              const normalizedCardTitle = normalizeTorrentTitleForNexStream(group.title);
              const cardYear = Number((group.title.match(/\\b((?:19|20)\\d{2})\\b/) || [])[1] || group.year || 0);
              const titleMatches = nexStreamResults.filter(item => {
                if (item.type !== 'movie') return false;
                const candidateTitle = normalizeTorrentTitleForNexStream(item.title);
                const exactTitle = candidateTitle === normalizedCardTitle;
                const titleContains = candidateTitle.length >= 4 && normalizedCardTitle.length >= 4 &&
                  (normalizedCardTitle.startsWith(candidateTitle + ' ') || candidateTitle.startsWith(normalizedCardTitle + ' '));
                const yearMatches = !cardYear || !item.date || Number(String(item.date).slice(0, 4)) === cardYear;
                return (exactTitle || titleContains) && yearMatches;
              }).slice(0, 1);
              const seriesTitle = episodeSeriesTitle(group.title);
              const mediaMetadata = searchMetadataForGroup(group, searchMetadata);
              const mediaDisplayTitle = !seriesTitle && mediaMetadata?.title
                ? mediaMetadata.title
                : group.title.replace(/\s*\((?:19|20)\d{2}\)\s*$/, '');
              const mediaDisplayYear = !seriesTitle ? (mediaMetadata?.year || group.year) : group.year;
              const imdbDisplayTitle = seriesTitle ? (mediaMetadata?.title || seriesTitle) : mediaDisplayTitle;
              const imdbDisplayYear = seriesTitle ? mediaMetadata?.year : mediaDisplayYear;
              const imdbDisplayType = seriesTitle ? 'tv' as const : (mediaMetadata?.mediaType === 'tv' ? 'tv' as const : 'movie' as const);
              const displayImdbRating =
                imdbRatingsByKey[imdbRatingKey(imdbDisplayTitle, imdbDisplayYear, imdbDisplayType)] ||
                imdbRatingsByKey[imdbRatingKey(mediaMetadata?.title || imdbDisplayTitle, mediaMetadata?.year || imdbDisplayYear, imdbDisplayType)] ||
                imdbRatingsByKey[imdbRatingKey(group.title, group.year, imdbDisplayType)];
              const imdbTitleHref = imdbTitleLink(displayImdbRating, imdbDisplayTitle || mediaDisplayTitle, imdbDisplayYear);
              const groupPosterKey = posterKeyFor(posterResult);
              // Never trust an unrelated catalogue/indexer poster. Use the title-and-year
              // resolver result (exact title match) or the card's own metadata poster.
              // While that resolver is still working, show the poster of the same TMDB
              // title the card's synopsis comes from, so no card sits blank.
              // TMDB-id based metadata poster first (same title as the synopsis), then the
              // title+year resolver. If the metadata poster fails to load, the resolver takes over.
              const loadedSearchPosterUrl = searchPosterUrlsRef.current[groupPosterKey] || '';
              const metadataPosterUrl = mediaMetadata?.posterUrl && !metadataPosterFailed[groupPosterKey]
                ? mediaMetadata.posterUrl
                : '';
              // Give the image an immediate same-origin resolver URL instead of
              // waiting for the background JSON lookup to finish before loading
              // its first thumbnail. The backend's poster map makes common
              // title/year lookups a fast redirect to the image CDN.
              const sharedPosterUrl = loadedSearchPosterUrl ||
                metadataPosterUrl ||
                posterOverrides[groupPosterKey] ||
                posterResult?.posterUrl ||
                posterImageUrlForResult(posterResult);

              // Normal searches use a poster-first two-state card. Clicking the poster
              // opens the Prepare/details card; clicking it again returns here.
              if (expandedSearchGroupKey !== group.key) {
                const availability = searchAvailabilityByGroup[group.key];
                const providerMatchTitle = episodeSeriesTitle(group.title) || group.title;
                const groupTitleKey = normalizeSavedOttTitle(providerMatchTitle);
                const matchingSavedTitles = (availability?.matches || []).filter(match =>
                  Array.isArray(match.providers) &&
                  match.providers.length > 0 &&
                  normalizeSavedOttTitle(String(match.title || '')) === groupTitleKey
                );
                const compactProviders = uniqueOttProviders(
                  matchingSavedTitles.length > 0
                    ? matchingSavedTitles.flatMap(match => match.providers)
                    : (availability?.providers || [])
                );
                return (
                  <MovieCompactCard
                    key={group.key || (group.title + '-' + index)}
                    title={mediaDisplayTitle}
                    titleHref={imdbTitleHref}
                    year={mediaDisplayYear}
                    imdb={displayImdbRating}
                    tmdbRating={mediaMetadata?.rating}
                    overview={
                      mediaMetadata?.overview ||
                      (searchMetadataStatus === 'loading'
                        ? 'Loading synopsis from TMDB…'
                        : searchMetadataStatus === 'unavailable'
                          ? 'TMDB synopsis is temporarily unavailable.'
                          : 'No synopsis available from TMDB for this title.')
                    }
                    posterSrc={lowQualityPosterUrl(sharedPosterUrl)}
                    posterAlt={mediaDisplayTitle}
                    onPosterClick={() => {
                      if (pendingPosterAction) return;
                      if (!posterFirstPageReadyRef.current) {
                        setPendingPosterAction({
                          kind: 'search',
                          groupKey: group.key,
                          page: franchiseGridPage,
                        });
                        return;
                      }
                      setExpandedSearchGroupKey(group.key);
                    }}
                    onPosterLoad={event => {
                      const key = posterKeyFor(posterResult);
                      searchPosterUrlsRef.current[key] = event.currentTarget.currentSrc || event.currentTarget.src;
                      posterLoadedRef.current.add(key);
                      event.currentTarget.style.display = '';
                      event.currentTarget.parentElement?.querySelector('[data-poster-placeholder="true"]')?.classList.add('hidden');
                    }}
                    onPosterError={event => {
                      const image = event.currentTarget;
                      // Do not fall back to catalogue artwork here: metadata sources can
                      // attach a poster for a different title/remake. Retry the strict
                      // title-and-year resolver instead.
                      const key = posterKeyFor(posterResult);
                      delete searchPosterUrlsRef.current[key];
                      posterLoadedRef.current.delete(key);
                      setMetadataPosterFailed(previous => previous[key] ? previous : { ...previous, [key]: true });
                      image.style.display = 'none';
                      image.parentElement?.querySelector('[data-poster-placeholder="true"]')?.classList.remove('hidden');

                      // Match the expanded card's retry path; do not leave a blank black poster
                      // slot just because the first image request failed.
                      if (!posterBackgroundQueuedRef.current.has(key)) {
                        posterBackgroundQueuedRef.current.add(key);
                        posterBackgroundQueueRef.current.unshift({ key, result: posterResult });
                        startPosterBackgroundWorkers();
                      }
                    }}
                    providers={toCardProviders(compactProviders)}
                    providersAriaLabel={'Streaming services for ' + group.title}
                    providersFallbackText={
                      availability?.status === 'loading'
                        ? 'Checking streaming services…'
                        : availability?.status === 'unavailable'
                          ? 'Streaming availability temporarily unavailable'
                          : availability?.status === 'not_confirmed'
                            ? 'Streaming providers not confirmed'
                            : availability?.status === 'ready'
                              ? 'No streaming providers reported'
                              : searchMetadataStatus === 'loading'
                                ? 'Checking streaming availability…'
                                : 'Streaming providers not listed'
                    }
                  />
                );
              }

              const prepareState = prepareStateFor(result);
              return (
                <MoviePrepareCard
                  key={group.key || (group.title + '-' + index)}
                  className="z-0"
                  posterSrc={loadedSearchPosterUrl || lowQualityPosterUrl(sharedPosterUrl)}
                  posterAlt={group.title}
                  providers={toCardProviders(uniqueOttProviders(
                    (searchAvailabilityByGroup[group.key]?.matches || [])
                      .filter(match => Array.isArray(match.providers) && match.providers.length > 0 &&
                        normalizeSavedOttTitle(String(match.title || '')) === normalizeSavedOttTitle(episodeSeriesTitle(group.title) || group.title))
                      .flatMap(match => match.providers)
                  ))}
                  onPosterClick={() => setExpandedSearchGroupKey(null)}
                  onPosterLoad={event => {
                    const key = posterKeyFor(posterResult);
                    searchPosterUrlsRef.current[key] = event.currentTarget.currentSrc || event.currentTarget.src;
                    posterLoadedRef.current.add(key);
                    event.currentTarget.style.display = '';
                    event.currentTarget.parentElement?.querySelector('[data-poster-placeholder="true"]')?.classList.add('hidden');
                  }}
                  onPosterError={event => {
                    const key = posterKeyFor(posterResult);
                    delete searchPosterUrlsRef.current[key];
                    posterLoadedRef.current.delete(key);
                    setMetadataPosterFailed(previous => previous[key] ? previous : { ...previous, [key]: true });
                    event.currentTarget.style.display = 'none';
                    event.currentTarget.parentElement?.querySelector('[data-poster-placeholder="true"]')?.classList.remove('hidden');

                    if (
                      !posterBackgroundQueuedRef.current.has(key) &&
                      !posterOverrides[key]
                    ) {
                      posterBackgroundQueuedRef.current.add(key);
                      posterBackgroundQueueRef.current.unshift({ key, result: posterResult });
                      startPosterBackgroundWorkers();
                    }
                  }}
                  imdb={displayImdbRating}
                  seeders={result.seeders}
                  indexer={result.indexer}
                  quality={extractedQuality(result)}
                  title={mediaDisplayTitle}
                  titleHref={imdbTitleHref}
                  year={mediaMetadata?.year || group.year}
                  onClose={() => setExpandedSearchGroupKey(null)}
                  variants={group.variants.map(variant => {
                    const quality = torrentQualityDetails(variant);
                    const provider = String(variant.indexer || '').trim();
                    const size = formatBytes(Number(variant.size) || 0);
                    const seeds = Number(variant.seeders) || 0;
                    return {
                      key: torrentResultKey(variant),
                      label: `${quality.label} · ${size} · ▲${seeds}${provider ? ` · ${provider}` : ''}`,
                    };
                  })}
                  selectedVariantKey={torrentResultKey(result)}
                  onSelectVariant={key => setSelectedQualityByGroup(previous => ({ ...previous, [group.key]: key }))}
                  variantsAriaLabel={`Choose quality for ${group.title}`}
                  prepared={prepareState.primaryFile ? preparedActionsFor(prepareState.torrentKey, prepareState.primaryFile) : null}
                  directStream={{
                    disabled: nexStreamResolvingId !== null || (nexStreamLoading && normalizeGroupTitle(nexStreamLoadingTitle) === normalizeGroupTitle(mediaDisplayTitle || group.title)),
                    label: nexStreamResolvingId !== null
                      ? 'Resolving…'
                      : nexStreamLoading && normalizeGroupTitle(nexStreamLoadingTitle) === normalizeGroupTitle(mediaDisplayTitle || group.title)
                        ? 'Searching…'
                        : 'Direct Stream',
                    onClick: () => {
                      if (titleMatches.length > 0) {
                        void playWithNexStream(titleMatches[0]);
                        return;
                      }
                      const tmdbId = Number(mediaMetadata?.tmdbId || mediaMetadata?.id);
                      if (Number.isInteger(tmdbId) && tmdbId > 0) {
                        void playWithNexStream({
                          id: tmdbId,
                          title: mediaDisplayTitle || group.title,
                          date: mediaMetadata?.year || group.year,
                          overview: mediaMetadata?.overview || '',
                          poster: mediaMetadata?.posterUrl || null,
                          type: 'movie',
                        });
                      } else {
                        void searchNexStreamTitles(mediaDisplayTitle || group.title, 'movie');
                      }
                    },
                  }}
                  prepare={{
                    isPreparing: prepareState.isPreparing,
                    disabled: !prepareState.source || prepareState.isPreparing,
                    onClick: () => void prepareMovieTorrent(result),
                  }}
                />
              );
            })}
          </div>
          {!isSearching && renderFranchisePagination(Math.max(groupedResults.length, catalogueExpectedTitles))}
          </>
          )}
        </div>
      )}

      {!movieCatalogue && !isSearching && searched && groupedResults.length === 0 && !error && (
        <div className="py-14 text-center rounded-2xl bg-slate-900 border border-slate-800">
          <Search className="w-10 h-10 text-slate-700 mx-auto mb-3" />
          <h3 className="text-sm font-bold text-slate-300">
            {results.length > 0 ? 'No results match your filters' : 'No results'}
          </h3>
          <p className="text-xs text-slate-500 mt-1">
            {results.length > 0
              ? 'Try a different resolution or sorting filter.'
              : 'Try a broader search term or change your filters.'}
          </p>
        </div>
      )}
    </div>
  );
};
