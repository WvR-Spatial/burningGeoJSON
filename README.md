# FireWatch

**Active fire detection and growth tracking across Australia, built entirely on static hosting.**

[![Update FIRMS data](https://github.com/wvr-spatial/REPO_NAME/actions/workflows/update-firms.yml/badge.svg)](https://github.com/wvr-spatial/REPO_NAME/actions/workflows/update-firms.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**[→ Live map](https://wvr-spatial.github.io/REPO_NAME/)**

FireWatch ingests NASA FIRMS thermal anomaly detections from four satellite sensors, clusters them into discrete fire complexes, integrates Fire Radiative Power over time to estimate energy released, and tracks day-over-day growth — updating itself every six hours with no server, no database and no runtime API calls.

![FireWatch dashboard](docs/screenshot.png)

---

## What it does

- **Four sensor streams.** VIIRS 375 m from Suomi-NPP, NOAA-20 and NOAA-21, plus MODIS 1 km, normalised into a single schema.
- **Fire complexes, not pixels.** DBSCAN groups detections into discrete fires, drawn as footprint polygons with per-complex statistics.
- **Radiative energy.** FRP integrated over satellite overpasses to estimate Fire Radiative Energy in gigajoules — the physically meaningful measure of cumulative burning.
- **Growth tracking.** Each complex carries a day-by-day energy curve and a trend ratio comparing current activity against its own prior baseline.
- **An accumulating archive.** FIRMS near-real-time data is a rolling seven-day window with no history. Because each scheduled run commits its output, this repository accumulates a time series the source API does not retain.
- **Deep-linkable state.** Filters, day selection, basemap and selected complex all serialise to the URL, so any view can be shared.

---

## Architecture

```mermaid
flowchart LR
    A["NASA FIRMS<br/>Area API"] -->|"7 days × 4 sensors<br/>key held as repo secret"| B["GitHub Actions<br/>every 6 hours"]
    B --> C["Normalise<br/>MODIS + VIIRS schemas"]
    C --> D["DBSCAN<br/>eps 1.5 km · minPts 3"]
    D --> E["FRE integration<br/>trapezoidal, per complex"]
    E --> F[("Static GeoJSON<br/>committed to the repo")]
    F -->|same-origin fetch| G["Mapbox GL JS<br/>dashboard"]
```

### Why the data pipeline runs in CI

The obvious implementation calls the FIRMS API from the browser. That fails for two reasons: the endpoint requires a key, which would be publicly visible in client-side JavaScript, and browser requests need CORS headers the API does not reliably provide. The common workaround — routing through a free public CORS proxy — makes the site's availability depend on a third party with no uptime commitment. That is exactly how the previous version of this project broke.

Moving the fetch into a scheduled GitHub Action removes the whole failure class. The API key lives in an encrypted repository secret and never reaches a browser. The page fetches same-origin static files, so CORS does not apply. There is no third-party dependency at runtime, and the map loads instantly because the heavy work — parsing tens of thousands of CSV rows, clustering, integration — already happened in CI.

### Why data is split by day

`data/days/YYYY-MM-DD.geojson` is written once and then frozen; only the current and previous day change between runs. Committing a single monolithic file four times daily would add gigabytes of git history per year. This layout stores one blob per day.

---

## Methodology

### Detections

Each point is a **thermal anomaly**: a satellite pixel whose mid-infrared signature exceeded a detection threshold at the moment of overpass. It is not a fire perimeter, and its coordinates are the pixel centroid — 375 m for VIIRS, 1 km for MODIS at nadir and considerably coarser off-nadir. Gas flares, industrial heat and hot bare ground all produce detections.

MODIS reports confidence as an integer 0–100 and VIIRS as a categorical `l`/`n`/`h`. Both are normalised to a common ordinal so the two products can be filtered together.

### Clustering

Detections across the full window are grouped with **DBSCAN** at `eps = 1.5 km`, `minPts = 3`.

The 1.5 km radius links pixels up to roughly four VIIRS cells apart — the scale of a contiguous fire front — without merging distinct fires in the same valley. `minPts = 3` discards isolated single-pixel detections, which is where most false positives live. These two parameters are the principal analytical judgement in the project and are exposed as workflow environment variables.

Neighbour queries use a uniform grid index sized so that each cell is at least `eps` wide in kilometres at every latitude in the study area, which keeps the 3×3 candidate search provably complete while avoiding an O(n²) scan. The implementation is validated against a brute-force reference.

Complexes are drawn as convex hulls. A hull **over**-estimates burnt area for a concave or ribbon-shaped fire; it is a footprint envelope, not a mapped burn scar.

### Fire Radiative Energy

FRP is an instantaneous rate in megawatts. Summing readings taken hours apart does not produce a physical total, because power does not sum across time — energy does. FRE is therefore estimated by trapezoidal integration of each complex's FRP time series across satellite overpasses, in gigajoules.

Integration is performed **per complex** rather than region-wide. A complex spans a few kilometres and is imaged whole in a single pass, whereas at any instant only a strip of the continent sits under a swath — a continental FRP series would undercount at every point and the integral would compound that error.

Three assumptions, each constrained:

| Assumption | Treatment |
|---|---|
| **Overpass binning** | Detections within 15 minutes are one observation; their FRP is summed. |
| **Edge interval** | The first and last observation of a burst have no neighbour on one side. Each is credited half the *median observed* overpass interval, measured from the dataset on every run and recorded in `meta.json`. |
| **Gap cap** | Where a complex goes unobserved for more than 12 hours — almost always cloud — the gap is not bridged. Interpolating would invent burning nobody measured. Each side receives a bounded tail, so FRE stops growing with the length of the hole. |

FRE is integrated over all detections regardless of the interface's confidence and sensor filters: removing observations from an energy integral biases it downward rather than subsetting it.

### Growth

Trend compares a complex's latest-day detection count against the mean of the preceding days it was active — a ratio, so small and large fires are directly comparable. Detection counts are a proxy for activity and are confounded by cloud cover, overpass timing and swath geometry: a fire can appear to recede simply because cloud obscured an afternoon pass.

---

## Limitations

These are stated plainly because the numbers are only useful if their bounds are understood.

- **FRE is a lower bound.** Fires peak mid-afternoon. If no overpass catches the peak, the integral misses it. Treat values as comparative, not absolute.
- **Absence of detections is not absence of fire.** Cloud blocks detection entirely.
- **Near-real-time data is provisional.** The science-quality product is published weeks later and will differ.
- **Detection counts measure observation as much as burning.** Overpass cadence, swath geometry and scan angle all affect how often a given fire is seen.
- **Convex hulls over-estimate irregular fires.** Area figures are envelopes.
- **Clustering is sensitive to its parameters.** Raising `eps` merges separate fires; lowering `minPts` admits flares and industrial heat sources.
- **Coverage is the configured bounding box only** — Australia by default. Fires outside it are not fetched.

---

## Tech stack

| Layer | Choice | Rationale |
|---|---|---|
| ETL | Python 3.12, **standard library only** | No dependency resolution to break the pipeline months later |
| Clustering | DBSCAN, implemented from scratch with a grid index | Avoids a scikit-learn dependency in CI; validated against brute force |
| Scheduling | GitHub Actions (`cron: 17 */6 * * *`) | Free, versioned alongside the code, self-sustaining |
| Mapping | Mapbox GL JS v3, globe projection | Vector rendering handles ~50k points comfortably |
| Charts | Chart.js 4 | Small, sufficient |
| Hosting | GitHub Pages | Static only, which the architecture is designed around |

---

## Repository structure

```
.
├── index.html                      Single-file dashboard — no build step
├── scripts/
│   └── fetch_firms.py              ETL: fetch, normalise, cluster, integrate
├── .github/workflows/
│   └── update-firms.yml            Six-hourly scheduled pipeline
└── data/                           Generated — committed by the workflow
    ├── days/YYYY-MM-DD.geojson     Detections, one frozen file per UTC day
    ├── fires.geojson               Fire complexes with footprints and statistics
    ├── daily_summary.json          Append-only archive time series
    └── meta.json                   Run metadata and the parameters used
```

---

## Running your own instance

1. Request a free MAP_KEY from [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/api/map_key/).
2. Add it as a repository secret named `FIRMS_MAP_KEY` under **Settings → Secrets and variables → Actions**.
3. Set **Settings → Actions → General → Workflow permissions** to *Read and write* so the pipeline can commit its output.
4. Replace the Mapbox access token near the top of the script block in `index.html` with your own, restricted to your Pages origin.
5. Run **Actions → Update FIRMS data → Run workflow** once. The schedule takes over afterwards.

The first run makes 28 API requests against a quota of 5,000 per ten minutes. Subsequent runs re-fetch only the two most recent days and read the rest from committed files.

### Configuration

All tuning is done through the workflow's environment block — no code changes:

| Variable | Default | Effect |
|---|---|---|
| `FIRMS_AREA` | `112,-45,155,-9` | Bounding box, `west,south,east,north` |
| `FIRMS_REGION_NAME` | `Australia` | Label shown in the interface |
| `FIRMS_DAYS` | `7` | Rolling window length |
| `FIRMS_RETAIN` | `30` | Day files kept in the working tree |
| `FIRMS_EPS_KM` | `1.5` | DBSCAN neighbourhood radius |
| `FIRMS_MIN_PTS` | `3` | Minimum detections to form a complex |
| `FIRMS_MAX_GAP_H` | `12` | Beyond this, FRE integration will not bridge a gap |

### Local development

```bash
python3 -m http.server 8000
```

Then open `http://localhost:8000`. Serve over HTTP rather than opening the file directly — `file://` breaks both the relative data fetches and the Mapbox origin check.

---

## Roadmap

- Persistence analysis across the accumulating archive: fires that reignite in the same footprint weeks apart
- Land cover context to separate savanna burning from forest fire
- Vector tiles (PMTiles) if peak-season detection volume outgrows plain GeoJSON
- Optional Landsat NRT layer for higher-resolution confirmation of large complexes

---

## Data and attribution

Active fire data courtesy of NASA's [Fire Information for Resource Management System (FIRMS)](https://earthdata.nasa.gov/firms), part of NASA's Earth Observing System Data and Information System (EOSDIS).

Basemaps © [Mapbox](https://www.mapbox.com/about/maps/) © [OpenStreetMap](https://www.openstreetmap.org/about/) contributors.

## Licence

Source code released under the MIT Licence — see [LICENSE](LICENSE). This applies to the code in this repository, not to the satellite data it retrieves, which remains subject to NASA's own terms of use.
