"""Pure query parsing and result-quality rules for media searches."""

from html import unescape
import json
import re
from pathlib import Path
from typing import Any


_SEARCH_LIMITS_CANDIDATES = (
    Path(__file__).resolve().parent / "search_limits.json",
    Path(__file__).resolve().parent.parent / "search_limits.json",
)
_SEARCH_LIMITS = {}
for _limits_path in _SEARCH_LIMITS_CANDIDATES:
    try:
        with _limits_path.open("r", encoding="utf-8") as _limits_file:
            _SEARCH_LIMITS = json.load(_limits_file)
        break
    except (OSError, json.JSONDecodeError):
        continue

MIN_SEARCH_RESULT_SIZE_BYTES = max(0, int(_SEARCH_LIMITS.get("minSizeMb", 100))) * 1024 * 1024
MAX_SEARCH_RESULT_SIZE_BYTES = max(
    MIN_SEARCH_RESULT_SIZE_BYTES,
    int(_SEARCH_LIMITS.get("maxSizeGb", 5)) * 1024 * 1024 * 1024,
)
SEARCH_COMPOUND_ALIASES = {
    "antman": "ant man",
    "spiderman": "spider man",
    "ironman": "iron man",
    "blackpanther": "black panther",
    "doctorstrange": "doctor strange",
    "captainamerica": "captain america",
    "guardiansofthegalaxy": "guardians of the galaxy",
}


def search_tokens(value: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", value.lower()) if token]


def media_search_parts(value: str) -> tuple[str, int | None, int | None]:
    """Split a media query into title text plus optional season/episode constraints."""
    query = re.sub(r"\s+", " ", value.strip())
    season_match = re.search(r"\b(?:season|series)\s*(\d{1,2})\b", query, re.I)
    episode_match = re.search(r"\bS(\d{1,2})(?:E(\d{1,3}))?\b", query, re.I)
    season = int(season_match.group(1)) if season_match else (
        int(episode_match.group(1)) if episode_match else None
    )
    episode = int(episode_match.group(2)) if episode_match and episode_match.group(2) else None
    title = query
    title = re.sub(r"\b(?:season|series)\s*\d{1,2}\b", " ", title, flags=re.I)
    title = re.sub(r"\bS\d{1,2}(?:E\d{1,3})?\b", " ", title, flags=re.I)
    title = re.sub(
        r"\b(?:19|20)\d{2}\b|"
        r"\b(?:2160p|1440p|1080p|720p|480p|4k|8k)\b|"
        r"\b(?:webrip|web-dl|bluray|brrip|x264|x265|h264|h265|hevc|hdr)\b",
        " ", title, flags=re.I,
    )
    return re.sub(r"\s+", " ", title).strip(), season, episode


def season_episode_match(title: str, season: int | None, episode: int | None) -> bool:
    if season is None:
        return True
    upper = title.upper()
    match = re.search(r"\bS(\d{1,2})(?:E(\d{1,3}))?\b", upper)
    if match:
        if int(match.group(1)) != season:
            return False
        if episode is not None and (not match.group(2) or int(match.group(2)) != episode):
            return False
        return True
    season_word = re.search(r"\bSEASON[\s._-]*(\d{1,2})\b", upper)
    return bool(season_word and int(season_word.group(1)) == season and episode is None)


def normalize_title(value: str) -> str:
    return " ".join(search_tokens(value))


def search_media_kind(query: str) -> str:
    """Classify enough to select strict media sources; never opens generic categories."""
    if re.search(r"\b(?:S\d{1,2}(?:E\d{1,3})?|season\s*\d+|episode\s*\d+)\b", query.strip(), re.I):
        return "tv"
    return "both"


def expand_compound_search_title(value: str) -> str:
    title = str(value or "")
    for compact, canonical in SEARCH_COMPOUND_ALIASES.items():
        title = re.sub(rf"(?<![a-z]){re.escape(compact)}(?![a-z])", canonical, title, flags=re.I)
    return title


def search_query_constraints(value: str) -> tuple[int | None, list[str]]:
    """Extract constraints that should affect matching but not title search."""
    years = re.findall(r"\b((?:19|20)\d{2})\b", value)
    year = int(years[-1]) if years else None
    aliases = (
        "hindi", "tamil", "telugu", "malayalam", "kannada", "bengali",
        "marathi", "punjabi", "gujarati", "urdu", "dual audio", "multi audio",
        "dubbed", "dub",
    )
    lower = value.lower()
    languages = [term for term in aliases if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", lower)]
    return year, languages


def media_provider_query(value: str) -> str:
    """Return the title portion used for provider searches, excluding qualifiers."""
    title, _season, _episode = media_search_parts(value)
    _year, languages = search_query_constraints(value)
    for language in sorted(languages, key=len, reverse=True):
        title = re.sub(rf"(?<![a-z]){re.escape(language)}(?![a-z])", " ", title, flags=re.I)
    title = expand_compound_search_title(title)
    title = re.sub(r"\bspider[- ]?man\b", "spider man", title, flags=re.I)
    title = re.sub(r"\bant[- ]?man\b", "ant man", title, flags=re.I)
    return re.sub(r"\s+", " ", title).strip()


def constraint_matches_title(title: str, query: str) -> bool:
    """Match year/language qualifiers without making normal title search brittle."""
    year, languages = search_query_constraints(query)
    normalized = normalize_title(title)
    lower = title.lower()
    if year is not None:
        title_years = {int(value) for value in re.findall(r"\b((?:19|20)\d{2})\b", title)}
        if year not in title_years:
            return False
    language_aliases = {
        "hindi": (r"(?<![a-z])hindi(?![a-z])", r"(?<![a-z])hin(?![a-z])"),
        "tamil": (r"(?<![a-z])tamil(?![a-z])", r"(?<![a-z])tam(?![a-z])"),
        "telugu": (r"(?<![a-z])telugu(?![a-z])", r"(?<![a-z])tel(?![a-z])"),
        "malayalam": (r"(?<![a-z])malayalam(?![a-z])", r"(?<![a-z])mal(?![a-z])"),
        "kannada": (r"(?<![a-z])kannada(?![a-z])", r"(?<![a-z])kan(?![a-z])"),
        "bengali": (r"(?<![a-z])bengali(?![a-z])", r"(?<![a-z])ben(?![a-z])"),
        "marathi": (r"(?<![a-z])marathi(?![a-z])", r"(?<![a-z])mar(?![a-z])"),
        "punjabi": (r"(?<![a-z])punjabi(?![a-z])", r"(?<![a-z])pun(?![a-z])"),
        "gujarati": (r"(?<![a-z])gujarati(?![a-z])", r"(?<![a-z])guj(?![a-z])"),
        "urdu": (r"(?<![a-z])urdu(?![a-z])", r"(?<![a-z])urd(?![a-z])"),
    }
    for language in languages:
        patterns = language_aliases.get(language)
        if patterns:
            if not any(re.search(pattern, lower, re.I) for pattern in patterns):
                return False
        elif language not in lower and language.replace(" ", "") not in normalized.replace(" ", ""):
            return False
    return True


def search_quality_filter(item: dict[str, Any], query: str) -> bool:
    """Apply media, size, and query-qualifier gates to a search result."""
    title = str(item.get("title") or "").strip()
    if not title:
        return False
    normalized_tokens = set(normalize_title(title).split())
    _query_title, season, episode = media_search_parts(query)
    target_tokens = search_tokens(media_provider_query(query))
    if target_tokens and not all(token in normalized_tokens for token in target_tokens):
        return False
    if not constraint_matches_title(title, query):
        return False
    if season is not None and not season_episode_match(title, season, episode):
        return False
    blocked = ("game", "software", "application", "music", "audio", "book", "ebook", "porn", "xxx", "adult", "anime")
    if any(word in str(item.get("category") or "").lower() for word in blocked):
        return False
    size = int(item.get("size") or 0)
    return MIN_SEARCH_RESULT_SIZE_BYTES <= size <= MAX_SEARCH_RESULT_SIZE_BYTES


def title_relevance(title: str, query: str) -> tuple[int, int]:
    """Prefer titles that begin with the searched title, ignoring leading articles."""
    normalized = normalize_title(title)
    target = normalize_title(media_search_parts(query)[0])
    if not target:
        return (0, 0)
    candidate = re.sub(r"^(?:the|a|an)\s+", "", normalized)
    if candidate == target:
        return (4, len(target))
    if candidate.startswith(target + " "):
        return (3, len(target))
    candidate_tokens = candidate.split()
    target_tokens = target.split()
    if not target_tokens:
        return (0, 0)
    for index in range(max(0, len(candidate_tokens) - len(target_tokens) + 1)):
        if candidate_tokens[index:index + len(target_tokens)] == target_tokens:
            return (2, len(target))
    if all(token in candidate_tokens for token in target_tokens):
        return (1, len(target))
    return (0, 0)


def release_quality_score(title: str) -> int:
    """Estimate release quality from common filename tags; not a safety/trust score."""
    lower = unescape(str(title or "")).lower()
    score = 0
    if re.search(r"\b(?:2160p|4k|uhd)\b", lower):
        score += 8
    elif re.search(r"\b1080p\b", lower):
        score += 6
    elif re.search(r"\b720p\b", lower):
        score += 4
    elif re.search(r"\b480p\b", lower):
        score += 2
    if re.search(r"\b(?:blu[ .-]?ray|bdrip|brrip|remux)\b", lower):
        score += 4
    elif re.search(r"\b(?:web[ .-]?dl|webdl|webrip)\b", lower):
        score += 3
    elif re.search(r"\bhdtv\b", lower):
        score += 2
    elif re.search(r"\b(?:dvdrip|dvd)\b", lower):
        score += 1
    if re.search(r"\b(?:x265|h265|hevc|av1)\b", lower):
        score += 1
    if re.search(r"\b(?:x264|h264)\b", lower):
        score += 1
    if re.search(r"\b(?:hdr10?|dolby[ .]?vision|10bit)\b", lower):
        score += 1
    if re.search(r"\b(?:hdcam|camrip|cam|telesync|telecine|ts|dvdscr|screener)\b", lower):
        score -= 8
    elif re.search(r"\br5\b", lower):
        score -= 2
    return score


def media_provider_queries(value: str) -> list[str]:
    """Build a small title-variant set without exploding provider traffic."""
    title, _season, _episode = media_search_parts(value)
    _year, languages = search_query_constraints(value)
    raw_title = title
    for language in sorted(languages, key=len, reverse=True):
        raw_title = re.sub(rf"(?<![a-z]){re.escape(language)}(?![a-z])", " ", raw_title, flags=re.I)
    raw_title = re.sub(r"\s+", " ", raw_title).strip()
    canonical = media_provider_query(value)
    language_terms = [language for language in languages if language in {
        "hindi", "tamil", "telugu", "malayalam", "kannada", "bengali", "marathi", "punjabi", "gujarati", "urdu",
    }]
    language_suffix = " ".join(language_terms)
    canonical_with_language = re.sub(r"\s+", " ", f"{canonical} {language_suffix}".strip())
    variants: list[str] = []
    seen: set[str] = set()
    for candidate in (canonical_with_language, canonical, raw_title, f"{raw_title} {language_suffix}".strip()):
        candidate = re.sub(r"\s+", " ", candidate or "").strip()
        lowered = candidate.lower()
        if candidate and lowered not in seen:
            seen.add(lowered)
            variants.append(candidate)
    tokens = search_tokens(raw_title)
    if len(tokens) >= 4:
        anchor = " ".join(tokens[:3])
        if anchor and anchor.lower() not in {item.lower() for item in variants}:
            variants.append(anchor)
    return variants[:3]