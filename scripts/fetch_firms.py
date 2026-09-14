#!/usr/bin/env python3
"""
FireWatch ETL — NASA FIRMS active-fire detections → analysis-ready GeoJSON.

Runs in GitHub Actions on a schedule. Fetches one CSV per (day, sensor) from the
FIRMS Area API, normalises the differing MODIS/VIIRS schemas into a single record
shape, clusters detections into discrete fire complexes with DBSCAN, and writes
static files that GitHub Pages serves same-origin.

Why this exists: the browser cannot call FIRMS directly without either exposing
the MAP_KEY or routing through a third-party CORS proxy. Doing the fetch here
removes both problems and makes the published page depend on nothing at runtime.

Standard library only — no pip install step to fail in CI.

Outputs (all under data/):
    days/YYYY-MM-DD.geojson   detections for one UTC day; written once, then frozen
    fires.geojson             clustered fire complexes as footprint polygons
    meta.json                 run metadata, date range, per-sensor counts
    daily_summary.json        append-only time series — the archive FIRMS doesn't keep
"""

import csv
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Configuration (all overridable from the workflow environment)
# ---------------------------------------------------------------------------

MAP_KEY = os.environ.get("FIRMS_MAP_KEY", "").strip()

# Area API takes an arbitrary bbox: west,south,east,north.
# This is the main advantage over the fixed regional CSVs the old build used —
# the study area is now a config value, not a filename.
AREA = os.environ.get("FIRMS_AREA", "112,-45,155,-9").strip()          # Australia
REGION_NAME = os.environ.get("FIRMS_REGION_NAME", "Australia").strip()

WINDOW_DAYS = int(os.environ.get("FIRMS_DAYS", "7"))    # rolling window to publish
RETAIN_DAYS = int(os.environ.get("FIRMS_RETAIN", "30")) # day files kept on disk
REFRESH_DAYS = 2                                        # re-fetch the last N days (NRT backfills)

SOURCES = [
    ("VIIRS_NOAA21_NRT", "N21"),
    ("VIIRS_NOAA20_NRT", "N20"),
    ("VIIRS_SNPP_NRT",   "SNPP"),
    ("MODIS_NRT",        "MODIS"),
]

BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
OUT_DIR = os.environ.get("FIRMS_OUT", "data")
DAYS_DIR = os.path.join(OUT_DIR, "days")

# DBSCAN parameters.
# eps 1.5 km: VIIRS pixels are ~375 m, so this links detections up to about four
# pixels apart — the scale of a contiguous fire front — without merging separate
# fires in the same valley. min_pts 3 suppresses isolated false positives
# (gas flares, hot roofs, sun glint) which are common single-pixel artefacts.
EPS_KM = float(os.environ.get("FIRMS_EPS_KM", "1.5"))
MIN_PTS = int(os.environ.get("FIRMS_MIN_PTS", "3"))

# --- Fire Radiative Energy integration -------------------------------------
# FRP is an instantaneous rate (MW). Energy is its integral over time (MJ).
# FRE is estimated per cluster by trapezoidal integration of the cluster's
# FRP time series across satellite overpasses.
#
# Two guards keep the estimate honest:
#   MAX_GAP_H  — beyond this, a fire went unobserved (usually cloud). Linearly
#                interpolating across a 20-hour hole invents burning nobody saw,
#                so the gap is not bridged; each side gets a bounded tail instead.
#   EDGE_DT    — the first and last observation of a burst have no neighbour on
#                one side, so each is credited with half the region's median
#                overpass interval. Derived from the data, not assumed.
OVERPASS_BIN_S = 900          # detections within 15 min are one overpass
MAX_GAP_H = float(os.environ.get("FIRMS_MAX_GAP_H", "12"))
EDGE_DT_MIN_H = 0.5
EDGE_DT_MAX_H = 3.0

KM_PER_DEG_LAT = 110.574
KM_PER_DEG_LON_EQ = 111.320

HTTP_TIMEOUT = 60
HTTP_RETRIES = 3


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def http_get(url):
    """GET with retries. Returns text, or None on permanent failure."""
    last = None
    for attempt in range(HTTP_RETRIES):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "firewatch-etl/1.0 (+github-actions)"}
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (400, 401, 403, 404):
                break                      # not transient — a bad key or bad bbox
        except Exception as e:              # noqa: BLE001 - network is best-effort
            last = str(e)
        time.sleep(2 * (attempt + 1))
    print(f"    ! request failed ({last})", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Parsing and schema normalisation
# ---------------------------------------------------------------------------

def norm_confidence(raw, is_viirs):
    """
    MODIS reports confidence as an integer 0-100; VIIRS as 'l' / 'n' / 'h'.
    The old build printed whichever it got with a '%' suffix, which would have
    rendered 'h%' the moment VIIRS was added. Collapse both to an ordinal.
        0 = low, 1 = nominal, 2 = high
    """
    if raw is None:
        return 1
    s = str(raw).strip().lower()
    if is_viirs:
        return {"l": 0, "n": 1, "h": 2}.get(s, 1)
    try:
        v = float(s)
    except ValueError:
        return 1
    if v < 30:
        return 0
    if v <= 80:
        return 1
    return 2


def to_float(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def parse_csv(text, sensor_code):
    """FIRMS CSV → normalised records. Returns [] for error bodies."""
    if not text or not text.strip():
        return []

    head = text.lstrip()[:400].lower()
    if "latitude" not in head:
        # FIRMS returns a plain-text message for a bad key / quota / bad bbox.
        print(f"    ! unexpected response body: {text.strip()[:160]}", file=sys.stderr)
        return []

    is_viirs = sensor_code != "MODIS"
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        lat = to_float(r.get("latitude"))
        lon = to_float(r.get("longitude"))
        if lat is None or lon is None:
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue

        # VIIRS carries bright_ti4 (I-4, 3.74 um); MODIS carries brightness (band 21/22).
        bright = to_float(r.get("bright_ti4") if is_viirs else r.get("brightness"))

        acq_time = str(r.get("acq_time") or "0").strip().zfill(4)
        rows.append({
            "lat": round(lat, 4),          # ~11 m — far finer than a 375 m pixel
            "lon": round(lon, 4),
            "date": (r.get("acq_date") or "").strip(),
            "time": acq_time,
            "frp": to_float(r.get("frp")) or 0.0,
            "bright": bright or 0.0,
            "conf": norm_confidence(r.get("confidence"), is_viirs),
            "sensor": sensor_code,
            "night": 1 if str(r.get("daynight") or "D").strip().upper() == "N" else 0,
        })
    return rows


def fetch_day(day_iso):
    """All sensors for one UTC day, deduplicated."""
    out = []
    for source, code in SOURCES:
        url = f"{BASE}/{MAP_KEY}/{source}/{AREA}/1/{day_iso}"
        text = http_get(url)
        rows = parse_csv(text, code)
        print(f"    {source:<18} {len(rows):>6} detections")
        out.extend(rows)
        time.sleep(1.0)                    # courtesy pacing; limit is 5000/10 min

    # Overlapping swaths can report the same pixel twice within a sensor.
    seen, deduped = set(), []
    for r in out:
        key = (r["lat"], r["lon"], r["time"], r["sensor"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def km_per_deg_lon(lat):
    return KM_PER_DEG_LON_EQ * math.cos(math.radians(lat))


def dist_km(a, b):
    """Equirectangular approximation — exact enough at the 1-2 km scale of eps."""
    mlat = math.radians((a["lat"] + b["lat"]) * 0.5)
    dx = (a["lon"] - b["lon"]) * KM_PER_DEG_LON_EQ * math.cos(mlat)
    dy = (a["lat"] - b["lat"]) * KM_PER_DEG_LAT
    return math.hypot(dx, dy)


def convex_hull(points):
    """Andrew's monotone chain. Returns hull as [(lon, lat), ...] counter-clockwise."""
    pts = sorted(set((p["lon"], p["lat"]) for p in points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def polygon_area_km2(ring, lat0):
    """Shoelace in a local equirectangular projection centred on lat0."""
    if len(ring) < 3:
        return 0.0
    kx = km_per_deg_lon(lat0)
    s = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i][0] * kx, ring[i][1] * KM_PER_DEG_LAT
        x2, y2 = ring[(i + 1) % len(ring)][0] * kx, ring[(i + 1) % len(ring)][1] * KM_PER_DEG_LAT
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def circle_ring(lon, lat, radius_km, n=18):
    """Fallback footprint for clusters whose hull is degenerate (collinear pixels)."""
    kx = km_per_deg_lon(lat) or 1e-6
    ring = []
    for i in range(n):
        t = 2 * math.pi * i / n
        ring.append([
            round(lon + (radius_km / kx) * math.cos(t), 5),
            round(lat + (radius_km / KM_PER_DEG_LAT) * math.sin(t), 5),
        ])
    ring.append(ring[0])
    return ring


# ---------------------------------------------------------------------------
# DBSCAN (grid-indexed, no dependencies)
# ---------------------------------------------------------------------------

def dbscan(points, eps_km, min_pts):
    """
    Density-based clustering over all detections in the window.

    A uniform grid indexes candidates so neighbour queries touch 9 cells rather
    than the whole dataset. Cell size is chosen so that a cell is at least eps
    wide in kilometres at every latitude in the study area — using the smallest
    cos(lat) makes cells larger than necessary near the equator, which costs a
    few extra candidate comparisons but keeps the 3x3 search provably complete.

    Returns a label per point: -1 for noise, otherwise a cluster index.
    """
    n = len(points)
    labels = [-1] * n
    if n == 0:
        return labels

    max_abs_lat = max(abs(p["lat"]) for p in points)
    cos_min = max(math.cos(math.radians(min(max_abs_lat, 85.0))), 0.05)
    cell_lat = eps_km / KM_PER_DEG_LAT
    cell_lon = eps_km / (KM_PER_DEG_LON_EQ * cos_min)

    grid = {}
    for i, p in enumerate(points):
        key = (int(math.floor(p["lat"] / cell_lat)), int(math.floor(p["lon"] / cell_lon)))
        grid.setdefault(key, []).append(i)

    def neighbours(i):
        p = points[i]
        gy = int(math.floor(p["lat"] / cell_lat))
        gx = int(math.floor(p["lon"] / cell_lon))
        out = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for j in grid.get((gy + dy, gx + dx), ()):
                    if dist_km(p, points[j]) <= eps_km:
                        out.append(j)
        return out

    cluster_id = 0
    visited = bytearray(n)

    for i in range(n):
        if visited[i]:
            continue
        visited[i] = 1
        seeds = neighbours(i)
        if len(seeds) < min_pts:
            continue                        # noise for now; may be absorbed as a border point

        labels[i] = cluster_id
        queue = [j for j in seeds if j != i]
        qi = 0
        while qi < len(queue):
            j = queue[qi]
            qi += 1
            if not visited[j]:
                visited[j] = 1
                jn = neighbours(j)
                if len(jn) >= min_pts:      # j is a core point — extend the cluster
                    queue.extend(k for k in jn if labels[k] == -1)
            if labels[j] == -1:
                labels[j] = cluster_id
        cluster_id += 1

    return labels


# ---------------------------------------------------------------------------
# Fire Radiative Energy
# ---------------------------------------------------------------------------

def obs_epoch(date_str, hhmm):
    """FIRMS acq_date + acq_time (HHMM, UTC) -> epoch seconds."""
    try:
        dt = datetime.strptime(f"{date_str}{str(hhmm).zfill(4)}", "%Y-%m-%d%H%M")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def overpass_series(pts):
    """
    Collapse a cluster's detections into one FRP value per satellite overpass.

    A cluster is small enough (~km) to be imaged whole in a single pass, which is
    why FRE is integrated per cluster rather than region-wide: at any instant only
    part of the continent is under a swath, so a region-wide FRP(t) series would
    undercount at every point and the integral would compound the error.

    Returns [(epoch_seconds, summed_frp_mw), ...] sorted in time.
    """
    stamped = []
    for p in pts:
        e = obs_epoch(p["date"], p["time"])
        if e is not None:
            stamped.append((e, p["frp"]))
    if not stamped:
        return []
    stamped.sort()

    series = []
    bin_start, bin_frp, bin_t = stamped[0][0], 0.0, []
    for e, frp in stamped:
        if e - bin_start > OVERPASS_BIN_S:
            series.append((sum(bin_t) / len(bin_t), bin_frp))
            bin_start, bin_frp, bin_t = e, 0.0, []
        bin_frp += frp
        bin_t.append(e)
    if bin_t:
        series.append((sum(bin_t) / len(bin_t), bin_frp))
    return series


def series_gaps(series):
    return [b[0] - a[0] for a, b in zip(series, series[1:]) if b[0] > a[0]]


def median(xs):
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def integrate_fre(series, edge_dt_s):
    """
    Trapezoidal integration of FRP over time.

    Returns (total_mj, {date: mj}). Energy from each trapezoid is attributed to
    the UTC day containing its midpoint, so daily totals sum to the total.
    MW x s = MJ.
    """
    if not series:
        return 0.0, {}

    max_gap_s = MAX_GAP_H * 3600.0
    total = 0.0
    per_day = {}

    def credit(epoch, mj):
        nonlocal total
        total += mj
        d = datetime.fromtimestamp(epoch, timezone.utc).date().isoformat()
        per_day[d] = per_day.get(d, 0.0) + mj

    # Interior
    for (t0, f0), (t1, f1) in zip(series, series[1:]):
        dt = t1 - t0
        if dt <= 0:
            continue
        if dt > max_gap_s:
            # Unobserved gap — credit each side a bounded tail instead of
            # interpolating across burning that was never measured.
            credit(t0, f0 * edge_dt_s)
            credit(t1, f1 * edge_dt_s)
        else:
            credit(t0 + dt * 0.5, 0.5 * (f0 + f1) * dt)

    # Leading and trailing observations have no neighbour on one side.
    credit(series[0][0], series[0][1] * edge_dt_s)
    credit(series[-1][0], series[-1][1] * edge_dt_s)

    return total, per_day


# ---------------------------------------------------------------------------
# Cluster summarisation
# ---------------------------------------------------------------------------

def summarise_clusters(points, labels, window_dates):
    n_clusters = (max(labels) + 1) if labels else 0
    buckets = [[] for _ in range(n_clusters)]
    for p, lab in zip(points, labels):
        if lab >= 0:
            buckets[lab].append(p)

    latest = window_dates[-1] if window_dates else None
    prev = window_dates[-2] if len(window_dates) > 1 else None

    # Pass 1: build each cluster's overpass series and measure the actual
    # observation cadence, so the edge interval is derived from this region and
    # period rather than assumed from the nominal number of satellites.
    series_by_cluster = {}
    all_gaps = []
    for idx, pts in enumerate(buckets):
        if not pts:
            continue
        s = overpass_series(pts)
        series_by_cluster[idx] = s
        all_gaps.extend(g for g in series_gaps(s) if g <= MAX_GAP_H * 3600.0)

    median_gap_s = median(all_gaps)
    edge_dt_s = median_gap_s * 0.5 if median_gap_s else EDGE_DT_MAX_H * 3600.0
    edge_dt_s = max(EDGE_DT_MIN_H * 3600.0, min(EDGE_DT_MAX_H * 3600.0, edge_dt_s))

    diagnostics = {
        "median_overpass_interval_h": round(median_gap_s / 3600.0, 2),
        "edge_dt_h": round(edge_dt_s / 3600.0, 2),
        "max_gap_h": MAX_GAP_H,
        "overpass_intervals_sampled": len(all_gaps),
    }

    # Pass 2: summarise.
    features = []
    for idx, pts in enumerate(buckets):
        if not pts:
            continue

        frps = [p["frp"] for p in pts]
        lats = [p["lat"] for p in pts]
        lons = [p["lon"] for p in pts]
        clat = sum(lats) / len(lats)
        clon = sum(lons) / len(lons)

        by_day = {}
        for p in pts:
            d = by_day.setdefault(p["date"], {"n": 0, "frp": 0.0})
            d["n"] += 1
            d["frp"] += p["frp"]
        for d in by_day.values():
            d["frp"] = round(d["frp"], 1)

        days_seen = sorted(by_day)
        first_seen, last_seen = days_seen[0], days_seen[-1]

        hull = convex_hull(pts)
        area = polygon_area_km2(hull, clat) if len(hull) >= 3 else 0.0
        if area < 0.25:
            # Collinear or near-coincident pixels: substitute a footprint circle
            # scaled by detection count so the polygon layer stays uniform.
            ring = circle_ring(clon, clat, max(0.35, 0.25 * math.sqrt(len(pts))))
            area = polygon_area_km2([(x, y) for x, y in ring[:-1]], clat)
        else:
            ring = [[round(x, 5), round(y, 5)] for x, y in hull]
            ring.append(ring[0])

        # Growth: latest day's detections against the mean of the preceding days
        # this cluster was active. Ratio, not difference, so small and large
        # fires are comparable.
        today_n = by_day.get(latest, {}).get("n", 0)
        prior = [by_day[d]["n"] for d in days_seen if d != latest]
        prior_mean = (sum(prior) / len(prior)) if prior else 0.0
        if prior_mean <= 0:
            trend = 1 if today_n > 0 else 0          # brand new
            ratio = None
        else:
            ratio = today_n / prior_mean
            trend = 1 if ratio >= 1.25 else (-1 if ratio <= 0.75 else 0)

        if today_n > 0:
            status = "active"
        elif prev and by_day.get(prev, {}).get("n", 0) > 0:
            status = "receding"
        else:
            status = "historic"

        # Fire Radiative Energy — the physically meaningful cumulative quantity.
        fre_mj, fre_by_day_mj = integrate_fre(series_by_cluster.get(idx, []), edge_dt_s)
        fre_gj = fre_mj / 1000.0
        fre_daily = {d: round(v / 1000.0, 1) for d, v in sorted(fre_by_day_mj.items())}

        features.append({
            "type": "Feature",
            "id": idx,
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {
                "fid": idx,
                "n": len(pts),
                "fre_gj": round(fre_gj, 1),
                "fre_daily": fre_daily,
                "overpasses": len(series_by_cluster.get(idx, [])),
                "frp_total": round(sum(frps), 1),
                "frp_max": round(max(frps), 1),
                "frp_mean": round(sum(frps) / len(frps), 2),
                "area_km2": round(area, 2),
                "lat": round(clat, 4),
                "lon": round(clon, 4),
                "first_seen": first_seen,
                "last_seen": last_seen,
                "duration_days": len(days_seen),
                "status": status,
                "trend": trend,
                "growth_ratio": round(ratio, 2) if ratio is not None else None,
                "night_frac": round(sum(p["night"] for p in pts) / len(pts), 2),
                "daily": by_day,
            },
        })

    # Rank by energy released, not by summed power — FRE is the defensible
    # ordering of "biggest fire", and it demotes complexes that merely happened
    # to be imaged more often.
    features.sort(key=lambda f: -f["properties"]["fre_gj"])
    return features, diagnostics


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    return os.path.getsize(path)


def day_path(day_iso):
    return os.path.join(DAYS_DIR, f"{day_iso}.geojson")


def write_day(day_iso, rows):
    """
    One file per UTC day. Past days are written once and never rewritten, so git
    stores each day's blob a single time instead of re-committing a monolithic
    file every run. Only the current and previous day churn.
    """
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "properties": {
                    "d": r["date"],
                    "t": r["time"],
                    "frp": round(r["frp"], 1),
                    "b": round(r["bright"], 1),
                    "c": r["conf"],
                    "s": r["sensor"],
                    "n": r["night"],
                },
            }
            for r in rows
        ],
    }
    return write_json(day_path(day_iso), fc)


def read_day(day_iso):
    path = day_path(day_iso)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            fc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    rows = []
    for feat in fc.get("features", []):
        lon, lat = feat["geometry"]["coordinates"]
        p = feat["properties"]
        rows.append({
            "lat": lat, "lon": lon, "date": p.get("d", day_iso), "time": p.get("t", "0000"),
            "frp": p.get("frp", 0.0), "bright": p.get("b", 0.0),
            "conf": p.get("c", 1), "sensor": p.get("s", "?"), "night": p.get("n", 0),
        })
    return rows


def prune_old_days(keep_dates):
    if not os.path.isdir(DAYS_DIR):
        return
    keep = set(keep_dates)
    for name in os.listdir(DAYS_DIR):
        if not name.endswith(".geojson"):
            continue
        if name[:-8] not in keep:
            os.remove(os.path.join(DAYS_DIR, name))
            print(f"  pruned {name}")


def update_daily_summary(by_date, cluster_features):
    """Append-only archive. FIRMS NRT is a rolling window with no history; this file
    is the project's own accumulating record, and its git history is the backup."""
    path = os.path.join(OUT_DIR, "daily_summary.json")
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                for row in json.load(f):
                    existing[row["date"]] = row
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            existing = {}

    active_by_date = {}
    fre_by_date = {}
    for f in cluster_features:
        for d in f["properties"]["daily"]:
            active_by_date[d] = active_by_date.get(d, 0) + 1
        # Daily FRE is summed from per-cluster integrals, so regional totals
        # inherit the per-cluster method rather than integrating a region-wide
        # FRP series that is only ever partially observed at any instant.
        for d, gj in f["properties"].get("fre_daily", {}).items():
            fre_by_date[d] = fre_by_date.get(d, 0.0) + gj

    for d, rows in by_date.items():
        existing[d] = {
            "date": d,
            "detections": len(rows),
            "fre_gj": round(fre_by_date.get(d, 0.0), 1),
            "frp_total": round(sum(r["frp"] for r in rows), 1),
            "frp_max": round(max((r["frp"] for r in rows), default=0.0), 1),
            "clusters": active_by_date.get(d, 0),
            "high_conf": sum(1 for r in rows if r["conf"] == 2),
        }

    out = [existing[k] for k in sorted(existing)]
    write_json(path, out)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not MAP_KEY:
        print("FATAL: FIRMS_MAP_KEY is not set. Add it as a repository secret.", file=sys.stderr)
        return 1

    today = datetime.now(timezone.utc).date()
    # FIRMS NRT lags by a few hours; today's file is always partial and is
    # refreshed on every run rather than treated as final.
    window = [(today - timedelta(days=i)).isoformat() for i in range(WINDOW_DAYS - 1, -1, -1)]

    print(f"FireWatch ETL — {REGION_NAME}  bbox={AREA}  window={window[0]}..{window[-1]}")

    by_date = {}
    fetched = 0
    for i, day in enumerate(window):
        age = len(window) - 1 - i
        have = os.path.exists(day_path(day))
        if have and age >= REFRESH_DAYS:
            by_date[day] = read_day(day)
            print(f"  {day}  cached ({len(by_date[day])})")
            continue

        print(f"  {day}  fetching")
        rows = fetch_day(day)
        fetched += 1

        # Never overwrite a good cached day with an empty response — a transient
        # API failure should not blank out data already on disk.
        if not rows and have:
            print("    ! empty response, keeping cached file")
            by_date[day] = read_day(day)
            continue

        size = write_day(day, rows)
        by_date[day] = rows
        print(f"    wrote {len(rows)} detections ({size/1024:.0f} KB)")

    all_points = [r for d in window for r in by_date.get(d, [])]
    print(f"\n{len(all_points)} detections in window; clustering "
          f"(eps={EPS_KM} km, minPts={MIN_PTS})…")

    t0 = time.time()
    labels = dbscan(all_points, EPS_KM, MIN_PTS)
    clusters, fre_diag = summarise_clusters(all_points, labels, window)
    noise = sum(1 for l in labels if l < 0)
    fre_total_gj = sum(c["properties"]["fre_gj"] for c in clusters)
    print(f"  {len(clusters)} fire complexes, {noise} unclustered "
          f"({time.time() - t0:.1f}s)")
    print(f"  median overpass interval {fre_diag['median_overpass_interval_h']} h "
          f"→ edge dt {fre_diag['edge_dt_h']} h")
    print(f"  total FRE {fre_total_gj/1e6:.2f} PJ")

    size = write_json(os.path.join(OUT_DIR, "fires.geojson"),
                      {"type": "FeatureCollection", "features": clusters})
    print(f"  fires.geojson {size/1024:.0f} KB")

    summary = update_daily_summary(by_date, clusters)

    active = [c for c in clusters if c["properties"]["status"] == "active"]
    meta = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "region": REGION_NAME,
        "bbox": [float(x) for x in AREA.split(",")],
        "window": window,
        "sources": [s for s, _ in SOURCES],
        "params": {
            "eps_km": EPS_KM, "min_pts": MIN_PTS, "window_days": WINDOW_DAYS,
            "fre": fre_diag,
        },
        "counts": {
            "detections": len(all_points),
            "clusters": len(clusters),
            "active_clusters": len(active),
            "unclustered": noise,
            "by_day": {d: len(by_date.get(d, [])) for d in window},
            "by_sensor": {
                code: sum(1 for r in all_points if r["sensor"] == code)
                for _, code in SOURCES
            },
        },
        "fre_total_gj": round(fre_total_gj, 1),
        "frp_total_mw": round(sum(r["frp"] for r in all_points), 1),
        "frp_max_mw": round(max((r["frp"] for r in all_points), default=0.0), 1),
        "archive_days": len(summary),
        "requests_made": fetched * len(SOURCES),
    }
    write_json(os.path.join(OUT_DIR, "meta.json"), meta)

    prune_old_days([(today - timedelta(days=i)).isoformat() for i in range(RETAIN_DAYS)])
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
