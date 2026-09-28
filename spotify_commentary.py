"""
spotify_commentary.py - Self-updating quantitative commentary for the Spotify
section of the Artist Performance Report.

Pure functions only (no network, no database).  Feed it the `report` dict built
by spotify_scraper.build_report() and it returns ready-to-paste text for every
free-text field in the template:

    Executive Summary  -> rating tick-boxes, key highlight, "one thing to watch"
    Spotify table      -> one sentence per metric
    Top Track          -> "Track - streams (+gained this period)"
    Playlist Status    -> official playlists + new appearances
    Notes & Observations -> geography, release momentum, playlist strategy, demographics
    Cross-Platform     -> direction + signal for Spotify

HOW TO CUSTOMISE
    * Thresholds  -> edit the SETTINGS block below.
    * Wording     -> edit the TEMPLATES dict below ({placeholders} are filled
                     automatically from the data - keep the names unchanged).
Nothing else needs touching when you analyse a different artist or period.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

# =============================================================================
# SETTINGS  (edit freely)
# =============================================================================

# Overall rating from the weighted period-over-period change (%).
# First band whose threshold the composite meets wins.
RATING_BANDS = [
    ("Breakout", 20.0),
    ("Strong", 5.0),
    ("Steady", -2.0),
    ("Soft", -10.0),
    ("Declining", float("-inf")),
]
RATING_ORDER = ["Breakout", "Strong", "Steady", "Soft", "Declining"]

# Which metrics feed the rating and how much each counts.
RATING_WEIGHTS = {
    "Monthly Listeners": 0.40,
    "Followers": 0.20,
    "Total Streams (7d)": 0.40,
}
PCT_CLIP = (-50.0, 100.0)   # stop one tiny-base metric dominating the composite
MIN_BASE = 50               # below this "previous" value, flag % as small-base

# Words for a % change (first threshold the change is >= wins).
CHANGE_WORDS = [
    (30.0, "surged"),
    (15.0, "climbed strongly"),
    (5.0, "rose"),
    (1.0, "edged up"),
    (-1.0, "was flat"),
    (-5.0, "edged down"),
    (-15.0, "fell"),
    (-30.0, "dropped sharply"),
    (float("-inf"), "plunged"),
]

# Concentration cut-offs (%).
CITY_CONCENTRATED = 50.0       # top city share of top-city listeners -> "heavily concentrated"
CITY_MODERATE = 30.0
TRACK_DEPENDENCY = 60.0        # top track share of top-10 streams -> dependency warning
RELEASE_FRESH_DAYS = 14
RELEASE_RECENT_DAYS = 56

# =============================================================================
# TEMPLATES  (edit wording; keep {placeholders})
# =============================================================================
TEMPLATES = {
    # --- per-metric sentences ---
    "metric_change": "{metric} {verb} ({pct}): {cur} vs {prev} previously.{flag}",
    "metric_no_base": "{metric} stands at {cur}; there is no prior-period baseline yet, so no change can be calculated.",
    "metric_zero_base": "{metric} is {cur}; the previous period was 0, so a % change is not meaningful.",
    "metric_unavailable": "{metric}: not available from public data - {note}",
    # --- executive summary ---
    "highlight": "{metric} {verb} ({pct}), {prev} -> {cur} - the largest Spotify move of the period.",
    "highlight_playlist": "Added to official Spotify playlist(s): {names}.",
    "highlight_none": "No period-over-period comparison is possible yet - this run establishes the baseline snapshot.",
    "watch_negative": "Watch {metric}: it {verb} ({pct}) and is the weakest Spotify metric this period.",
    "watch_goal": "Goal gap: {metric} is {gap} below target ({cur} vs {goal}).",
    "watch_track": "Streams are concentrated - '{track}' is {share}% of ranked top-track streams; one-track dependency.",
    "watch_city": "Listeners are heavily concentrated in {city} ({share}% of top-city listeners).",
    "watch_clear": "No adverse Spotify signals; the watch item is whether this momentum holds next period.",
    "goal_met": "Goal met: {metric} {cur} vs target {goal} ({over} over).",
    "goal_missed": "Goal not met: {metric} {cur} vs target {goal} ({gap} short).",
    # --- top track / playlists ---
    "top_track": "{name} - {streams} streams{gain}",
    "top_track_gain": " ({gained} added this period)",
    "top_track_none": "Top-track data was not returned by Spotify this run.",
    "playlist_official": "Official Spotify playlists featuring the artist: {names}.",
    "playlist_official_none": "No official Spotify playlists detected among {total} playlist appearance(s) returned.",
    "playlist_none_at_all": "Spotify returned no playlist appearances for this artist this run.",
    # --- notes ---
    "geo": ("Listeners are {label}: {city} ({country}) leads with {listeners} listeners "
            "({share}% of the top-{n} city total); the top 3 cities account for {top3}%. "
            "{n_countries} countr{ies} appear in the list.{moves}"),
    "geo_moves_in": " New in the top {n}: {cities}.",
    "geo_moves_out": " Dropped out: {cities}.",
    "geo_none": "City-level listener data was not returned by Spotify this run.",
    "release_fresh": "Release momentum: '{name}' ({rtype}) dropped {days} day(s) ago - the artist is inside the release window.{conc}",
    "release_recent": "Release momentum: latest release '{name}' ({rtype}) is {days} days old - post-release tail.{conc}",
    "release_old": "Release momentum: no new release in {days} days (latest: '{name}', {rtype}); streams are catalogue-driven.{conc}",
    "release_none": "Release momentum: latest-release data was not returned by Spotify this run.{conc}",
    "release_conc": " The top track holds {share}% of streams across the {n} ranked top tracks.",
    "playlist_strategy": ("Playlist footprint: {total} playlist appearance(s) returned, {official} official/Spotify-curated, "
                          "{other} listener-curated.{new}"),
    "playlist_new": " {n} new since last snapshot: {names}.",
    "playlist_lost": " {n} no longer listed.",
    "demographics": ("Demographics: age/gender splits are not exposed publicly - "
                     "add them from Spotify for Artists via the manual-inputs file."),
    # --- cross-platform ---
    "xp_gaining": "Spotify - gaining: {signal}",
    "xp_flat": "Spotify - flat: {signal}",
    "xp_declining": "Spotify - declining: {signal}",
    "xp_unknown": "Spotify - no comparison yet (baseline snapshot only).",
}

# =============================================================================
# Formatting helpers
# =============================================================================


def fmt_num(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{int(round(v)):,}"


def fmt_pct(p: Optional[float], decimals: int = 1) -> str:
    return "n/a" if p is None else f"{p:+.{decimals}f}%"


def change_verb(pct: float) -> str:
    for threshold, word in CHANGE_WORDS:
        if pct >= threshold:
            return word
    return CHANGE_WORDS[-1][1]


def _row(report: Dict[str, Any], metric: str) -> Optional[Dict[str, Any]]:
    for r in report.get("rows", []):
        if r["metric"] == metric:
            return r
    return None


def _usable(r: Dict[str, Any]) -> bool:
    """Row has a % change based on a non-trivial base."""
    return r.get("delta_pct") is not None and (r.get("previous") or 0) >= MIN_BASE


# =============================================================================
# Building blocks
# =============================================================================


def metric_sentence(r: Dict[str, Any]) -> str:
    cur, prev, pct = r.get("current"), r.get("previous"), r.get("delta_pct")
    if cur is None:
        return TEMPLATES["metric_unavailable"].format(metric=r["metric"], note=r.get("note") or "see manual inputs")
    if prev is None:
        return TEMPLATES["metric_no_base"].format(metric=r["metric"], cur=fmt_num(cur))
    if pct is None:
        return TEMPLATES["metric_zero_base"].format(metric=r["metric"], cur=fmt_num(cur))
    flag = ""
    if prev < MIN_BASE:
        flag += " Small base - treat the % with caution."
    if r.get("source") == "proxy":
        flag += " (proxy measure)"
    return TEMPLATES["metric_change"].format(
        metric=r["metric"], verb=change_verb(pct), pct=fmt_pct(pct),
        cur=fmt_num(cur), prev=fmt_num(prev), flag=flag)


def classify_rating(report: Dict[str, Any]) -> Dict[str, Any]:
    """Weighted, clipped composite of the RATING_WEIGHTS metrics -> Breakout..Declining."""
    total_w, acc, used = 0.0, 0.0, []
    for metric, w in RATING_WEIGHTS.items():
        r = _row(report, metric)
        if r and r.get("delta_pct") is not None:
            pct = max(PCT_CLIP[0], min(PCT_CLIP[1], r["delta_pct"]))
            acc += w * pct
            total_w += w
            used.append(metric)
    if total_w == 0:
        return {"rating": None, "composite": None, "used": [], "ticks": _ticks(None)}
    composite = acc / total_w
    rating = next(name for name, thr in RATING_BANDS if composite >= thr)
    return {"rating": rating, "composite": composite, "used": used, "ticks": _ticks(rating)}


def _ticks(rating: Optional[str]) -> str:
    return "  ".join(("\u2611 " if n == rating else "\u2610 ") + n for n in RATING_ORDER)


def goal_lines(report: Dict[str, Any]) -> List[str]:
    key_map = {"monthly_listeners": "Monthly Listeners", "followers": "Followers",
               "streams_7d": "Total Streams (7d)", "playlist_additions": "Playlist Additions",
               "saves": "Saves"}
    out = []
    for key, goal in (report.get("goals") or {}).items():
        r = _row(report, key_map.get(key, key))
        if not r or r.get("current") is None or goal in (None, 0):
            continue
        cur = r["current"]
        if cur >= goal:
            out.append(TEMPLATES["goal_met"].format(metric=r["metric"], cur=fmt_num(cur),
                                                    goal=fmt_num(goal), over=fmt_pct((cur - goal) / goal * 100)))
        else:
            out.append(TEMPLATES["goal_missed"].format(metric=r["metric"], cur=fmt_num(cur),
                                                       goal=fmt_num(goal), gap=fmt_pct((goal - cur) / goal * 100).lstrip("+")))
    return out


def _top_share(report: Dict[str, Any]) -> Optional[float]:
    tracks = report.get("top_tracks") or []
    total = sum(t.get("streams") or 0 for t in tracks[:10])
    if not tracks or total <= 0:
        return None
    return (tracks[0].get("streams") or 0) / total * 100


def _city_stats(cities: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    cities = [c for c in cities if c.get("listeners")]
    total = sum(c["listeners"] for c in cities)
    if not cities or total <= 0:
        return None
    ranked = sorted(cities, key=lambda c: c["listeners"], reverse=True)
    return {
        "ranked": ranked, "total": total,
        "top_share": ranked[0]["listeners"] / total * 100,
        "top3_share": sum(c["listeners"] for c in ranked[:3]) / total * 100,
    }


def highlight(report: Dict[str, Any]) -> str:
    new_off = [p["name"] for p in report.get("new_playlists", []) if p.get("official")]
    if new_off:
        return TEMPLATES["highlight_playlist"].format(names=", ".join(new_off[:3]))
    cands = [r for r in report["rows"] if _usable(r)]
    if not cands:
        return TEMPLATES["highlight_none"]
    r = max(cands, key=lambda x: abs(x["delta_pct"]))
    return TEMPLATES["highlight"].format(metric=r["metric"], verb=change_verb(r["delta_pct"]),
                                         pct=fmt_pct(r["delta_pct"]), prev=fmt_num(r["previous"]),
                                         cur=fmt_num(r["current"]))


def watch_item(report: Dict[str, Any]) -> str:
    neg = [r for r in report["rows"] if _usable(r) and r["delta_pct"] <= -5]
    if neg:
        r = min(neg, key=lambda x: x["delta_pct"])
        return TEMPLATES["watch_negative"].format(metric=r["metric"], verb=change_verb(r["delta_pct"]),
                                                  pct=fmt_pct(r["delta_pct"]))
    goal_names = {"monthly_listeners": "Monthly Listeners", "followers": "Followers",
                  "streams_7d": "Total Streams (7d)"}
    for key, goal in (report.get("goals") or {}).items():
        r = _row(report, goal_names[key]) if key in goal_names else None
        if r and r.get("current") is not None and goal and r["current"] < goal:
            return TEMPLATES["watch_goal"].format(
                metric=r["metric"], gap=f"{(goal - r['current']) / goal * 100:.1f}%",
                cur=fmt_num(r["current"]), goal=fmt_num(goal))
    share = _top_share(report)
    if share is not None and share >= TRACK_DEPENDENCY:
        return TEMPLATES["watch_track"].format(track=report["top_tracks"][0]["name"], share=f"{share:.0f}")
    cs = _city_stats(report.get("cities") or [])
    if cs and cs["top_share"] >= CITY_CONCENTRATED:
        return TEMPLATES["watch_city"].format(city=cs["ranked"][0]["city"], share=f"{cs['top_share']:.0f}")
    return TEMPLATES["watch_clear"]


def top_track_line(report: Dict[str, Any]) -> str:
    tracks = report.get("top_tracks") or []
    if not tracks:
        return TEMPLATES["top_track_none"]
    t = tracks[0]
    gained = report.get("top_track_gained")
    gain = TEMPLATES["top_track_gain"].format(gained=fmt_num(gained)) if gained else ""
    return TEMPLATES["top_track"].format(name=t["name"], streams=fmt_num(t["streams"]), gain=gain)


def playlist_status(report: Dict[str, Any]) -> str:
    pls = report.get("playlists") or []
    if not pls:
        return TEMPLATES["playlist_none_at_all"]
    official = [p["name"] for p in pls if p.get("official")]
    if official:
        return TEMPLATES["playlist_official"].format(names=", ".join(official))
    return TEMPLATES["playlist_official_none"].format(total=len(pls))


def geography_note(report: Dict[str, Any]) -> str:
    cs = _city_stats(report.get("cities") or [])
    if not cs:
        return TEMPLATES["geo_none"]
    label = ("heavily concentrated" if cs["top_share"] >= CITY_CONCENTRATED
             else "moderately concentrated" if cs["top_share"] >= CITY_MODERATE
             else "geographically spread")
    top = cs["ranked"][0]
    countries = {c.get("country") for c in cs["ranked"] if c.get("country")}
    moves = ""
    prev = _city_stats(report.get("cities_prev") or [])
    if prev:
        now_set = {(c["city"], c.get("country")) for c in cs["ranked"]}
        old_set = {(c["city"], c.get("country")) for c in prev["ranked"]}
        entered = sorted(c for c, _ in now_set - old_set)
        left = sorted(c for c, _ in old_set - now_set)
        if entered:
            moves += TEMPLATES["geo_moves_in"].format(n=len(cs["ranked"]), cities=", ".join(entered))
        if left:
            moves += TEMPLATES["geo_moves_out"].format(cities=", ".join(left))
    return TEMPLATES["geo"].format(
        label=label, city=top["city"], country=top.get("country") or "n/a",
        listeners=fmt_num(top["listeners"]), share=f"{cs['top_share']:.0f}", n=len(cs["ranked"]),
        top3=f"{cs['top3_share']:.0f}", n_countries=len(countries), ies="y" if len(countries) == 1 else "ies",
        moves=moves)


def release_note(report: Dict[str, Any]) -> str:
    share = _top_share(report)
    n_tr = len(report.get("top_tracks") or [])
    conc = TEMPLATES["release_conc"].format(share=f"{share:.0f}", n=n_tr) if share is not None else ""
    rel = report.get("latest_release")
    if not rel or not rel.get("date"):
        return TEMPLATES["release_none"].format(conc=conc)
    try:
        rdate = date.fromisoformat(rel["date"][:10])
        end = date.fromisoformat(report["period_end"])
    except (ValueError, KeyError):
        return TEMPLATES["release_none"].format(conc=conc)
    days = (end - rdate).days
    key = ("release_fresh" if 0 <= days <= RELEASE_FRESH_DAYS
           else "release_recent" if days <= RELEASE_RECENT_DAYS else "release_old")
    return TEMPLATES[key].format(name=rel["name"], rtype=(rel.get("type") or "release").lower(),
                                 days=max(days, 0), conc=conc)


def playlist_strategy_note(report: Dict[str, Any]) -> str:
    pls = report.get("playlists") or []
    if not pls:
        return TEMPLATES["playlist_none_at_all"]
    official = sum(1 for p in pls if p.get("official"))
    new = report.get("new_playlists") or []
    lost = report.get("lost_playlists_count") or 0
    tail = ""
    if new:
        tail += TEMPLATES["playlist_new"].format(n=len(new), names=", ".join(p["name"] for p in new[:3]))
    if lost:
        tail += TEMPLATES["playlist_lost"].format(n=lost)
    return TEMPLATES["playlist_strategy"].format(total=len(pls), official=official,
                                                 other=len(pls) - official, new=tail)


def cross_platform(report: Dict[str, Any], rating: Dict[str, Any]) -> Dict[str, str]:
    if rating["rating"] is None:
        return {"platform": "Spotify", "direction": "unknown", "text": TEMPLATES["xp_unknown"]}
    direction = ("gaining" if rating["rating"] in ("Breakout", "Strong")
                 else "flat" if rating["rating"] == "Steady" else "declining")
    cands = [r for r in report["rows"] if r.get("delta_pct") is not None]
    if direction == "declining":
        r = min(cands, key=lambda x: x["delta_pct"])
    else:
        r = max(cands, key=lambda x: x["delta_pct"])
    signal = f"{r['metric']} {fmt_pct(r['delta_pct'])} ({fmt_num(r['previous'])} -> {fmt_num(r['current'])})"
    return {"platform": "Spotify", "direction": direction,
            "text": TEMPLATES[f"xp_{direction}"].format(signal=signal)}


# =============================================================================
# Public entry points
# =============================================================================


def build_commentary(report: Dict[str, Any]) -> Dict[str, Any]:
    """Return every free-text field for the Spotify section + executive summary."""
    rating = classify_rating(report)
    notes = [geography_note(report), release_note(report), playlist_strategy_note(report)]
    demo = (report.get("extra_notes") or {}).get("demographics")
    notes.append(f"Demographics: {demo}" if demo else TEMPLATES["demographics"])
    return {
        "rating": rating["rating"],
        "rating_composite": rating["composite"],
        "rating_ticks": rating["ticks"],
        "goal_lines": goal_lines(report),
        "highlight": highlight(report),
        "watch": watch_item(report),
        "metric_sentences": [metric_sentence(r) for r in report["rows"]],
        "top_track": top_track_line(report),
        "playlist_status": playlist_status(report),
        "notes": notes,
        "cross_platform": cross_platform(report, rating),
    }


def render_markdown(report: Dict[str, Any], c: Dict[str, Any]) -> str:
    """Markdown block laid out in the same order as the template's Spotify section."""
    L: List[str] = []
    L.append(f"# {report.get('artist_name') or report['artist_id']} - Spotify "
             f"({report['period_start']} to {report['period_end']})\n")
    L.append("## Executive summary inputs")
    rating_txt = c["rating"] or "Insufficient data"
    comp = f" (weighted change {fmt_pct(c['rating_composite'])})" if c["rating_composite"] is not None else ""
    L.append(f"**Overall Performance Rating:** {c['rating_ticks']}  -> **{rating_txt}**{comp}")
    for g in c["goal_lines"]:
        L.append(f"- {g}")
    L.append(f"- **Most important Spotify move:** {c['highlight']}")
    L.append(f"- **One thing to watch:** {c['watch']}\n")

    L.append("## SPOTIFY PERFORMANCE")
    L.append("| Metric | Current | Previous | \u0394 (%) | Source |")
    L.append("|---|---:|---:|---:|---|")
    for r in report["rows"]:
        L.append(f"| {r['metric']} | {fmt_num(r['current'])} | {fmt_num(r['previous'])} | "
                 f"{fmt_pct(r['delta_pct'])} | {r['source']} |")
    L.append("")
    for s in c["metric_sentences"]:
        L.append(f"- {s}")
    L.append("")
    L.append(f"**Top Track:** {c['top_track']}\n")
    L.append(f"**Playlist Status:** {c['playlist_status']}\n")
    L.append("**Spotify Notes & Observations:**")
    for n in c["notes"]:
        L.append(f"- {n}")
    L.append("")
    L.append(f"**Cross-platform signal:** {c['cross_platform']['text']}\n")
    b = report.get("baseline")
    if b:
        L.append(f"_Baseline snapshot: {b['date']}. {b.get('note') or ''}_".replace(". _", "._"))
    else:
        L.append("_No baseline snapshot yet - Previous / \u0394 columns fill in once a snapshot "
                 "at least 1 day older exists. Run the scraper daily (cron / Task Scheduler)._")
    L.append("_Source key: public = read directly from Spotify's public web player; proxy = derived "
             "from snapshot differences (lower bound); manual = supplied from Spotify for Artists; "
             "unavailable = not publicly exposed._")
    return "\n".join(L) + "\n"
