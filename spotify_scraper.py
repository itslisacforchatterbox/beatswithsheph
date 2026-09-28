"""
spotify_scraper.py - Local Zero-Cost Spotify Public Metrics Scraper

Extracts public artist metrics from Spotify's web player infrastructure without an
authenticated API app token, stores dated snapshots in SQLite (socials_cache.db), and
builds every number + commentary line the "SPOTIFY PERFORMANCE" section of the
Artist Performance Report template asks for.

USAGE
    python spotify_scraper.py <ARTIST_ID>                         # scrape + save + report (last 7 days)
    python spotify_scraper.py <ARTIST_ID> --period-start 2026-09-22 --period-end 2026-09-28
    python spotify_scraper.py <ARTIST_ID> --manual manual.json --goals goals.json
    python spotify_scraper.py <ARTIST_ID> --report-only --period-end 2026-09-21   # rebuild a past report from the DB

    Run it DAILY (cron / Task Scheduler): "Previous" and "Delta" come from your own
    stored snapshots, so the first week only builds the baseline.

manual.json (optional - values only Spotify for Artists can give you):
    {"total_streams_7d":   {"current": 12400, "previous": 11000},
     "playlist_additions": {"current": 6,     "previous": 4},
     "saves":              {"current": 830,   "previous": 790},
     "demographics": "62% aged 18-24; 55% female"}

goals.json (optional):
    {"monthly_listeners": 50000, "followers": 3000, "streams_7d": 15000}

Requires spotify_commentary.py in the same folder.
"""

import argparse
import json
import logging
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import spotify_commentary as commentary

# Attempt import of curl_cffi for TLS fingerprint impersonation; fall back to requests
try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests as cffi_requests
    HAS_CURL_CFFI = False

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# Constants & Configuration
DEFAULT_DB_PATH = "socials_cache.db"
TARGET_ARTIST_ID = "3fNvLxWjqCfeHwBYtHmuGI"  # K.LAW (Joshua Kalaw)

PATHFINDER_URL = "https://api-partner.spotify.com/pathfinder/v1/query"

# Persisted query hashes for queryArtistOverview known to Spotify's web client
ARTIST_OVERVIEW_HASHES = [
    "433e28d1e949372d3ca3aa6c47975cff428b5dc37b12f5325d9213accadf770a",
    "d66221ea13998b2f81883c5187d174c8646e4041d67f5b1e103bc262d447e3a0",
    "35a699e12a728c1a02f5bf67121a50f87341e65054e13126c03b7697fbd26692"
]

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://open.spotify.com",
    "Referer": "https://open.spotify.com/",
    "app-platform": "WebPlayer"
}


def _get_session():
    """Initializes a requests or curl_cffi session with browser emulation."""
    if HAS_CURL_CFFI:
        return cffi_requests.Session(impersonate="chrome124")
    session = cffi_requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


def fetch_anonymous_token_and_embed_data(artist_id: str) -> Tuple[Optional[str], Optional[dict]]:
    """
    Scrapes the Spotify Embed page for the artist.
    Extracts the anonymous Bearer token and fallback SSR hydration entity data.
    """
    embed_url = f"https://open.spotify.com/embed/artist/{artist_id}"
    session = _get_session()

    try:
        logger.info("Harvesting anonymous token from embed page: %s", embed_url)
        response = session.get(embed_url, timeout=12)
        if response.status_code != 200:
            logger.warning("Embed page returned HTTP %d", response.status_code)
            return None, None

        html = response.text
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if not match:
            logger.warning("Could not locate __NEXT_DATA__ in embed page HTML.")
            return None, None

        data = json.loads(match.group(1))
        page_props = data.get("props", {}).get("pageProps", {})

        # Extract anonymous accessToken from state settings session
        token = (
            page_props.get("state", {})
            .get("settings", {})
            .get("session", {})
            .get("accessToken")
        )

        # In case the JSON structure slightly shifts, search recursively
        if not token:
            def find_key(obj, target):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k == target and isinstance(v, str):
                            return v
                        res = find_key(v, target)
                        if res:
                            return res
                elif isinstance(obj, list):
                    for item in obj:
                        res = find_key(item, target)
                        if res:
                            return res
                return None
            token = find_key(data, "accessToken")

        embed_entity = page_props.get("state", {}).get("data", {}).get("entity")
        return token, embed_entity

    except Exception as exc:
        logger.error("Error retrieving anonymous token: %s", exc)
        return None, None


def fetch_artist_overview_graphql(artist_id: str, access_token: str) -> Optional[dict]:
    """
    Queries Spotify's Pathfinder GraphQL API using the anonymous Bearer token.
    """
    session = _get_session()
    headers = {
        **DEFAULT_HEADERS,
        "Authorization": f"Bearer {access_token}",
        "content-type": "application/json;charset=UTF-8"
    }

    variables = {
        "uri": f"spotify:artist:{artist_id}",
        "locale": "",
        "includePrerelease": True
    }

    for sha256_hash in ARTIST_OVERVIEW_HASHES:
        params = {
            "operationName": "queryArtistOverview",
            "variables": json.dumps(variables),
            "extensions": json.dumps({
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": sha256_hash
                }
            })
        }

        try:
            resp = session.get(PATHFINDER_URL, params=params, headers=headers, timeout=12)
            if resp.status_code == 200:
                result = resp.json()
                if "data" in result and result["data"]:
                    logger.info("Successfully fetched GraphQL overview with hash %s...", sha256_hash[:8])
                    return result["data"]
            elif resp.status_code == 400:
                # Hash mismatch; continue to next hash
                continue
            else:
                logger.warning("GraphQL query returned HTTP %d with hash %s...", resp.status_code, sha256_hash[:8])
        except Exception as exc:
            logger.warning("GraphQL request error with hash %s: %s", sha256_hash[:8], exc)

    return None


def fetch_artist_page_fallback(artist_id: str) -> Dict[str, int]:
    """
    Scrapes the public artist HTML profile to extract monthly listeners
    from Open Graph or meta descriptions if GraphQL is unreachable.
    """
    artist_url = f"https://open.spotify.com/artist/{artist_id}"
    session = _get_session()
    metrics = {"monthly_listeners": 0, "followers": 0}

    try:
        resp = session.get(artist_url, timeout=12)
        if resp.status_code == 200:
            html = resp.text
            # Look for patterns like "15,200 monthly listeners" or "1.5M monthly listeners"
            match = re.search(r'([\d,\.]+[KkMmBb]?)\s+monthly\s+listeners', html, re.IGNORECASE)
            if match:
                raw_str = match.group(1).replace(",", "").strip()
                if raw_str.upper().endswith("K"):
                    metrics["monthly_listeners"] = int(float(raw_str[:-1]) * 1_000)
                elif raw_str.upper().endswith("M"):
                    metrics["monthly_listeners"] = int(float(raw_str[:-1]) * 1_000_000)
                elif raw_str.upper().endswith("B"):
                    metrics["monthly_listeners"] = int(float(raw_str[:-1]) * 1_000_000_000)
                else:
                    metrics["monthly_listeners"] = int(float(raw_str))
    except Exception as exc:
        logger.error("Fallback artist page scraping failed: %s", exc)

    return metrics


# =============================================================================
# Parsing helpers (defensive: Spotify's internal JSON shape shifts between releases)
# =============================================================================
OFFICIAL_OWNER_NAMES = {"spotify"}
OFFICIAL_ID_PREFIX = "37i9dQZF1D"   # ids Spotify uses for its own editorial/algorithmic playlists
MIN_BASELINE_DAYS = 1.0             # a "previous" snapshot must be at least this old


def _dig(obj: Any, *keys: Any, default: Any = None) -> Any:
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        elif isinstance(obj, list) and isinstance(k, int) and 0 <= k < len(obj):
            obj = obj[k]
        else:
            return default
        if obj is None:
            return default
    return obj


def _to_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_playlists(related: dict) -> List[Dict[str, Any]]:
    out, seen = [], set()
    for kind in ("discoveredOnV2", "discoveredOn", "featuringV2", "featuring", "playlists"):
        for item in _dig(related, kind, "items", default=[]) or []:
            data = item.get("data") if isinstance(item.get("data"), dict) else item
            uri = data.get("uri") or item.get("uri") or ""
            name = data.get("name")
            if not name or ":playlist:" not in uri or uri in seen:
                continue
            seen.add(uri)
            owner = (_dig(data, "ownerV2", "data", "name") or _dig(data, "owner", "name")
                     or _dig(data, "owner", "display_name") or "")
            pid = uri.split(":")[-1]
            official = (owner.strip().lower() in OFFICIAL_OWNER_NAMES
                        or uri.startswith("spotify:user:spotify:") or pid.startswith(OFFICIAL_ID_PREFIX))
            out.append({"name": name, "uri": uri, "owner": owner, "official": bool(official),
                        "kind": "discovered_on" if kind.startswith("discovered") else "featuring"})
    return out


def _parse_release_date(d: Any) -> Optional[str]:
    if not isinstance(d, dict):
        return None
    iso = d.get("isoString")
    if iso:
        return iso[:10]
    y, m, dd = d.get("year"), d.get("month") or 1, d.get("day") or 1
    return f"{int(y):04d}-{int(m):02d}-{int(dd):02d}" if y else None


def parse_artist_union(au: dict) -> Dict[str, Any]:
    """Normalises Pathfinder's artistUnion into flat fields."""
    tracks = []
    for item in _dig(au, "discography", "topTracks", "items", default=[]) or []:
        t = item.get("track", {}) or {}
        streams = _to_int(t.get("playcount"))
        if streams is not None and t.get("name"):
            tracks.append({"uri": t.get("uri") or t.get("name"), "name": t["name"], "streams": streams})

    cities = []
    for c in _dig(au, "stats", "topCities", "items", default=[]) or []:
        n = _to_int(c.get("numberOfListeners"))
        if c.get("city") and n is not None:
            cities.append({"city": c["city"], "country": c.get("country") or "",
                           "region": c.get("region") or "", "listeners": n})

    latest = _dig(au, "discography", "latest") or {}
    release = None
    if latest.get("name"):
        release = {"name": latest["name"], "type": latest.get("type") or "",
                   "date": _parse_release_date(latest.get("date"))}

    return {
        "name": _dig(au, "profile", "name"),
        "monthly_listeners": _to_int(_dig(au, "stats", "monthlyListeners")) or 0,
        "followers": _to_int(_dig(au, "stats", "followers")) or 0,
        "world_rank": _to_int(_dig(au, "stats", "worldRank")),
        "top_tracks": tracks, "top_cities": cities,
        "playlists": _parse_playlists(au.get("relatedContent") or {}),
        "latest_release": release,
    }


# =============================================================================
# Scrape
# =============================================================================
def scrape_spotify_artist(artist_id: str) -> Dict[str, Any]:
    """Coordinates extraction, parsing, and normalization into one snapshot dict."""
    token, embed_entity = fetch_anonymous_token_and_embed_data(artist_id)

    parsed: Dict[str, Any] = {}
    graphql_success = False
    if token:
        overview_data = fetch_artist_overview_graphql(artist_id, token)
        if overview_data:
            au = overview_data.get("artistUnion") or overview_data.get("artist") or {}
            parsed = parse_artist_union(au)
            graphql_success = True

    monthly_listeners = parsed.get("monthly_listeners", 0)
    followers = parsed.get("followers", 0)

    # Fallback to main HTML if GraphQL failed or returned empty
    if not graphql_success or (monthly_listeners == 0 and followers == 0):
        logger.info("Applying fallback parsing from HTML/embed entities...")
        fb = fetch_artist_page_fallback(artist_id)
        if monthly_listeners == 0:
            monthly_listeners = fb.get("monthly_listeners", 0)

    tracks = parsed.get("top_tracks", [])
    quality = "full" if (graphql_success and (monthly_listeners or followers)) else (
        "partial" if monthly_listeners else "failed")

    name = parsed.get("name") or (embed_entity.get("name") if isinstance(embed_entity, dict) else None)

    return {
        "artist_id": str(artist_id),
        "artist_name": name,
        "spotify_monthly_listeners": int(monthly_listeners),
        "spotify_followers": int(followers),
        "spotify_top_track_1_streams": int(tracks[0]["streams"]) if tracks else 0,
        "spotify_top_10_cumulative_streams": int(sum(t["streams"] for t in tracks[:10])),
        "spotify_top_track_1_name": tracks[0]["name"] if tracks else None,
        "spotify_top_tracks": tracks[:10],
        "spotify_world_rank": parsed.get("world_rank"),
        "spotify_top_cities": parsed.get("top_cities", []),
        "spotify_playlists": parsed.get("playlists", []),
        "spotify_latest_release": parsed.get("latest_release"),
        "data_quality": quality,   # full | partial (listeners only) | failed
    }


# =============================================================================
# Database
# =============================================================================
NUMERIC_KEYS = ["spotify_monthly_listeners", "spotify_followers",
                "spotify_top_track_1_streams", "spotify_top_10_cumulative_streams"]


def init_db(conn: sqlite3.Connection) -> None:
    """Ensures cache and historical tables exist before running queries."""
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metrics_cache (
                channel_id TEXT PRIMARY KEY,
                data TEXT,
                updated_at TIMESTAMP
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS historical_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT,
                entity_id TEXT,
                data TEXT,
                extracted_at TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hist ON historical_metrics(platform, entity_id, extracted_at)")


def get_fresh_cache(conn: sqlite3.Connection, artist_id: str,
                    ttl_hours: float = 24.0) -> Optional[Tuple[Dict[str, Any], datetime]]:
    """Returns (data, updated_at) if the cached snapshot is younger than the TTL."""
    row = conn.execute("SELECT data, updated_at FROM metrics_cache WHERE channel_id = ?", (artist_id,)).fetchone()
    if not row:
        return None
    ts = datetime.fromisoformat(row[1])
    if datetime.now(timezone.utc) - ts < timedelta(hours=ttl_hours):
        return json.loads(row[0]), ts
    return None


def save_to_cache(conn: sqlite3.Connection, artist_id: str, new_data: Dict[str, Any],
                  extracted_at: Optional[datetime] = None) -> None:
    """
    Saves metrics with delta calculation and deduplication:
    1. Compares against existing metrics_cache.
    2. Computes deltas (e.g., diff in followers, streams).
    3. Always UPSERTs metrics_cache (for TTL freshness).
    4. Only INSERTS into historical_metrics if any stored field has actually changed.
    """
    now_iso = _iso(extracted_at or datetime.now(timezone.utc))

    with conn:  # Atomic transaction
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM metrics_cache WHERE channel_id = ?", (artist_id,))
        row = cursor.fetchone()

        deltas: Dict[str, int] = {}
        if row:
            old_data = json.loads(row[0])
            has_changed = old_data != new_data
            for key in NUMERIC_KEYS:
                deltas[f"delta_{key}"] = (new_data.get(key) or 0) - (old_data.get(key) or 0)
        else:
            has_changed = True
            for key in NUMERIC_KEYS:
                deltas[f"delta_{key}"] = new_data.get(key, 0)

        new_data_json = json.dumps(new_data, ensure_ascii=False)
        hist_data_json = json.dumps({**new_data, "deltas": deltas}, ensure_ascii=False)

        cursor.execute(
            """
            INSERT INTO metrics_cache (channel_id, data, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                data = excluded.data,
                updated_at = excluded.updated_at
            """,
            (artist_id, new_data_json, now_iso)
        )

        if has_changed:
            cursor.execute(
                """
                INSERT INTO historical_metrics (id, platform, entity_id, data, extracted_at)
                VALUES (NULL, 'spotify', ?, ?, ?)
                """,
                (artist_id, hist_data_json, now_iso)
            )
            logger.info("Metrics changed for %s: recorded new historical entry with deltas.", artist_id)
        else:
            logger.info("No metrics changed for %s: updated TTL without duplicate historical entry.", artist_id)


def _row_to_snapshot(row) -> Optional[Tuple[Dict[str, Any], datetime]]:
    return (json.loads(row[0]), datetime.fromisoformat(row[1])) if row else None


def get_snapshot_at(conn: sqlite3.Connection, artist_id: str, when: datetime,
                    before: Optional[datetime] = None) -> Optional[Tuple[Dict[str, Any], datetime]]:
    """Latest stored snapshot at or before `when` (and strictly before `before`, if given)."""
    q = ("SELECT data, extracted_at FROM historical_metrics WHERE platform='spotify' "
         "AND entity_id=? AND extracted_at<=?")
    args: List[Any] = [artist_id, _iso(when)]
    if before is not None:
        q += " AND extracted_at<?"
        args.append(_iso(before))
    return _row_to_snapshot(conn.execute(q + " ORDER BY extracted_at DESC LIMIT 1", args).fetchone())


def get_earliest_snapshot(conn: sqlite3.Connection, artist_id: str,
                          before: datetime) -> Optional[Tuple[Dict[str, Any], datetime]]:
    return _row_to_snapshot(conn.execute(
        "SELECT data, extracted_at FROM historical_metrics WHERE platform='spotify' AND entity_id=? "
        "AND extracted_at<? ORDER BY extracted_at ASC LIMIT 1", (artist_id, _iso(before))).fetchone())


# =============================================================================
# Report building
# =============================================================================
def _pct(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / prev * 100.0


def streams_gained(now_tracks: List[dict], before_tracks: List[dict]) -> Optional[int]:
    """
    Streams gained between two snapshots by tracks present in BOTH top-10 lists
    (matched by uri). Robust to tracks entering/leaving the top 10. A lower bound
    on the artist's true streams for the window.
    """
    if not now_tracks or not before_tracks:
        return None
    base = {t["uri"]: t["streams"] for t in before_tracks}
    common = [t for t in now_tracks if t["uri"] in base]
    if not common:
        return None
    return sum(max(t["streams"] - base[t["uri"]], 0) for t in common)


def _new_playlists(now_pl: List[dict], before_pl: List[dict]) -> List[dict]:
    seen = {p["uri"] for p in before_pl}
    return [p for p in now_pl if p["uri"] not in seen]


def find_baseline(conn, artist_id, start_dt, current_ts):
    """Snapshot at/before period start; else earliest older snapshot; must be >= MIN_BASELINE_DAYS old."""
    snap = get_snapshot_at(conn, artist_id, start_dt, before=current_ts)
    if snap:
        return snap
    snap = get_earliest_snapshot(conn, artist_id, current_ts)
    if snap and (current_ts - snap[1]) >= timedelta(days=MIN_BASELINE_DAYS):
        return snap
    return None


def build_report(conn: sqlite3.Connection, current: Dict[str, Any], current_ts: datetime,
                 period_start: date, period_end: date,
                 manual: Optional[dict] = None, goals: Optional[dict] = None) -> Dict[str, Any]:
    """Builds the structured Spotify section: table rows (Current/Previous/Delta) + raw fields."""
    manual, goals = manual or {}, goals or {}
    artist_id = current["artist_id"]
    length = (period_end - period_start).days + 1
    start_dt = datetime.combine(period_start, datetime.min.time(), tzinfo=timezone.utc)
    prior_start_dt = start_dt - timedelta(days=length)

    found = find_baseline(conn, artist_id, start_dt, current_ts)
    base, base_ts = found if found else (None, None)
    found2 = get_snapshot_at(conn, artist_id, prior_start_dt, before=base_ts) if base_ts else None
    base2 = found2[0] if found2 else None

    partial = current.get("data_quality") != "full"
    base_partial = base is not None and base.get("data_quality") != "full"

    def val(snap, key, need_full=False):
        if snap is None or (need_full and snap.get("data_quality") != "full"):
            return None
        return snap.get(key)

    rows: List[Dict[str, Any]] = []

    def add(metric, cur, prev, source, note=""):
        rows.append({"metric": metric, "current": cur, "previous": prev,
                     "delta_pct": _pct(cur, prev), "source": source, "note": note})

    add("Monthly Listeners", current["spotify_monthly_listeners"],
        val(base, "spotify_monthly_listeners"), "public")
    add("Followers", None if partial else current["spotify_followers"],
        val(base, "spotify_followers", need_full=True), "public",
        "GraphQL data unavailable this run" if partial else "")

    # Total streams: manual (Spotify for Artists) beats proxy
    m = manual.get("total_streams_7d")
    if m:
        add("Total Streams (7d)", m.get("current"), m.get("previous"), "manual")
    else:
        cur_s = None if partial or base is None or base_partial else streams_gained(
            current["spotify_top_tracks"], base.get("spotify_top_tracks", []))
        prev_s = None if base is None or base2 is None else streams_gained(
            base.get("spotify_top_tracks", []), base2.get("spotify_top_tracks", []))
        add("Total Streams (7d)", cur_s, prev_s, "proxy" if cur_s is not None else "unavailable",
            "top-10-track stream gain between snapshots (lower bound); supply Spotify for Artists "
            "totals via manual.json for the true figure")

    # Playlist additions: manual beats proxy
    new_pl: List[dict] = []
    m = manual.get("playlist_additions")
    if m:
        add("Playlist Additions", m.get("current"), m.get("previous"), "manual")
    else:
        if base is not None and current.get("spotify_playlists"):
            new_pl = _new_playlists(current["spotify_playlists"], base.get("spotify_playlists", []))
        cur_p = len(new_pl) if base is not None and current.get("spotify_playlists") else None
        prev_p = (len(_new_playlists(base.get("spotify_playlists", []), base2.get("spotify_playlists", [])))
                  if base and base2 and base.get("spotify_playlists") else None)
        add("Playlist Additions", cur_p, prev_p, "proxy" if cur_p is not None else "unavailable",
            "new playlist appearances vs previous snapshot; Spotify only lists a subset of playlists publicly")

    m = manual.get("saves")
    if m:
        add("Saves", m.get("current"), m.get("previous"), "manual")
    else:
        add("Saves", None, None, "unavailable", "only shown in Spotify for Artists - add via manual.json")

    top_tracks = current.get("spotify_top_tracks", [])
    gained = None
    if top_tracks and base and base.get("spotify_top_tracks"):
        b = {t["uri"]: t["streams"] for t in base["spotify_top_tracks"]}
        if top_tracks[0]["uri"] in b:
            gained = max(top_tracks[0]["streams"] - b[top_tracks[0]["uri"]], 0)

    baseline_info = None
    if base_ts:
        note = ""
        if base_ts > start_dt + timedelta(days=1):
            note = "Baseline is later than the period start, so changes cover a shorter window."
        elif base_ts < start_dt - timedelta(days=3):
            note = "Baseline is older than the period start (no closer snapshot stored)."
        baseline_info = {"date": base_ts.date().isoformat(), "note": note}

    return {
        "artist_id": artist_id,
        "artist_name": current.get("artist_name"),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "report_date": datetime.now(timezone.utc).date().isoformat(),
        "rows": rows,
        "top_tracks": top_tracks,
        "top_track_gained": gained,
        "cities": current.get("spotify_top_cities", []),
        "cities_prev": (base or {}).get("spotify_top_cities") if base else None,
        "playlists": current.get("spotify_playlists", []),
        "new_playlists": new_pl,
        "lost_playlists_count": (len(_new_playlists(base.get("spotify_playlists", []), current.get("spotify_playlists", [])))
                                 if base and current.get("spotify_playlists") else 0),
        "latest_release": current.get("spotify_latest_release"),
        "world_rank": current.get("spotify_world_rank"),
        "goals": goals,
        "extra_notes": {"demographics": manual.get("demographics")} if manual.get("demographics") else {},
        "baseline": baseline_info,
        "data_quality": current.get("data_quality"),
    }


def _load_json(path: Optional[str]) -> dict:
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    ap = argparse.ArgumentParser(description="Spotify public metrics scraper + report builder")
    ap.add_argument("artist_id", nargs="?", default=TARGET_ARTIST_ID,
                    help="22-char Spotify artist ID (default: TARGET_ARTIST_ID)")
    ap.add_argument("--period-start", help="YYYY-MM-DD (default: period-end minus 6 days)")
    ap.add_argument("--period-end", help="YYYY-MM-DD (default: today, UTC)")
    ap.add_argument("--manual", help="JSON with Spotify-for-Artists-only values")
    ap.add_argument("--goals", help="JSON with targets")
    ap.add_argument("--force", action="store_true", help="ignore the 24h cache and re-scrape")
    ap.add_argument("--report-only", action="store_true", help="no scraping: rebuild the report from the DB")
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--out-dir", default="reports")
    args = ap.parse_args()

    artist_id = args.artist_id.strip()
    end = date.fromisoformat(args.period_end) if args.period_end else datetime.now(timezone.utc).date()
    start = date.fromisoformat(args.period_start) if args.period_start else end - timedelta(days=6)
    logger.info("Starting Spotify public metrics scraper for ID: %s (period %s to %s)", artist_id, start, end)

    conn = sqlite3.connect(args.db)
    init_db(conn)
    try:
        if args.report_only:
            end_dt = datetime.combine(end, datetime.max.time(), tzinfo=timezone.utc)
            snap = get_snapshot_at(conn, artist_id, end_dt)
            if not snap:
                logger.critical("No stored snapshot at or before %s for %s.", end, artist_id)
                sys.exit(2)
            current, current_ts = snap
        else:
            cached = None if args.force else get_fresh_cache(conn, artist_id)
            if cached:
                current, current_ts = cached
                logger.info("Using cached snapshot from %s (TTL 24h; --force to refresh).", current_ts.isoformat())
            else:
                current = scrape_spotify_artist(artist_id)
                current_ts = datetime.now(timezone.utc)
                if current["data_quality"] == "failed":
                    logger.critical("Scrape returned no usable data (token/hash/HTML changes?). Nothing saved.")
                    sys.exit(2)
                if current["data_quality"] == "partial":
                    logger.warning("Only monthly listeners could be read; followers/tracks/cities are missing this run.")
                # Build the report BEFORE saving so this run isn't its own baseline
                report = build_report(conn, current, current_ts, start, end, _load_json(args.manual), _load_json(args.goals))
                save_to_cache(conn, artist_id, current, extracted_at=current_ts)
                _emit(report, args, artist_id, end)
                return

        report = build_report(conn, current, current_ts, start, end, _load_json(args.manual), _load_json(args.goals))
        _emit(report, args, artist_id, end)
    finally:
        conn.close()


def _emit(report: Dict[str, Any], args, artist_id: str, end: date) -> None:
    import os
    text = commentary.build_commentary(report)
    md = commentary.render_markdown(report, text)
    os.makedirs(args.out_dir, exist_ok=True)
    stem = os.path.join(args.out_dir, f"spotify_{artist_id}_{end.isoformat()}")
    with open(stem + ".json", "w", encoding="utf-8") as fh:
        json.dump({"report": report, "commentary": text}, fh, indent=2, ensure_ascii=False)
    with open(stem + ".md", "w", encoding="utf-8") as fh:
        fh.write(md)
    print("\n--- Spotify section (also saved to %s.md / .json) ---\n" % stem)
    print(md)
    logger.info("Pipeline executed successfully.")


if __name__ == "__main__":
    main()
