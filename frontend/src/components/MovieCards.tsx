import React from 'react';
import { Check, Copy, Database, Download, Film, Loader2, Play, Users, X } from 'lucide-react';
import type { ImdbRatingResult } from '../api/client.ts';
import { MovieCatalogueCard } from './MovieCatalogueCard.tsx';

/**
 * The two movie cards used by EVERY movie section (Marvel, DC, DC Animated,
 * Classics, Bollywood, Hollywood, TMDB discovery and normal search results):
 *
 *   1. MovieCompactCard  - poster, IMDb rating, title, synopsis, streaming services.
 *      Clicking the card opens its Prepare view.
 *   2. MoviePrepareCard  - torrent details: quality picker, seeders, source,
 *      Direct Stream + Prepare, and Play / Download / Copy once prepared.
 *
 * Web series cards are intentionally separate. These components are purely
 * presentational: callers supply data and handlers, so a design or behaviour
 * change made here applies to every movie card at once.
 */

export type MovieCardImdb = Pick<ImdbRatingResult, 'imdbRating' | 'imdbVotes' | 'imdbId' | 'imdbUrl'> | null | undefined;

export interface MovieCardProvider {
  key: string;
  name: string;
  webUrl?: string | null;
}

/**
 * Staged poster loading shared by every movie card: cards first show the lightweight
 * (w185) thumbnail. The large (w780) version waits for page details and browser idle,
 * then loads at low priority so navigation and initial card content stay responsive.
 */
let highQualityPostersReady = false;
const pendingHighQualityPosters = new Set<HTMLImageElement>();
const upgradedHighQualityPosters = new Set<HTMLImageElement>();
let highQualityUpgradeScheduled = false;
let highQualityUpgradeInFlight = false;
let highQualityUpgradeGeneration = 0;
let highQualityIdleHandle: number | null = null;
let highQualityFallbackTimer: number | null = null;
let lastPosterUserActivity = Date.now();

if (typeof document !== 'undefined') {
  const notePosterUserActivity = () => {
    lastPosterUserActivity = Date.now();
  };
  document.addEventListener('pointerdown', notePosterUserActivity, { passive: true });
  document.addEventListener('keydown', notePosterUserActivity);
  document.addEventListener('scroll', notePosterUserActivity, { passive: true });
  document.addEventListener('touchstart', notePosterUserActivity, { passive: true });
}

function normalizeLowQualityPosterUrl(value: string): string {
  const url = String(value || '').trim();
  if (!url) return '';
  try {
    const parsed = new URL(url, window.location.origin);
    // Catalogue poster URLs are resolved by the backend. Explicitly request the
    // small same-origin image variant instead of allowing a redirect to w500.
    if (parsed.pathname.replace(/\/+$/, '') === '/api/poster') {
      parsed.searchParams.set('size', 'w185');
    } else if (parsed.hostname === 'image.tmdb.org') {
      parsed.pathname = parsed.pathname.replace(
        /\/t\/p\/(?:original|w780|w500|w342|w300|w200|w185|w154|w92)\//,
        '/t/p/w185/',
      );
    }
    return parsed.toString();
  } catch {
    return url;
  }
}

function cancelHighQualityUpgradeSchedule(): void {
  const idleWindow = window as Window & {
    cancelIdleCallback?: (handle: number) => void;
  };
  if (highQualityIdleHandle !== null) {
    idleWindow.cancelIdleCallback?.(highQualityIdleHandle);
    highQualityIdleHandle = null;
  }
  if (highQualityFallbackTimer !== null) {
    window.clearTimeout(highQualityFallbackTimer);
    highQualityFallbackTimer = null;
  }
  highQualityUpgradeScheduled = false;
}

function restoreLowQualityPoster(image: HTMLImageElement): void {
  if (!image.isConnected || image.dataset.highQualityTried !== 'true') return;
  const lowQualityUrl = normalizeLowQualityPosterUrl(image.currentSrc || image.src);
  image.dataset.highQualityTried = '';
  image.dataset.highQualityFailed = '';
  image.dataset.posterQuality = 'low-retrying';
  image.fetchPriority = 'high';
  image.loading = 'eager';
  if (lowQualityUrl && image.src !== lowQualityUrl) image.src = lowQualityUrl;
}

function upgradeToHighQualityPoster(image: HTMLImageElement): boolean {
  if (
    !image.isConnected ||
    image.dataset.highQualityTried === 'true' ||
    image.dataset.highQualityFailed === 'true'
  ) return false;
  const current = image.src || image.currentSrc;
  try {
    const parsed = new URL(current, window.location.origin);
    if (parsed.pathname.replace(/\/+$/, '') === '/api/poster') {
      parsed.searchParams.set('size', 'w780');
      const highQualityUrl = parsed.toString();
      if (highQualityUrl === current) return false;
      image.fetchPriority = 'low';
      image.loading = 'lazy';
      image.dataset.highQualityTried = 'true';
      image.dataset.posterQuality = 'high-pending';
      image.src = highQualityUrl;
      upgradedHighQualityPosters.add(image);
      highQualityUpgradeInFlight = true;
      return true;
    }
  } catch {
    // Fall through to the direct TMDB URL handling below.
  }
  if (!current.includes('image.tmdb.org') || !/\/t\/p\/w185\//.test(current)) return false;
  image.fetchPriority = 'low';
  image.loading = 'lazy';
  image.dataset.highQualityTried = 'true';
  image.dataset.posterQuality = 'high-pending';
  image.src = current.replace('/t/p/w185/', '/t/p/w780/');
  upgradedHighQualityPosters.add(image);
  highQualityUpgradeInFlight = true;
  return true;
}

function scheduleHighQualityPosterUpgrades(): void {
  if (
    !highQualityPostersReady ||
    highQualityUpgradeScheduled ||
    highQualityUpgradeInFlight ||
    pendingHighQualityPosters.size === 0
  ) return;
  highQualityUpgradeScheduled = true;
  const generation = highQualityUpgradeGeneration;

  const run = () => {
    highQualityIdleHandle = null;
    highQualityFallbackTimer = null;
    highQualityUpgradeScheduled = false;
    if (
      generation !== highQualityUpgradeGeneration ||
      !highQualityPostersReady ||
      highQualityUpgradeInFlight ||
      pendingHighQualityPosters.size === 0
    ) return;
    if (Date.now() - lastPosterUserActivity < 1800) {
      scheduleHighQualityPosterUpgrades();
      return;
    }

    const images = [...pendingHighQualityPosters].filter(image => image.isConnected);
    images.sort((a, b) => {
      const visible = (image: HTMLImageElement) => {
        const rect = image.getBoundingClientRect();
        return rect.bottom > 0 && rect.top < window.innerHeight && rect.right > 0 && rect.left < window.innerWidth;
      };
      return Number(visible(b)) - Number(visible(a));
    });

    for (const image of images) {
      pendingHighQualityPosters.delete(image);
      if (upgradeToHighQualityPoster(image)) return;
    }
  };

  const idleWindow = window as Window & {
    requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number;
  };
  if (idleWindow.requestIdleCallback) {
    highQualityIdleHandle = idleWindow.requestIdleCallback(run, { timeout: 5000 });
  } else {
    highQualityFallbackTimer = window.setTimeout(run, 2500);
  }
}

export function requestHighQualityPoster(image: HTMLImageElement): void {
  if (image.dataset.highQualityTried === 'true') {
    if (upgradedHighQualityPosters.has(image)) {
      highQualityUpgradeInFlight = false;
    }
    scheduleHighQualityPosterUpgrades();
    return;
  }
  if (image.dataset.posterQuality === 'low-loaded' || image.complete) {
    pendingHighQualityPosters.add(image);
    scheduleHighQualityPosterUpgrades();
  } else {
    pendingHighQualityPosters.add(image);
  }
}

/** Pause and reset upgrades on navigation; resume only after the page details settle. */
export function setHighQualityPostersReady(ready: boolean): void {
  if (!ready) {
    highQualityUpgradeGeneration += 1;
    cancelHighQualityUpgradeSchedule();
    highQualityUpgradeInFlight = false;
    pendingHighQualityPosters.clear();
    for (const image of upgradedHighQualityPosters) {
      restoreLowQualityPoster(image);
    }
    upgradedHighQualityPosters.clear();
    for (const image of document.querySelectorAll<HTMLImageElement>('img[data-movie-poster-stage="true"]')) {
      image.dataset.highQualityFailed = '';
    }
  }
  highQualityPostersReady = ready;
  if (!ready) {
    return;
  }
  scheduleHighQualityPosterUpgrades();
}

type PosterEventHandler = (event: React.SyntheticEvent<HTMLImageElement>) => void;

const hasImdbRating = (imdb: MovieCardImdb): imdb is NonNullable<MovieCardImdb> & { imdbRating: number } =>
  imdb?.imdbRating != null && imdb.imdbRating > 0;

/** Link target for a movie's IMDb badge (title page when the id is known). */
export const imdbBadgeHref = (imdb: MovieCardImdb): string =>
  imdb?.imdbUrl || (imdb?.imdbId ? 'https://www.imdb.com/title/' + imdb.imdbId + '/' : '#');

/** Link target for a movie title: the IMDb page, or an IMDb search as a fallback. */
export const imdbTitleLink = (imdb: MovieCardImdb, title: string, year?: number | string | null): string =>
  imdb?.imdbUrl ||
  (imdb?.imdbId
    ? 'https://www.imdb.com/title/' + imdb.imdbId + '/'
    : 'https://www.imdb.com/find/?q=' + encodeURIComponent([title, year].filter(Boolean).join(' ')));

const imdbBadgeTitle = (imdb: NonNullable<MovieCardImdb>) =>
  'IMDb rating' + (imdb.imdbVotes ? ' · ' + imdb.imdbVotes + ' votes' : '');

export interface MovieCompactCardProps {
  title: string;
  titleHref: string;
  year?: number | string | null;
  imdb?: MovieCardImdb;
  /** Shown only when there is no IMDb rating. */
  tmdbRating?: number | null;
  trendWeekLabel?: string;
  overview: string;
  posterSrc: string;
  posterAlt: string;
  onPosterClick: () => void;
  onPosterLoad: PosterEventHandler;
  onPosterError: PosterEventHandler;
  providers: MovieCardProvider[];
  providersAriaLabel: string;
  /** Text shown when there are no streaming providers to list. */
  providersFallbackText: string;
}

export const MovieCompactCard: React.FC<MovieCompactCardProps> = ({
  title,
  titleHref,
  year,
  imdb,
  tmdbRating,
  trendWeekLabel,
  overview,
  posterSrc,
  posterAlt,
  onPosterClick,
  onPosterLoad,
  onPosterError,
  providers,
  providersAriaLabel,
  providersFallbackText,
}) => {
  const showImdb = hasImdbRating(imdb);
  const showTmdb = !showImdb && tmdbRating != null && tmdbRating > 0;
  return (
    <MovieCatalogueCard
      onClick={event => {
        const target = event.target;
        if (target instanceof Element && target.closest('a, button, input, select, textarea, [role="button"]')) return;
        onPosterClick();
      }}
      className="cursor-pointer transition hover:border-slate-700 hover:bg-slate-900/95"
    >
      <div data-movie-poster-slot="true" data-poster-waiting={!posterSrc ? "true" : "false"} className="relative aspect-[2/3] w-full overflow-hidden bg-slate-950">
        {posterSrc ? (
          <>
            <img
              data-movie-poster-stage="true"
              src={normalizeLowQualityPosterUrl(posterSrc)}
              alt={posterAlt}
              loading="eager"
              fetchPriority="high"
              decoding="async"
              className="h-full w-full object-cover"
              onLoad={event => {
                      const image = event.currentTarget;
                      image.dataset.posterQuality = image.dataset.highQualityTried === 'true' ? 'high-loaded' : 'low-loaded';
                      onPosterLoad(event);
                      requestHighQualityPoster(image);
                    }}
                    onError={event => {
                      const image = event.currentTarget;
                      const previousSrc = image.src;
                      if (image.dataset.highQualityTried === 'true') {
                        requestHighQualityPoster(image);
                        image.dataset.highQualityTried = '';
                        image.dataset.highQualityFailed = 'true';
                        try {
                          const parsed = new URL(image.src || image.currentSrc, window.location.origin);
                          if (parsed.pathname.replace(/\/+$/, '') === '/api/poster' && parsed.searchParams.get('size') === 'w780') {
                            parsed.searchParams.set('size', 'w185');
                            image.dataset.posterQuality = 'low-retrying';
                            image.src = parsed.toString();
                            return;
                          }
                        } catch {
                          // Use the direct URL fallback below.
                        }
                        if (image.src.includes('/t/p/w780/')) {
                          image.dataset.posterQuality = 'low-retrying';
                          image.src = image.src.replace('/t/p/w780/', '/t/p/w185/');
                          return;
                        }
                      }
                      onPosterError(event);
                      image.dataset.posterQuality = image.src !== previousSrc ? 'low-retrying' : 'low-failed';
                    }}
            />
            <div data-poster-placeholder="true" className="hidden absolute inset-0 flex items-center justify-center text-slate-600">
              <Film className="h-8 w-8" />
            </div>
          </>
        ) : (
          <div className="absolute inset-0 flex items-center justify-center text-slate-600">
            <Film className="h-8 w-8" />
          </div>
        )}
        {showImdb ? (
          <a
            href={imdbBadgeHref(imdb)}
            target="_blank"
            rel="noreferrer"
            title={imdbBadgeTitle(imdb)}
            className="absolute left-2 top-2 rounded-md border border-amber-300/60 bg-slate-950/95 px-2 py-1 text-[10px] font-bold text-amber-300 shadow"
          >
            ⭐ {imdb.imdbRating.toFixed(1)}/10
          </a>
        ) : showTmdb ? (
          <span title="TMDB rating" className="absolute left-2 top-2 rounded-md border border-amber-300/40 bg-slate-950/95 px-2 py-1 text-[10px] font-bold text-amber-300 shadow">
            ⭐ TMDB {tmdbRating!.toFixed(1)}
          </span>
        ) : null}
      </div>

      <div className="flex flex-1 flex-col gap-2 p-2.5">
        <div>
          <h3 className="line-clamp-2 text-sm font-bold leading-snug text-slate-100">
            <a href={titleHref} target="_blank" rel="noreferrer" className="hover:text-cyan-300" title="Open on IMDb">
              {title}
            </a>
          </h3>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] sm:text-xs">
            {year ? <span className="text-slate-400">{year}</span> : null}
            {trendWeekLabel ? <span className="text-cyan-300">{trendWeekLabel}</span> : null}
            {showImdb ? (
              <a
                href={imdbBadgeHref(imdb)}
                target="_blank"
                rel="noreferrer"
                title={imdbBadgeTitle(imdb)}
                className="font-semibold text-amber-300 hover:text-amber-200"
              >
                IMDb {imdb.imdbRating.toFixed(1)}
              </a>
            ) : showTmdb ? (
              <span className="text-amber-300">TMDB {tmdbRating!.toFixed(1)}</span>
            ) : null}
          </div>
        </div>

        <p className="line-clamp-3 text-[10px] leading-relaxed text-slate-400">{overview}</p>

        {providers.length > 0 ? (
          <div className="flex flex-wrap gap-1" aria-label={providersAriaLabel}>
            {providers.slice(0, 5).map(provider => (
              <a
                key={provider.key}
                href={provider.webUrl || undefined}
                target={provider.webUrl ? '_blank' : undefined}
                rel={provider.webUrl ? 'noreferrer' : undefined}
                title={provider.name}
                className="rounded-full border border-cyan-900/70 bg-cyan-950/40 px-1.5 py-0.5 text-[10px] leading-4 text-cyan-200 hover:border-cyan-600"
              >
                {provider.name}
              </a>
            ))}
          </div>
        ) : (
          <div className="text-[10px] leading-relaxed text-slate-500">{providersFallbackText}</div>
        )}
      </div>
    </MovieCatalogueCard>
  );
};

export interface MoviePrepareCardVariant {
  key: string;
  label: string;
}

export interface MoviePrepareCardProps {
  posterSrc: string;
  posterAlt: string;
  onPosterClick: () => void;
  onPosterLoad: PosterEventHandler;
  onPosterError: PosterEventHandler;
  imdb?: MovieCardImdb;
  seeders?: number;
  indexer?: string;
  /** Short quality label such as "1080p" (badge on the poster). */
  quality?: string;
  title: string;
  titleHref: string;
  year?: number | string | null;
  onClose: () => void;
  variants: MoviePrepareCardVariant[];
  selectedVariantKey: string;
  onSelectVariant: (key: string) => void;
  variantsAriaLabel: string;
  /** Streaming services resolved for this movie. */
  providers?: MovieCardProvider[];
  /** Set once the torrent has been prepared in Seedr. */
  prepared: null | {
    isPlaying: boolean;
    isCopied: boolean;
    onPlay: () => void;
    onDownload: () => void;
    onCopy: () => void;
  };
  directStream: { label: string; disabled: boolean; onClick: () => void };
  prepare: { isPreparing: boolean; disabled: boolean; onClick: () => void };
  className?: string;
}

export const MoviePrepareCard: React.FC<MoviePrepareCardProps> = ({
  posterSrc,
  posterAlt,
  onPosterClick,
  onPosterLoad,
  onPosterError,
  imdb,
  seeders,
  indexer,
  quality,
  title,
  titleHref,
  year,
  onClose,
  variants,
  selectedVariantKey,
  onSelectVariant,
  variantsAriaLabel,
  providers = [],
  prepared,
  directStream,
  prepare,
  className = '',
}) => {
  const showImdb = hasImdbRating(imdb);
  return (
    <div
      className={[
        'relative min-w-0 rounded-xl border border-slate-800 bg-slate-900 p-2 hover:bg-slate-800/80 transition sm:rounded-none sm:border-0 sm:bg-transparent sm:p-2 sm:px-4 sm:py-4 lg:flex lg:flex-col lg:self-start lg:h-fit lg:rounded-[14px] lg:border lg:border-slate-800 lg:bg-slate-900 lg:p-0 lg:hover:-translate-y-1 lg:hover:border-slate-700',
        className || 'z-0',
      ].join(' ')}
    >
      <div className="flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-3 lg:flex-col lg:items-stretch lg:gap-0">
        <div className="min-w-0 flex-1 lg:w-full">
          <div className="flex flex-col sm:flex-row items-stretch sm:items-start gap-2 sm:gap-3 lg:flex-col lg:gap-0">
            <div data-movie-poster-slot="true" data-poster-waiting={!posterSrc ? "true" : "false"} className="relative w-full sm:w-24 shrink-0 aspect-[2/3] rounded-lg overflow-hidden border border-slate-800 bg-slate-950 shadow-md lg:w-full lg:rounded-none lg:border-0 lg:shadow-none">
              {posterSrc ? (
                <>
                  <img
                    data-movie-poster-stage="true"
                    src={posterSrc}
                    alt={posterAlt}
                    loading="eager"
                    fetchPriority="high"
                    className="w-full h-full object-cover cursor-pointer"
                    onClick={onPosterClick}
                    onLoad={event => {
                      const image = event.currentTarget;
                      image.dataset.posterQuality = image.dataset.highQualityTried === 'true' ? 'high-loaded' : 'low-loaded';
                      onPosterLoad(event);
                      requestHighQualityPoster(image);
                    }}
                    onError={event => {
                      const image = event.currentTarget;
                      const previousSrc = image.src;
                      if (image.dataset.highQualityTried === 'true') {
                        requestHighQualityPoster(image);
                        image.dataset.highQualityTried = '';
                        image.dataset.highQualityFailed = 'true';
                        try {
                          const parsed = new URL(image.src || image.currentSrc, window.location.origin);
                          if (parsed.pathname.replace(/\/+$/, '') === '/api/poster' && parsed.searchParams.get('size') === 'w780') {
                            parsed.searchParams.set('size', 'w185');
                            image.dataset.posterQuality = 'low-retrying';
                            image.src = parsed.toString();
                            return;
                          }
                        } catch {
                          // Use the direct TMDB fallback below.
                        }
                        if (image.src.includes('/t/p/w780/')) {
                          image.dataset.posterQuality = 'low-retrying';
                          image.src = image.src.replace('/t/p/w780/', '/t/p/w185/');
                          return;
                        }
                      }
                      onPosterError(event);
                      image.dataset.posterQuality = image.src !== previousSrc ? 'low-retrying' : 'low-failed';
                    }}
                  />
                  <div data-poster-placeholder="true" onClick={onPosterClick} className="hidden cursor-pointer absolute inset-0 flex items-center justify-center bg-slate-950 text-slate-500">
                    <Film className="w-7 h-7" />
                  </div>
                </>
              ) : (
                <div className="absolute inset-0 flex items-center justify-center bg-slate-950 text-slate-700">
                  <Film className="w-7 h-7" />
                </div>
              )}

              {showImdb && (
                <a
                  href={imdbBadgeHref(imdb)}
                  target="_blank"
                  rel="noreferrer"
                  title={imdbBadgeTitle(imdb)}
                  className="absolute left-2 top-2 rounded-md border border-amber-300/60 bg-slate-950/95 px-2 py-1 text-[10px] font-bold text-amber-300 shadow"
                >
                  ⭐ {imdb.imdbRating.toFixed(1)}/10
                </a>
              )}
              {Number(seeders) > 0 && (
                <div className={'absolute left-2 inline-flex items-center gap-1 rounded-md bg-black/80 px-2 py-1 text-[10px] font-bold text-emerald-400 ' + (showImdb ? 'top-10' : 'top-2')}>
                  <Users className="w-3 h-3" />
                  {seeders}
                </div>
              )}

              {indexer && (
                <div
                  className="absolute right-2 top-2 max-w-[70%] truncate rounded-md bg-black/75 px-2 py-1 text-[10px] font-medium text-slate-300"
                  title={indexer}
                >
                  {indexer}
                </div>
              )}

              {quality && (
                <div className="absolute left-2 bottom-2 rounded-md border border-cyan-400/30 bg-slate-950/85 px-2 py-1 text-[10px] font-bold text-cyan-300">
                  {quality}
                </div>
              )}
            </div>

            <div className="min-w-0 flex-1 lg:w-full lg:flex-none lg:p-3">
              <div className="flex items-start gap-2">
                <div className="hidden sm:flex lg:hidden p-1.5 rounded-lg bg-cyan-500/10 border border-cyan-500/20 shrink-0">
                  <Database className="w-3.5 h-3.5 text-cyan-400" />
                </div>
                <div className="relative min-w-0 flex-1 z-10">
                  <div className="flex items-start gap-1.5">
                    <div className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-1.5">
                      <a
                        href={titleHref}
                        target="_blank"
                        rel="noreferrer"
                        className="min-w-0 text-left text-[11px] leading-4 sm:text-sm font-semibold text-slate-100 hover:text-cyan-300 transition"
                        title="Open on IMDb"
                      >
                        {title}
                      </a>
                      {year ? <span className="shrink-0 text-[10px] sm:text-xs text-slate-500">{year}</span> : null}
                    </div>
                    <button
                      type="button"
                      onClick={onClose}
                      className="shrink-0 rounded-md p-1 text-slate-500 hover:bg-slate-800 hover:text-slate-100"
                      title="Back to search cards"
                      aria-label="Back to search cards"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>

                  {providers.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1" aria-label={`Streaming services for ${title}`}>
                      {providers.slice(0, 5).map(provider => (
                        <a key={provider.key} href={provider.webUrl || undefined} target={provider.webUrl ? '_blank' : undefined}
                          rel={provider.webUrl ? 'noreferrer' : undefined} title={provider.name}
                          className="rounded-full border border-cyan-900/70 bg-cyan-950/40 px-1.5 py-0.5 text-[10px] leading-4 text-cyan-200">
                          {provider.name}
                        </a>
                      ))}
                    </div>
                  )}

                  {variants.length > 1 && (
                    <select
                      aria-label={variantsAriaLabel}
                      value={selectedVariantKey}
                      onChange={event => onSelectVariant(event.target.value)}
                      className="mt-2 w-full min-w-0 rounded-lg border border-slate-800 bg-slate-950 px-2 py-2 text-[10px] sm:text-xs font-medium text-slate-200 outline-none focus:border-cyan-500"
                      title="Choose quality and torrent source"
                    >
                      {variants.map(variant => (
                        <option key={variant.key} value={variant.key}>
                          {variant.label}
                        </option>
                      ))}
                    </select>
                  )}
                </div>
              </div>

              <div className="mt-2 flex items-center justify-between gap-2">
                {prepared ? (
                  <div className="min-w-0 max-w-full flex flex-wrap items-center justify-end gap-1.5">
                    <button
                      type="button"
                      disabled={prepared.isPlaying}
                      onClick={prepared.onPlay}
                      className="shrink-0 px-2 py-1.5 rounded-lg bg-emerald-400 text-slate-950 font-bold text-xs hover:bg-emerald-300 transition flex items-center gap-1 disabled:opacity-70 disabled:cursor-wait whitespace-nowrap"
                      title={prepared.isPlaying ? 'Opening stream…' : 'Play from Seedr'}
                    >
                      {prepared.isPlaying
                        ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        : <Play className="w-3.5 h-3.5" />}
                    </button>

                    <button
                      type="button"
                      onClick={prepared.onDownload}
                      className="shrink-0 p-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white transition"
                      title="Download file"
                      aria-label="Download file"
                    >
                      <Download className="w-3.5 h-3.5" />
                    </button>

                    <button
                      type="button"
                      disabled={prepared.isCopied}
                      onClick={prepared.onCopy}
                      className="shrink-0 p-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-cyan-400 transition disabled:opacity-60"
                      title="Copy download link"
                      aria-label="Copy download link"
                    >
                      {prepared.isCopied
                        ? <Check className="w-3.5 h-3.5 text-emerald-400" />
                        : <Copy className="w-3.5 h-3.5" />}
                    </button>
                  </div>
                ) : (
                  <div className="min-w-0 max-w-full">
                    <div className="flex w-full items-center justify-between gap-2">
                      <button
                        type="button"
                        disabled={directStream.disabled}
                        onClick={directStream.onClick}
                        className="min-w-0 flex-1 whitespace-nowrap rounded-lg border border-violet-400/40 bg-violet-500/15 px-1.5 py-1.5 text-[10px] font-bold text-violet-200 hover:bg-violet-500/25 disabled:opacity-60"
                      >
                        {directStream.label}
                      </button>
                      <div className="shrink-0">
                        <button
                          type="button"
                          disabled={prepare.disabled}
                          onClick={prepare.onClick}
                          className="shrink-0 whitespace-nowrap px-3 py-1.5 rounded-lg bg-cyan-500 hover:bg-cyan-400 disabled:opacity-60 disabled:cursor-not-allowed text-slate-950 text-[11px] font-bold transition"
                        >
                          {prepare.isPreparing ? 'Preparing…' : 'Prepare'}
                        </button>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
