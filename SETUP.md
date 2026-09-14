# FireWatch — setup

Five steps, about ten minutes. After this the site updates itself every six hours with no further action.

## 1. Repository layout

Unzip into the repo root so the paths look like this:

```
.
├── index.html
├── scripts/
│   └── fetch_firms.py
├── .github/
│   └── workflows/
│       └── update-firms.yml
└── data/                  ← created by the first workflow run
```

The paths matter. `index.html` fetches `data/meta.json` relative to itself, and GitHub only runs workflows found in `.github/workflows/`.

## 2. Get a FIRMS MAP_KEY

Request one at <https://firms.modaps.eosdis.nasa.gov/api/map_key/>. It is free, arrives by email, and has no expiry. The quota is 5,000 transactions per 10 minutes; this pipeline uses about 28 per run.

## 3. Store the key as a repository secret

**Settings → Secrets and variables → Actions → New repository secret**

- Name: `FIRMS_MAP_KEY`
- Value: your key

Do not put it in any committed file. The whole point of this architecture is that the key never reaches the browser.

## 4. Add your Mapbox token

In `index.html`, replace the placeholder:

```js
mapboxgl.accessToken = 'pk.PASTE_YOUR_RESTRICTED_TOKEN_HERE';
```

Your existing URL restriction is origin-level (`wvr-spatial.github.io`), so the same token already works here — no change needed on the Mapbox side.

## 5. Run it once by hand

**Actions → Update FIRMS data → Run workflow.**

The first run fetches seven days (28 API calls, ~1 minute), clusters the detections, and commits `data/`. Watch the run summary — it prints detection counts per sensor, the number of fire complexes found, and the total FRP. If the key is wrong you will see `unexpected response body: Invalid MAP_KEY` in the log.

After that the schedule takes over: `cron: '17 */6 * * *'`.

---

## Changing the study area

One line in the workflow. The Area API takes an arbitrary bounding box, unlike the fixed regional CSV files the old build used:

```yaml
FIRMS_AREA: '112,-45,155,-9'      # west,south,east,north
FIRMS_REGION_NAME: 'Australia'
```

To genuinely include SE Asia — which the old page claimed but never fetched — use roughly `'92,-45,180,29'` and rename accordingly. Expect substantially more detections and larger files; Indonesian peat fires alone can exceed the whole Australian count.

## Tuning the clustering

```yaml
FIRMS_EPS_KM: '1.5'      # neighbourhood radius
FIRMS_MIN_PTS: '3'       # minimum detections to form a complex
```

Raise `eps` and separate fires merge into one complex. Lower `min_pts` and single-pixel false positives — gas flares, industrial heat — start appearing as fires. These two numbers are the main analytical judgement call in the project, and they are worth being able to defend.

## Why files are split by day

`data/days/YYYY-MM-DD.geojson` is written once and then frozen. Only today's and yesterday's files change between runs, so git stores one blob per day instead of re-committing a monolithic file four times daily. Files older than `FIRMS_RETAIN` (30 days) are deleted from the working tree — git history keeps them.

## The archive

`data/daily_summary.json` is append-only. FIRMS near-real-time data is a rolling 7-day window with no history, so after a month of scheduled runs you hold a time series that the source API cannot give you. The daily-activity chart automatically widens to show up to 30 days as it accumulates.

## Notes

- Scheduled workflows are disabled after 60 days of repository inactivity. This one commits on every run, which counts as activity — it keeps itself alive.
- GitHub queues scheduled jobs; a run may start 5–20 minutes after the nominal time. This is normal and does not matter for a 7-day fire product.
- NRT data is provisional. The science-quality product is published weeks later and will differ slightly.
