# arrow — tribal knowledge

**Answers:** how do I get there turning left only on a green arrow, and around the hills?
**Lives:** `github.com/MiteTython/Plane` → `arrow/` → https://mitetython.github.io/Plane/arrow/
**Data, phone:** IndexedDB `arrow` / `kv` / `graph` = the LA roads `{meta, buf}` (~18 MB); localStorage `arrow-cfg` (look, flat, recents), `arrow-base` (last good route), `arrow-lastfail`
**Data, repo:** `arrow/data/` — `la-graph.bin.gz` + `manifest.json` (+ `report.json`, `whitelist.geojson`, `baseline/`), written only by the "arrow map data" Action
**Built from:** app-builder skill, 2026-10-29
**Current:** build 2 · data `20261029T021955Z` (442,005 road segments, 6.5 MB download, OSM of 2026-10-28)

## How it works

1. **First open:** `manifest.json` from Pages (raw.githubusercontent.com when Pages lags) → `la-graph.bin.gz` streamed with a progress bar → gunzip (DecompressionStream) → sha256 against the manifest → IndexedDB → the router worker. **Every open after:** IndexedDB → worker in about a second, then a quiet manifest check 5 s later; a newer build downloads in the background and swaps in.
2. **Destination:** search (Photon, LA bbox, biased to the map center; Nominatim fallback only after typing stops) or press-and-hold the map. **Origin:** the phone's location, or a place from the From row.
3. **Routing, on the phone** (`<script id="router">`, run as a Blob worker): A* over directed edges. Each directed edge carries a u16 turn mask for the node it arrives at — bit k = may leave by the k-th road there, roads in ascending edge id. Three masks: `car`, `arrow` (car minus every blocked left), protected-left (green chips, "lefts on an arrow"). Costs copy build.py's Router: length ÷ speed, +8 s signal, +4 right, +8 left, +25 U-turn; Avoid hills multiplies uphill edges ×1.25 (5%+ average), ×3.3 (7%+), ×20 (10%+), ×50 (15%+ climb anywhere on it).
4. **Snapping:** the nearest point on a road within 1 km — never a freeway, ramp, bridge or tunnel. 45 s extra for pulling away or arriving with the place across a two-way street. Two candidates each end, up to four searches before "no way there".
5. **Pins + hand-off:** where arrow's route leaves the plain `car` route, after each turn inside those stretches, 80 m after the start, 80 m before the end; none on freeways or ramps, ≥70 m apart, ≤12 (≤6 when the routes match). `maps.apple.com/directions?source&destination&waypoint…&mode=driving`.

## The data build

`.github/workflows/arrow-data.yml` runs `arrow/build/build.py`: the 2nd of every month at 10:23 UTC, on any commit under `arrow/build/` (a `whitelist.csv` row, say), or by hand (Actions → arrow map data → Run workflow). About 4 minutes.

Stages: fetch Geofabrik SoCal (md5) → osmium clip to LA County, highways + restrictions → LADOT double-left signs (falls back to the last good copy) → graph (split at junctions, classify every left, masks, drop islands) → elevation (AWS skadi tiles, 1″ USGS 3DEP) → write + read back → probe (four canned routes, Python copy of the phone's router) → publish. Same roads as last time (sha256) → nothing is written, nothing commits, phones don't re-download.

When it breaks: the run page shows a red annotation — stage, expected vs got, the dump path, traceback, counts so far. The full log, baselines and dumps are the `arrow-build-diagnostics` artifact on that run.

## Decisions and why

- 2026-10-29 — **No server (build 2).** Build 1 needed Docker + GraphHopper + a tunnel; Charlie: "can i just have it run by opening the app". The graph is built once a month in GitHub Actions and routed on the phone. The arrow-server bundle is retired.
- 2026-10-29 — **Own binary format, not GraphHopper's:** flat little-endian sections that become typed arrays with no copying (layout in build.py's docstring and `write_graph`). The worker checks magic, format, every section length and every node id before swapping a graph in.
- 2026-10-29 — **Pages first, raw.githubusercontent.com second:** a push made by the Action's token may not rebuild Pages, so the workflow also asks Pages to publish; raw (CORS `*`, 5-minute cache) covers the gap. The sha256 check catches a stale copy from either.
- 2026-10-29 — **Strict:** every left without an arrow is blocked, including one-way and T-stem lefts at signals ("geo", 4,777 of them). Relaxing means dropping `geo` from the arrow mask in `build_masks`.
- 2026-10-29 — **Whitelist sources:** OSM dual lefts at a signal (≥2 left lanes in `turn:lanes`), LADOT double-left lane signs (STK 1588/1589/1598/1616; 215 of 243 matched), `arrow/build/whitelist.csv` by hand. No public dataset of single-lane arrows exists (LADOT and County DPW layers checked 2026-10-28).
- 2026-10-29 — Apple Maps layout (full-bleed map, translucent bottom sheet, floating controls top right) instead of the house tab nav — the ask was "looks and functions like Apple or Google Maps". Settings floor, log, dump, BUILD, motion block all kept.
- 2026-10-29 — MapLibre GL 5.24.0 from jsDelivr + OpenFreeMap `liberty` / `dark`: free, no key.
- 2026-10-29 — No service worker (ios-pwa.md). The roads are the only offline part; the map tiles and search still need a connection.

## Gotchas

- Whitelist coverage is thin: 3,330 approaches county-wide. Routes lean on rights. `whitelist.csv` is how it grows (rows match the nearest signalized approach within 45 m; misses are WARN lines in the build).
- 943 OSM dual-left approaches were dropped as "unsignalized" — no signal node within 35 m of the junction. Wide intersections map their signals farther out; those lefts are blocked today though they likely have arrows.
- 18,691 OSM turn restrictions don't resolve onto the graph (most are `no_u_turn`, already impossible here, or ride on service roads that aren't in it).
- iOS can clear IndexedDB when the phone runs short of space; the app then downloads the roads again on open.
- `gzip.open()` takes no `mtime`; `gzip.compress(..., mtime=0)` does. That's what keeps an unchanged graph byte-identical.
- Claude's sandbox can't reach github.io or the Actions log/artifact storage; raw.githubusercontent.com and the run annotations work.
- Manifest or status-bar meta changed → delete the icon, re-add. Reload won't do it.
- Legacy `maps.apple.com/?daddr=` links regressed in 2025; the unified `/directions` URL is the supported one. Apple tops out around 13 stops.
- `.search .clr[hidden]` needs its own display:none — a class selector with display beats the hidden attribute.

## Session log

**2026-10-29** — build 1: search, press-and-hold pin, From picker, Avoid hills, route card, green left chips, pins, Open in Apple Maps + Copy link, Settings, all against a GraphHopper server in Docker. Never run.

**2026-10-29** — build 2: server gone. Data Action built and published `20261029T021955Z` after two fixes (gzip `mtime`; failures now annotate the run page). Probes: Sherman Oaks → DTLA 21.5 min arrow vs 21.4 car; DTLA → Sherman Oaks 21.0 both; Santa Monica → Pasadena 33.7 vs 33.5; Long Beach → Burbank 42.4 vs 39.8. App routes in a worker from IndexedDB; Settings shows the map data (size, OSM date, segments, green-arrow approaches, Check for new). Not yet opened on a phone. Open: first real route, Sherman Oaks → DTLA, in the car through Apple Maps.
