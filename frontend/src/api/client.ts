export interface TorrentSearchResult {
  guid?: string;
  title: string;
  mediaTitle?: string;
  size: number;
  seeders: number;
  leechers: number;
  indexer?: string;
  protocol?: string;
  publishDate?: string;
  infoHash?: string;
  magnetUrl?: string;
  downloadUrl?: string;
  infoUrl?: string;
  sourceUrl?: string;
  descriptorUrl?: string;
  year?: number | string | null;
  releaseDate?: string;
  rating?: number | null;
  genres?: string[];
  posterUrl?: string;
  quality?: string;
}


export interface MarvelCatalogueResponse {
  status: 'idle' | 'building' | 'ready' | 'failed';
  results: TorrentSearchResult[];
  completed: number;
  total: number;
  resultCount?: number;
  builtAt?: string;
  error?: string;
  refreshing?: boolean;
  page?: number;
  pageSize?: number;
  totalPages?: number;
  totalMovies?: number;
}

export interface TvCatalogueItem {
  id: number | string;
  title: string;
  year?: number | null;
  releaseDate?: string;
  overview?: string;
  posterUrl?: string;
  backdropUrl?: string;
  rating?: number;
  tmdbUrl?: string;
  streamingProviders?: Array<{
    providerId?: number | null;
    name: string;
    type: 'subscription' | 'free' | 'ads' | 'rent' | 'buy' | string;
    region?: string;
    logoUrl?: string;
    webUrl?: string;
  }>;
  providerLink?: string;
  source?: string;
}

export interface TvCataloguePage {
  catalogue: string;
  page: number;
  totalPages: number;
  totalResults: number;
  provider: string;
  results: TvCatalogueItem[];
}

export type MovieCatalogueKey =
  | 'marvel'
  | 'dc-live-action'
  | 'dc-animated'
  | 'latest-hollywood'
  | 'latest-bollywood'
  | 'popular-hollywood'
  | 'popular-bollywood'
  | 'trending-hollywood'
  | 'trending-bollywood'
  | 'bollywood-library';

export interface MovieCatalogueItem {
  id: number | string;
  title: string;
  year?: number | null;
  releaseDate?: string;
  overview?: string;
  posterUrl?: string;
  backdropUrl?: string;
  rating?: number;
  genres?: number[];
  source?: string;
  tmdbId?: number | string;
  trendingWeek?: string;
  trendingRank?: number;
  trendingWeeks?: string[];
  nexStreamPlayable?: boolean;
  nexStreamCheckedAt?: string;
  imdbRatingStatus?: ImdbRatingResult['status'];
  imdbRating?: number | null;
  imdbVotes?: string | null;
  imdbId?: string | null;
  imdbCheckedAt?: number | string | null;
  verified?: boolean;
  tmdbUrl?: string;
  imdbUrl?: string;
  torrentMatch?: {
    title?: string;
    indexer?: string;
    infoUrl?: string;
    magnetUrl?: string;
    infoHash?: string;
    size?: number;
    seeders?: number;
    leechers?: number;
    matchScore?: number;
  };
  torrentMatches?: Array<{
    title?: string;
    indexer?: string;
    infoUrl?: string;
    magnetUrl?: string;
    infoHash?: string;
    size?: number;
    seeders?: number;
    leechers?: number;
    matchScore?: number;
  }>;
  ottAvailability?: {
    region?: string;
    status?: string;
    streamingProviders?: MovieOttProvider[];
    rentProviders?: MovieOttProvider[];
    buyProviders?: MovieOttProvider[];
    providerLink?: string;
    checkedAt?: string;
    source?: string;
    providerAttribution?: string;
    attribution?: string;
  };
}

export interface SearchMediaMetadata {
  id: number | string;
  tmdbId?: number | string;
  title: string;
  year?: number | null;
  mediaType: 'movie' | 'tv';
  overview?: string;
  posterUrl?: string;
  backdropUrl?: string;
  rating?: number;
  tmdbUrl: string;
  source?: string;
}

export interface SearchMediaMetadataResponse {
  status: 'ready' | 'not_found' | 'unavailable';
  query: string;
  source?: string;
  results: SearchMediaMetadata[];
}

export interface ImdbRatingResult {
  query?: string;
  title: string;
  year?: number | null;
  mediaType?: 'movie' | 'tv';
  status: 'found' | 'not_found' | 'unavailable' | 'rate_limited';
  imdbRating?: number | null;
  imdbVotes?: string | null;
  imdbId?: string | null;
  imdbUrl?: string | null;
}

export interface MovieCataloguePage {
  catalogue: MovieCatalogueKey;
  provider: string;
  page: number;
  totalPages: number;
  totalResults: number;
  releasedOnly?: boolean;
  maxSizeGb?: number;
  results: MovieCatalogueItem[];
  attribution?: string;
}


export type MovieOttStatus =
  | 'streaming'
  | 'rent_buy'
  | 'digital_scheduled'
  | 'digital_release_known'
  | 'not_confirmed';

export interface MovieOttProvider {
  providerId?: number | string | null;
  name: string;
  logoUrl?: string;
  webUrl?: string;
}

export interface MovieOttAvailability {
  movieId: number;
  region: string;
  status: MovieOttStatus;
  streamingProviders: MovieOttProvider[];
  rentProviders: MovieOttProvider[];
  buyProviders: MovieOttProvider[];
  theatricalReleaseDate?: string | null;
  digitalReleaseDate?: string | null;
  providerLink: string;
  checkedAt: string;
  source: string;
  providerAttribution: string;
  attribution: string;
  stale?: boolean;
  cacheWarning?: string;
}

import {
  TorrentItem,
  TorrentFileItem,
  StorageFile,
  StorageFolder,
  UserProfile,
  StorageStats,
  ActivityLog,
  AppNotification,
  CleanupSettings,
  QbtSettings
} from '../types/index.ts';

// Home is a VPS-only all-in-one deployment: the browser talks directly
// to the same origin on the Oracle VPS for both the React UI and FastAPI.
// Keep VITE_API_URL only as an optional override for local development.
const configuredApiBase = String(import.meta.env.VITE_API_URL || '').trim();
export const API_BASE = (
  configuredApiBase ||
  (typeof window !== 'undefined' ? window.location.origin : 'http://127.0.0.1:10000')
).replace(/\/+$/, '');
const makeSeedrError = (data: any, body: string, status: number, fallback: string) => {
  const error = new Error(
    data?.error ||
    data?.message ||
    data?.detail ||
    body ||
    fallback
  ) as Error & { code?: string; status?: number };
  error.code = typeof data?.code === 'string' ? data.code : undefined;
  error.status = status;
  return error;
};

let seedrCsrfToken = '';

const apiFetch = (input: RequestInfo | URL, init?: RequestInit) => {
  const value = String(input);
  const url = value.startsWith('/') ? API_BASE + value : value;
  const method = String(init?.method || 'GET').toUpperCase();
  // Some callers pass an absolute URL (API_BASE + '/api/...'); normalise to a
  // path so the CSRF header is attached to every state-changing API request.
  const path = value.startsWith('/')
    ? value
    : API_BASE && value.startsWith(API_BASE)
      ? value.slice(API_BASE.length)
      : value;
  const isApiMutation =
    path.startsWith('/api/') &&
    !['GET', 'HEAD', 'OPTIONS'].includes(method);

  const headers = new Headers(init?.headers || undefined);
  if (isApiMutation && seedrCsrfToken) {
    headers.set('X-Torrent-Studio-CSRF', seedrCsrfToken);
  }

  return fetch(url, {
    ...init,
    headers,
    credentials: 'include',
  });
};

export const api = {
  async searchMediaMetadata(query: string, signal?: AbortSignal): Promise<SearchMediaMetadataResponse> {
    const params = new URLSearchParams({ q: query });
    const res = await fetch(API_BASE + '/api/search/metadata?' + params.toString(), {
      signal, credentials: 'include', cache: 'no-store',
    });
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) throw new Error(data?.detail || body || ('TMDB metadata lookup failed (HTTP ' + res.status + ')'));
    return data as SearchMediaMetadataResponse;
  },

  async searchMediaMetadataBatch(titles: string[], signal?: AbortSignal): Promise<{
    status: string;
    source?: string;
    requested?: number;
    matched?: number;
    results: SearchMediaMetadata[];
  }> {
    const params = new URLSearchParams();
    [...new Set(titles.map(title => String(title || '').trim()).filter(Boolean))]
      .slice(0, 80)
      .forEach(title => params.append('titles', title));
    const res = await fetch(API_BASE + '/api/search/metadata/batch?' + params.toString(), {
      signal, credentials: 'include', cache: 'no-store',
    });
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) throw new Error(data?.detail || body || ('Cached search metadata lookup failed (HTTP ' + res.status + ')'));
    return data;
  },

  async getImdbRatings(
    titles: string[],
    mediaTypes: Array<'movie' | 'tv'> = [],
    tmdbIds: Array<number | string | null | undefined> = [],
    signal?: AbortSignal,
  ): Promise<{
    status: string;
    requested?: number;
    matched?: number;
    results: ImdbRatingResult[];
  }> {
    const params = new URLSearchParams();
    const seen = new Set<string>();
    titles.forEach((rawTitle, index) => {
      const title = String(rawTitle || '').trim();
      if (!title) return;
      const mediaType = mediaTypes[index] === 'tv' ? 'tv' : 'movie';
      const key = mediaType + '|' + title.toLowerCase();
      if (seen.has(key) || seen.size >= 80) return;
      seen.add(key);
      params.append('titles', title);
      params.append('media_types', mediaType);
      const tmdbId = Number(tmdbIds[index] || 0);
      params.append('tmdb_ids', String(Number.isFinite(tmdbId) && tmdbId > 0 ? tmdbId : 0));
    });
    const res = await fetch(API_BASE + '/api/catalogue/imdb-ratings?' + params.toString(), {
      signal, credentials: 'include', cache: 'no-store',
    });
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) throw new Error(data?.detail || body || ('IMDb rating lookup failed (HTTP ' + res.status + ')'));
    return data;
  },

  async searchSavedOtt(titles: string[], signal?: AbortSignal): Promise<{
    status: string;
    results: Array<{ query: string; found: boolean; entries: Array<{
      title: string;
      year?: string | null;
      tmdbId?: number | string;
      ottAvailability: {
        region?: string;
        status?: string;
        streamingProviders?: unknown[];
        rentProviders?: unknown[];
        buyProviders?: unknown[];
        providerLink?: string;
        source?: string;
      };
    }> }>;
  }> {
    const params = new URLSearchParams();
    titles.slice(0, 80).forEach(title => params.append('titles', title));
    const res = await fetch(API_BASE + '/api/catalogue/ott/search?' + params.toString(), {
      signal, credentials: 'include', cache: 'no-store',
    });
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) throw new Error(data?.detail || body || ('Saved OTT lookup failed (HTTP ' + res.status + ')'));
    return data;
  },

  async getMarvelCatalogue(signal?: AbortSignal, paging?: { page: number; pageSize: number }): Promise<MarvelCatalogueResponse> {
    const res = await fetch(API_BASE + '/api/catalogue/marvel' + (paging ? '?page=' + paging.page + '&page_size=' + paging.pageSize : ''), {
      signal,
      credentials: 'include',
    });
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) {
      throw new Error(data?.detail || data?.error || body || ('Marvel catalogue failed (HTTP ' + res.status + ')'));
    }
    return data as MarvelCatalogueResponse;
  },

  async getCatalogue(key: string, signal?: AbortSignal, paging?: { page: number; pageSize: number }): Promise<MarvelCatalogueResponse> {
    const res = await fetch(API_BASE + '/api/catalogue/extra/' + encodeURIComponent(key) + (paging ? '?page=' + paging.page + '&page_size=' + paging.pageSize : ''), {
      signal,
      credentials: 'include',
    });
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) {
      throw new Error(data?.detail || data?.error || body || ('Movie catalogue failed (HTTP ' + res.status + ')'));
    }
    return data as MarvelCatalogueResponse;
  },

  async getTvCatalogue(key: string, page = 1, signal?: AbortSignal): Promise<TvCataloguePage> {
    const params = new URLSearchParams({ page: String(Math.min(Math.max(page, 1), 500)) });
    const res = await fetch(API_BASE + '/api/tv/catalogue/' + encodeURIComponent(key) + '?' + params.toString(), {
      signal, credentials: 'include', cache: 'no-store',
    });
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) throw new Error(data?.detail || data?.error || body || ('TV catalogue failed (HTTP ' + res.status + ')'));
    return data as TvCataloguePage;
  },

  async getMovieCatalogue(
    key: MovieCatalogueKey,
    page = 1,
    releasedOnly = true,
    signal?: AbortSignal,
    maxSizeGb = 5,
    basic = false,
  ): Promise<MovieCataloguePage> {
    const params = new URLSearchParams({
      page: String(Math.min(Math.max(page, 1), 500)),
      released_only: String(releasedOnly),
    });
    if (basic) params.set('basic', 'true');
    if (key === 'bollywood-library') params.set('max_size_gb', String(Math.min(5, Math.max(1, maxSizeGb))));
    const res = await fetch(API_BASE + '/api/movies/catalogue/' + encodeURIComponent(key) + '?' + params.toString(), {
      signal,
      credentials: 'include',
      cache: 'no-store',
    });
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) {
      throw new Error(data?.detail || data?.error || body || ('Movie catalogue failed (HTTP ' + res.status + ')'));
    }
    return data as MovieCataloguePage;
  },

  async clearTmdbMovieCaches(): Promise<{ status: string; cacheTypes?: string[]; refetchRequired?: boolean }> {
    const res = await apiFetch('/api/movies/catalogue/cache/clear', {
      method: 'POST',
      cache: 'no-store',
    });
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) {
      throw new Error(data?.detail || data?.error || body || ('TMDB cache clear failed (HTTP ' + res.status + ')'));
    }
    return data;
  },

  async getMovieOttAvailability(
    movieId: number,
    region = 'IN',
    signal?: AbortSignal,
  ): Promise<MovieOttAvailability> {
    const params = new URLSearchParams({ region: region.toUpperCase() });
    const res = await fetch(
      API_BASE + '/api/movies/ott/' + encodeURIComponent(String(movieId)) + '?' + params.toString(),
      { signal, credentials: 'include', cache: 'no-store' },
    );
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) {
      throw new Error(data?.detail || data?.error || body || ('OTT availability failed (HTTP ' + res.status + ')'));
    }
    return data as MovieOttAvailability;
  },

  async searchAvailability(query: string, signal?: AbortSignal): Promise<{
    status: 'ready' | 'not_found' | 'not_confirmed' | 'unavailable';
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
      providers: Array<{ name: string; region: string; type: string; webUrl?: string; logoUrl?: string; providerId?: number | string }>;
    }>;
    providers: Array<{ name: string; region: string; type: string; webUrl?: string; logoUrl?: string; providerId?: number | string }>;
    message?: string;
  }> {
    const params = new URLSearchParams({ q: query });
    const controller = new AbortController();
    let timedOut = false;
    const timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 14000);
    const forwardAbort = () => controller.abort();
    if (signal?.aborted) controller.abort();
    else signal?.addEventListener('abort', forwardAbort, { once: true });
    try {
      const res = await fetch(API_BASE + '/api/search/availability?' + params.toString(), {
        signal: controller.signal, credentials: 'include', cache: 'no-store',
      });
      const body = await res.text();
      let data: any = null;
      try { data = body ? JSON.parse(body) : null; } catch {}
      if (!res.ok) throw new Error(data?.detail || data?.message || body || ('Availability lookup failed (HTTP ' + res.status + ')'));
      return data;
    } catch (error: any) {
      if (timedOut) throw new Error('Streaming availability check timed out.');
      throw error;
    } finally {
      window.clearTimeout(timeoutId);
      signal?.removeEventListener('abort', forwardAbort);
    }
  },

  // Torrents (qBittorrent WebAPI)
  async searchTorrents(query: string, limit = 50, signal?: AbortSignal, prepareTrace?: { id: string; intent: 'explicit-card-click' }): Promise<TorrentSearchResult[]> {
    const params = new URLSearchParams({
      q: query,
      limit: String(Math.min(Math.max(limit, 1), 50))
    });

    // Search is served by the Render backend alongside the Seedr API.
    // Use the same API base in development and production so the frontend
    // can be hosted independently as a Render Static Site.
    const searchBase = API_BASE;
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), 12000);
    let res: Response;
    try {
      res = await fetch(searchBase + '/api/search?' + params.toString(), {
        signal: signal || controller.signal,
        headers: prepareTrace ? {
          'X-Prepare-Intent': prepareTrace.intent,
          'X-Prepare-Trace': prepareTrace.id,
        } : undefined,
      });
    } catch (error: any) {
      if (error?.name === 'AbortError') {
        throw new Error('Search timed out. Please try again.');
      }
      throw error;
    } finally {
      window.clearTimeout(timeoutId);
    }
    const body = await res.text();

    let data: any = null;
    try {
      data = body ? JSON.parse(body) : null;
    } catch {
      // Keep raw response for the error below.
    }

    if (!res.ok) {
      throw new Error(data?.error || data?.message || body || ('Torrent search failed (HTTP ' + res.status + ')'));
    }

    return Array.isArray(data) ? data : (Array.isArray(data?.results) ? data.results : []);
  },

  async resolveSearchMagnet(infoUrl: string, indexer = ''): Promise<{ magnet: string; infoHash: string }> {
    const res = await apiFetch('/api/search/resolve-magnet', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ infoUrl, indexer })
    });
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) {
      throw new Error(data?.detail || data?.error || body || 'Could not resolve the magnet link.');
    }
    return data as { magnet: string; infoHash: string };
  },

  async addSearchTorrent(source: string, size: number, infoHash?: string, torrentName?: string): Promise<any> {
    const magnet = source.trim();
    const resolvedMagnet =
      magnet.toLowerCase().startsWith('magnet:?')
        ? magnet
        : infoHash
          ? 'magnet:?xt=urn:btih:' + infoHash.trim()
          : magnet;

    const res = await apiFetch('/api/search/torrents/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: resolvedMagnet, size, infoHash, torrent_name: torrentName || undefined })
    });
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) throw new Error(data?.error || body || 'Failed to add search result');
    return data;
  },

  async getTorrents(filter?: string): Promise<TorrentItem[]> {
    const url = filter ? `/api/v2/torrents/info?filter=${filter}` : '/api/v2/torrents/info';
    const res = await apiFetch(url);
    if (!res.ok) throw new Error('Failed to fetch torrents');
    return res.json();
  },

  async getTorrentFiles(hash: string): Promise<TorrentFileItem[]> {
    const res = await apiFetch(`/api/v2/torrents/files?hash=${encodeURIComponent(hash)}`);
    if (!res.ok) throw new Error('Failed to fetch files');
    return res.json();
  },

  async exportTorrent(hash: string): Promise<Blob> {
    const res = await apiFetch(`/api/v2/torrents/export?hash=${encodeURIComponent(hash)}`);
    if (!res.ok) {
      const message = await res.text().catch(() => '');
      throw new Error(message || 'Failed to export torrent file');
    }
    return res.blob();
  },

  async setFilePriority(hash: string, fileIds: string, priority: number): Promise<void> {
    const res = await apiFetch('/api/v2/torrents/filePrio', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hash, id: fileIds, priority })
    });
    if (!res.ok) throw new Error('Failed to set file priority');
  },

  async inspectMagnet(magnet: string, category = 'Downloads', sourceUrl = '', descriptorUrl = ''): Promise<{
    name: string;
    hash: string;
    files: { index: number; name: string; size: number; path: string; type: string; priority?: number }[];
    totalSize: number;
    source: string;
    pending?: boolean;
    createdPreview?: boolean;
    message?: string;
    jobId?: string;
  }> {
    const request = async () => {
      const res = await apiFetch('/api/v2/torrents/inspect-magnet', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          magnet,
          category,
          sourceUrl: sourceUrl || undefined,
          descriptorUrl: descriptorUrl || undefined
        })
      });
      const body = await res.text();
      let data: any = null;
      try { data = body ? JSON.parse(body) : null; } catch {}
      return { res, data, body };
    };

    const first = await request();
    if (first.res.ok && first.res.status !== 202 && Array.isArray(first.data?.files)) {
      return first.data;
    }

    if (first.res.status !== 202) {
      throw new Error(
        first.data?.error ||
        first.data?.message ||
        first.body ||
        `Torrent metadata inspection failed (HTTP ${first.res.status})`
      );
    }

    const jobId = String(first.data?.jobId || '').trim();
    if (!jobId) {
      throw new Error(first.data?.message || 'Torrent metadata is still resolving.');
    }

    // Keep the UI responsive while the backend resolver stays alive and
    // benefits from its warm libtorrent session/cache.
    // Do not keep the modal blocked for a full minute. Return a pending
    // result after a short foreground wait so the UI can move the resolver
    // into My Cloud Files and the user can continue working.
    for (let attempt = 0; attempt < 12; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 1000));

      const res = await apiFetch(
        '/api/v2/torrents/inspect-magnet/status?jobId=' + encodeURIComponent(jobId)
      );
      const body = await res.text();
      let data: any = null;
      try { data = body ? JSON.parse(body) : null; } catch {}

      if (res.ok && Array.isArray(data?.files) && data.files.length > 0) {
        return data;
      }

      if (!res.ok) {
        throw new Error(
          data?.error ||
          data?.message ||
          body ||
          `Torrent metadata inspection failed (HTTP ${res.status})`
        );
      }
    }

    return {
      name: '',
      hash: jobId,
      files: [],
      totalSize: 0,
      source: 'libtorrent_metadata',
      pending: true,
      jobId,
      message: 'Torrent metadata is still resolving in the background. Seedr has not been started.'
    };
  },

  async getTorrentMetadataJobs(): Promise<{
    jobs: Array<{
      jobId: string;
      hash: string;
      name: string;
      status: string;
      rounds: number;
      startedAt: number;
      updatedAt: number;
      deadlineAt: number;
      elapsedSeconds: number;
      remainingSeconds: number;
      fileCount: number;
      totalSize: number;
      source: string;
      error?: string | null;
    }>;
    backgroundTtlSeconds: number;
    retentionSeconds: number;
  }> {
    const res = await apiFetch('/api/v2/torrents/metadata-jobs');
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) {
      throw new Error(data?.error || data?.message || body || 'Torrent metadata jobs request failed');
    }
    return {
      jobs: Array.isArray(data?.jobs) ? data.jobs : [],
      backgroundTtlSeconds: Number(data?.backgroundTtlSeconds || 0),
      retentionSeconds: Number(data?.retentionSeconds || 0),
    };
  },

  async getTorrentMetadataStatus(jobId: string): Promise<any> {
    const res = await apiFetch(
      '/api/v2/torrents/inspect-magnet/status?jobId=' + encodeURIComponent(jobId)
    );
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) {
      throw new Error(data?.error || data?.message || body || 'Torrent metadata status failed');
    }
    return data;
  },

  async uploadTorrentFile(file: File): Promise<{
    name: string;
    hash: string;
    files: { index: number; name: string; size: number; path: string; type: string }[];
    totalSize: number;
    magnetUri: string;
  }> {
    const arrayBuffer = await file.arrayBuffer();
    const bytes = new Uint8Array(arrayBuffer);
    let binary = '';
    for (let i = 0; i < bytes.byteLength; i++) {
      binary += String.fromCharCode(bytes[i]);
    }
    const base64 = btoa(binary);

    const res = await apiFetch('/api/v2/torrents/upload-torrent', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ base64, filename: file.name })
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: 'Failed to parse torrent file' }));
      throw new Error(err.error || 'Failed to parse torrent file');
    }
    return res.json();
  },

  async prepareSeedrMagnet(
    magnet: string,
    requiredBytes = 0,
    torrentName?: string,
    replaceTaskId?: number | string
  ): Promise<any> {
    const value = String(magnet || '');
    if (!value.trim().toLowerCase().startsWith('magnet:?')) {
      throw new Error('A valid magnet URL is required');
    }

    const res = await apiFetch('/api/seedr/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        magnet: value,
        required_bytes: Math.max(0, Number(requiredBytes) || 0),
        auto_cleanup: true,
        replace_active: true,
        replace_task_id: replaceTaskId == null ? undefined : String(replaceTaskId),
        torrent_name: torrentName || undefined,
      })
    });

    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}

    if (!res.ok) {
      const error = new Error(
        data?.error || data?.message || data?.detail || body ||
        `Seedr prepare failed (HTTP ${res.status})`
      );
      Object.assign(error as any, data || {});
      if (res.status === 413) (error as any).code = data?.code || 'SEEDR_INSUFFICIENT_SPACE';
      throw error;
    }

    return {
      backend: 'seedr',
      seedrTaskId: data?.task_id ?? data?.taskId ?? data?.id ?? data?.task?.id ?? null,
      seedrResponse: data,
      seedrFolderName: data?.torrent_name || data?.name || data?.task?.name || torrentName || null,
      seedrFolderId: data?.folder_id ?? data?.folderId ?? data?.task?.folder_id ?? null,
      deletedFolders: Array.isArray(data?.deleted_folders) ? data.deleted_folders : [],
      cancelledTasks: Array.isArray(data?.cancelled_tasks) ? data.cancelled_tasks : [],
    };
  },

  async addMagnet(
    urls: string,
    category = 'Downloads',
    selectedFiles?: number[],
    manifest?: { index?: number; name: string; size: number; priority: number }[],
    existingHash?: string,
    forceBackend?: 'seedr' | 'qbittorrent',
    selectedNames?: string[],
    seedrTaskId?: number | string,
    torrentName?: string
  ): Promise<any> {
    const magnet = urls;
    if (!magnet.trim().toLowerCase().startsWith('magnet:?')) {
      throw new Error('A valid magnet URL is required');
    }

    if (forceBackend === 'seedr') {
      const res = await apiFetch('/api/seedr/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ magnet })
      });

      const body = await res.text();
      let data: any = null;
      try { data = body ? JSON.parse(body) : null; } catch {}

      if (!res.ok) {
        const error = new Error(
          data?.error || data?.message || data?.detail || body ||
          `Seedr add failed (HTTP ${res.status})`
        );
        Object.assign(error as any, data || {});
        if (res.status === 413) (error as any).code = data?.code || 'SEEDR_INSUFFICIENT_SPACE';
        throw error;
      }

      return {
        backend: 'seedr',
        seedrTaskId: data?.task_id ?? data?.taskId ?? data?.id ?? data?.task?.id ?? null,
        seedrResponse: data,
        seedrFolderName: data?.torrent_name || data?.name || data?.task?.name || null,
        seedrFolderId: data?.folder_id ?? data?.folderId ?? data?.task?.folder_id ?? null,
        selectionApplied: false,
        selectionError: null
      };
    }

    // qBittorrent path retains the existing selected-file priority behavior.
    const res = await apiFetch(API_BASE + '/api/v2/torrents/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        urls: magnet,
        category,
        selectedFiles: selectedFiles || [],
        manifest: manifest || [],
        existingHash,
        forceBackend: 'qbittorrent',
        selectedNames: selectedNames || [],
        seedrTaskId,
        torrentName
      })
    });

    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) {
      const error = new Error(data?.error || data?.message || data?.detail || body || `Torrent add failed (HTTP ${res.status})`);
      if (res.status === 413) (error as any).code = 'SEEDR_INSUFFICIENT_SPACE';
      if (data?.code) (error as any).code = data.code;
      Object.assign(error as any, data || {});
      throw error;
    }
    return data;
  },

  async getSeedrSession(): Promise<{
    connected: boolean;
    csrfToken: string;
  }> {
    const res = await apiFetch('/api/seedr/session');
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) throw makeSeedrError(data, body, res.status, 'Failed to initialize Seedr session');

    seedrCsrfToken = typeof data?.csrfToken === 'string' ? data.csrfToken : '';
    return {
      connected: Boolean(data?.connected),
      csrfToken: seedrCsrfToken,
    };
  },

  async connectSeedrPat(pat: string): Promise<{
    status: 'connected' | 'error';
    connected: boolean;
  }> {
    const value = String(pat || '').trim();
    if (!value) throw new Error('Enter your Seedr Personal Access Token.');

    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), 15000);
    let res: Response;
    try {
      res = await apiFetch('/api/seedr/connect/pat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pat: value }),
        signal: controller.signal,
      });
    } catch (error: any) {
      if (error?.name === 'AbortError') {
        throw new Error('Seedr connection timed out. The server could not validate the token in time; please retry.');
      }
      throw error;
    } finally {
      window.clearTimeout(timeoutId);
    }
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) {
      throw makeSeedrError(data, body, res.status, 'Seedr rejected this Personal Access Token.');
    }
    return {
      status: data?.status === 'connected' ? 'connected' : 'error',
      connected: Boolean(data?.connected),
    };
  },

  async startSeedrConnection(): Promise<{
    status: 'pending' | 'connected' | 'error';
    connected: boolean;
    userCode?: string;
    verificationUrl?: string;
    expiresIn?: number;
    interval?: number;
  }> {
    const res = await apiFetch('/api/seedr/connect/start', { method: 'POST' });
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) throw makeSeedrError(data, body, res.status, 'Failed to start Seedr account connection');
    return data;
  },

  async pollSeedrConnection(): Promise<{
    status: 'idle' | 'pending' | 'connected' | 'expired' | 'error';
    connected: boolean;
    message?: string;
    code?: string;
    expiresIn?: number;
  }> {
    const res = await apiFetch('/api/seedr/connect/status');
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) throw makeSeedrError(data, body, res.status, 'Failed to check Seedr connection');
    return data;
  },

  async disconnectSeedr(): Promise<void> {
    const res = await apiFetch('/api/seedr/connect/disconnect', { method: 'POST' });
    const body = await res.text();
    if (!res.ok) {
      let data: any = null;
      try { data = body ? JSON.parse(body) : null; } catch {}
      throw makeSeedrError(data, body, res.status, 'Failed to disconnect Seedr account');
    }
  },

  async getSeedrTokenDiagnostic(): Promise<any> {
    const res = await apiFetch('/api/seedr/token-diagnostic');
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch { data = null; }
    if (!res.ok) throw makeSeedrError(data, body, res.status, 'Failed to diagnose Seedr token');
    return data;
  },

  async getSeedrAuthStatus(): Promise<{
    configured: boolean;
    authenticated: boolean;
    code: string;
    message?: string;
  }> {
    const res = await apiFetch('/api/seedr/auth-status');
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch { data = null; }
    if (!res.ok) throw makeSeedrError(data, body, res.status, 'Failed to check Seedr authentication');
    return {
      configured: Boolean(data?.configured),
      authenticated: Boolean(data?.authenticated),
      code: String(data?.code || ''),
      message: typeof data?.message === 'string' ? data.message : undefined,
    };
  },

  async getSeedrQuota(): Promise<{
    configured: boolean;
    maxSpace: number;
    usedSpace: number;
    remainingSpace: number;
  }> {
    const res = await apiFetch('/api/seedr/quota');
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch { data = null; }
    if (!res.ok) throw makeSeedrError(data, body, res.status, 'Failed to fetch Seedr quota');
    return {
      configured: Boolean(data?.configured),
      maxSpace: Number(data?.maxSpace || 0),
      usedSpace: Number(data?.usedSpace || 0),
      remainingSpace: Number(data?.remainingSpace || 0),
    };
  },

  async getSeedrLibrary(fresh = false): Promise<{
    configured: boolean;
    root: {
      id: string;
      folderId: string;
      name: string;
      path: string;
      filesCount: number;
      totalSize: number;
      folderCount: number;
    } | null;
    folders: Array<{
      id: string;
      folderId: string;
      name: string;
      torrentName?: string;
      path: string;
      filesCount: number;
      totalSize: number;
      folderCount: number;
    }>;
  }> {
    const res = await apiFetch('/api/seedr/library' + (fresh ? '?fresh=1' : ''));
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch { data = null; }
    if (!res.ok) throw makeSeedrError(data, body, res.status, 'Failed to fetch Seedr library metadata');
    return {
      configured: Boolean(data?.configured),
      root: data?.root || null,
      folders: Array.isArray(data?.folders) ? data.folders : [],
    };
  },

  async getSeedrFolderContents(folderId: string): Promise<{
    configured: boolean;
    folderId: string;
    files: Array<{ id: string; streamId?: string; name: string; size: number; folderId: string; url?: string | null }>;
    folders: Array<{ id: string; folderId: string; name: string }>;
  }> {
    const res = await apiFetch('/api/seedr/folders/' + encodeURIComponent(folderId) + '/contents');
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch { data = null; }
    if (!res.ok) throw makeSeedrError(data, body, res.status, 'Failed to load Seedr folder contents');
    return {
      configured: Boolean(data?.configured),
      folderId: String(data?.folderId || folderId),
      files: Array.isArray(data?.files) ? data.files : [],
      folders: Array.isArray(data?.folders) ? data.folders : [],
    };
  },

  async getSeedrFiles(): Promise<{
    configured: boolean;
    files: Array<{ id: string; name: string; size: number; folderId: string; folderPath: string }>;
  }> {
    const res = await apiFetch('/api/seedr/files');
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch { data = null; }
    if (!res.ok) throw makeSeedrError(data, body, res.status, 'Failed to fetch Seedr files');
    return {
      configured: Boolean(data?.configured),
      files: Array.isArray(data?.files) ? data.files : [],
    };
  },

  openSeedrFileDownload(fileId: string, filename = ''): void {
    const query = filename ? '?filename=' + encodeURIComponent(filename) : '';
    const url = '/api/seedr/files/' + encodeURIComponent(fileId) + '/download' + query;
    window.open(API_BASE + url, '_blank', 'noopener,noreferrer');
  },

  async getSeedrFileDownload(fileId: string): Promise<{ url: string; name: string }> {
    const res = await apiFetch('/api/seedr/files/' + encodeURIComponent(fileId) + '/download/url');
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch { data = null; }
    if (!res.ok) throw new Error(data?.error || body || 'Failed to create Seedr download link');
    return data;
  },

  async getSeedrFileStream(fileId: string, fileName: string, type: 'video' | 'audio'): Promise<{ url: string; externalUrl?: string; name: string }> {
    const params = new URLSearchParams({
      type,
      file_id: fileId,
      name: fileName
    });
    const res = await apiFetch('/api/seedr/files/stream?' + params.toString());
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch { data = null; }
    if (!res.ok) throw new Error(data?.error || body || 'Failed to create Seedr stream URL');
    const streamUrl = typeof data?.url === 'string' && data.url.startsWith('/')
      ? API_BASE + data.url
      : data?.url;
    return {
      ...data,
      url: streamUrl,
      externalUrl: data?.externalUrl
    };
  },

  async getSeedrMediaInfo(fileId: string): Promise<{
    name: string;
    audioTracks: Array<{
      index: number;
      streamIndex?: number;
      language: string;
      title: string;
      codec: string;
      channels: number;
      default: boolean;
    }>;
    subtitleTracks: Array<{
      index: number;
      streamIndex?: number;
      language: string;
      title: string;
      codec: string;
      url: string;
    }>;
  }> {
    const res = await apiFetch('/api/seedr/media-info/' + encodeURIComponent(fileId));
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch { data = null; }

    if (!res.ok) {
      throw new Error(data?.error || data?.message || body || 'Failed to inspect Seedr media tracks');
    }

    const subtitleTracks = Array.isArray(data?.subtitleTracks)
      ? data.subtitleTracks.map((track: any) => ({
          ...track,
          url: typeof track?.url === 'string' && track.url.startsWith('/')
            ? API_BASE + track.url
            : track?.url,
        }))
      : [];

    return {
      name: String(data?.name || fileId),
      audioTracks: Array.isArray(data?.audioTracks) ? data.audioTracks : [],
      subtitleTracks,
    };
  },

  openSeedrFolderDownload(folderId: string, filename = ''): void {
    const query = filename ? '?filename=' + encodeURIComponent(filename) : '';
    const url = '/api/seedr/folders/' + encodeURIComponent(folderId) + '/download' + query;
    window.open(API_BASE + url, '_blank', 'noopener,noreferrer');
  },

  async getSeedrFolderDownload(folderId: string): Promise<{ url: string }> {
    const res = await apiFetch('/api/seedr/folders/' + encodeURIComponent(folderId) + '/download/url');
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch { data = null; }
    if (!res.ok) throw new Error(data?.error || body || 'Failed to create Seedr folder download');
    return data;
  },

  async deleteSeedrFolder(folderId: string): Promise<void> {
    const res = await apiFetch('/api/seedr/folders/' + encodeURIComponent(folderId), { method: 'DELETE' });
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch { data = null; }
    if (!res.ok) throw new Error(data?.error || body || 'Failed to delete Seedr folder');
  },

  async deleteSeedrTask(taskId: number | string): Promise<any> {
    const res = await apiFetch('/api/seedr/tasks/' + encodeURIComponent(String(taskId)), { method: 'DELETE' });
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) {
      throw new Error(data?.error || data?.detail || body || 'Failed to delete Seedr task');
    }
    return data || {};
  },

  async deleteSeedrFile(fileId: string): Promise<void> {
    const res = await apiFetch('/api/seedr/files/' + encodeURIComponent(fileId), { method: 'DELETE' });
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch { data = null; }
    if (!res.ok) throw new Error(data?.error || body || 'Failed to delete Seedr file');
  },

  async getSeedrTaskProgress(taskId: number | string): Promise<{
    taskId: number | string;
    name?: string;
    folderId?: string;
    status: 'waiting' | 'downloading' | 'completed' | 'not_found';
    progress: number;
  }> {
    const res = await apiFetch('/api/seedr/tasks/' + encodeURIComponent(String(taskId)) + '/progress');
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch { data = null; }
    if (!res.ok) throw new Error(data?.error || body || 'Failed to check Seedr task progress');
    return {
      taskId: data?.taskId ?? taskId,
      name: typeof data?.name === 'string' ? data.name : '',
      folderId: typeof data?.folderId === 'string' ? data.folderId : '',
      status: data?.status || 'downloading',
      progress: Math.max(0, Math.min(100, Number(data?.progress) || 0)),
    };
  },

  async getSeedrTask(taskId: number | string): Promise<{
    taskId: number | string;
    name?: string;
    folderName?: string;
    folderId?: string;
    status: 'waiting' | 'downloading' | 'completed' | 'not_found';
    progress: number;
    downloadUrl: string | null;
    files: Array<{
      id: string;
      name: string;
      size: number;
      folderId: string;
      folderPath: string;
      url: string | null;
      available?: boolean;
    }>;
  }> {
    const res = await apiFetch('/api/seedr/tasks/' + encodeURIComponent(String(taskId)));
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch { data = null; }
    if (!res.ok) throw new Error(data?.error || body || 'Failed to check Seedr task');
    return data;
  },
  async pauseTorrent(hash: string): Promise<void> {
    const res = await apiFetch('/api/v2/torrents/pause', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hashes: hash })
    });
    if (!res.ok) throw new Error('Failed to pause torrent');
  },

  async resumeTorrent(hash: string): Promise<void> {
    const res = await apiFetch('/api/v2/torrents/resume', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hashes: hash })
    });
    if (!res.ok) throw new Error('Failed to resume torrent');
  },

  async deleteTorrent(hash: string, deleteFiles = false): Promise<void> {
    const res = await apiFetch('/api/v2/torrents/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hashes: hash, deleteFiles })
    });
    if (!res.ok) throw new Error('Failed to delete torrent');
  },

  // Storage Files
  async getFiles(folder = '/', search = '', type = 'all', folderId = ''): Promise<StorageFile[]> {
    const params = new URLSearchParams({ folder, search, type });
    if (folderId) params.set('folder_id', folderId);
    const res = await apiFetch(`/api/files?${params.toString()}`);
    if (!res.ok) throw new Error('Failed to fetch files');
    return res.json();
  },

  async deleteFile(id: string): Promise<{ success: boolean; cleanup: any }> {
    const res = await apiFetch('/api/files/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id })
    });
    if (!res.ok) throw new Error('Failed to delete file');
    return res.json();
  },

  async renameItem(id: string, newName: string, isFolder: boolean) {
    const res = await apiFetch('/api/files/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, newName, isFolder })
    });
    if (!res.ok) throw new Error('Failed to rename item');
    return res.json();
  },

  async moveFile(fileId: string, targetFolder: string) {
    const res = await apiFetch('/api/files/move', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fileId, targetFolder })
    });
    if (!res.ok) throw new Error('Failed to move file');
    return res.json();
  },

  async createFolder(name: string, parentPath = '/', isShared = false): Promise<StorageFolder> {
    const res = await apiFetch('/api/files/folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, parentPath, isShared })
    });
    if (!res.ok) throw new Error('Failed to create folder');
    return res.json();
  },

  async getFolders(): Promise<StorageFolder[]> {
    const res = await apiFetch('/api/folders');
    if (!res.ok) throw new Error('Failed to fetch folders');
    return res.json();
  },

  async updateFolderShare(folderId: string, isShared: boolean, permissions: Record<string, string>) {
    const res = await apiFetch('/api/folders/share', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folderId, isShared, permissions })
    });
    if (!res.ok) throw new Error('Failed to update share permissions');
    return res.json();
  },

  // Users
  async getUsers(): Promise<{ users: UserProfile[]; activeUserId: string; activeUser: UserProfile }> {
    const res = await apiFetch('/api/users');
    if (!res.ok) throw new Error('Failed to fetch users');
    return res.json();
  },

  async switchUser(userId: string): Promise<{ activeUser: UserProfile }> {
    const res = await apiFetch('/api/users/switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ userId })
    });
    if (!res.ok) throw new Error('Failed to switch user');
    return res.json();
  },

  async createUser(name: string, email: string, role: string): Promise<UserProfile> {
    const res = await apiFetch('/api/users/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, email, role })
    });
    if (!res.ok) throw new Error('Failed to create user');
    return res.json();
  },

  // Storage & Cleanup
  async getStorageStats(): Promise<StorageStats> {
    const res = await apiFetch('/api/storage/stats');
    if (!res.ok) throw new Error('Failed to fetch storage stats');
    return res.json();
  },

  async getCleanupSettings(): Promise<CleanupSettings> {
    const res = await apiFetch('/api/cleanup/settings');
    if (!res.ok) throw new Error('Failed to fetch cleanup settings');
    return res.json();
  },

  async updateCleanupSettings(settings: Partial<CleanupSettings>): Promise<CleanupSettings> {
    const res = await apiFetch('/api/cleanup/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings)
    });
    if (!res.ok) throw new Error('Failed to update cleanup settings');
    return res.json();
  },

  async runCleanup(): Promise<{ bytesFreed: number; filesRemoved: number; tempRemoved: number; orphansRemoved: number }> {
    const res = await apiFetch('/api/cleanup/run', { method: 'POST' });
    if (!res.ok) throw new Error('Failed to run cleanup');
    return res.json();
  },

  // Logs & Notifications
  async getLogs(): Promise<ActivityLog[]> {
    const res = await apiFetch('/api/logs');
    if (!res.ok) throw new Error('Failed to fetch logs');
    return res.json();
  },

  async clearLogs(): Promise<void> {
    await apiFetch('/api/logs/clear', { method: 'POST' });
  },

  async getNotifications(): Promise<AppNotification[]> {
    const res = await apiFetch('/api/notifications');
    if (!res.ok) throw new Error('Failed to fetch notifications');
    return res.json();
  },

  async markNotificationsRead(): Promise<void> {
    await apiFetch('/api/notifications/read', { method: 'POST' });
  },

  async testNotification(): Promise<void> {
    await apiFetch('/api/notifications/test', { method: 'POST' });
  },

  async submitFeedback(feedback: {
    type: 'review' | 'suggestion' | 'bug';
    rating?: number;
    message: string;
    name?: string;
  }): Promise<{ submitted: boolean; issueUrl?: string }> {
    const res = await apiFetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(feedback)
    });
    const body = await res.text();
    let data: any = null;
    try { data = body ? JSON.parse(body) : null; } catch {}
    if (!res.ok) throw new Error(data?.error || body || 'Failed to send feedback');
    return data || { submitted: true };
  },

  // qBittorrent Configuration
  async getQbtSettings(): Promise<QbtSettings> {
    const res = await apiFetch('/api/qbt/settings');
    if (!res.ok) throw new Error('Failed to fetch qbt settings');
    return res.json();
  },

  async updateQbtSettings(settings: Partial<QbtSettings>): Promise<QbtSettings> {
    const res = await apiFetch('/api/qbt/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings)
    });
    if (!res.ok) throw new Error('Failed to update qbt settings');
    return res.json();
  }
};