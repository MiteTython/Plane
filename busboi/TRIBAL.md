# busboi — tribal knowledge

One sentence: **how many minutes until you walk out the door.** Transit, Bay Area, for a guy who was Ubering to West Berkeley.

URL: https://mitetython.github.io/Plane/busboi/ · one file, `index.html`, no build step.

## Decisions

- **Routing = Transitous** (`api.transitous.org/api/v1/plan`, MOTIS). Free, no key, CORS works from the browser, carries the full 511 regional feed (`us-ca-SF-bayarea_*` stop ids), returns `headsign`, `routeShortName`, `routeColor`, `realTime`. Policy: non-commercial, ≤720 `/plan` a day, send a Referer (browsers do). No fallback engine built — Pi/MOTIS or Google only if this flakes.
- **Transitous geocoder is bad at street addresses** (sent "450 28th St Oakland" to Tempe, AZ; "2929 Seventh" to a BART station). Places are geocoded with **Nominatim** biased to a Bay Area viewbox; the five starting places are hardcoded coords in `CONF.places`.
- Origin = phone location (`getCurrentPosition`, 6 s timeout, 60 s cache). Within 250 m of a place = "at <place>". Location off → assume home, amber flag.
- Direction guess, by location only: at home → work; anywhere else → home. Tap a place row to override; refresh keeps the override.
- Big number = first itinerary with ≥2 min of lead (`CONF.slack`); the one after it is the comparison. Lead ≤0 shows `now` green; ≤2 amber.
- Legs: two lines. Bus = `line · headsign` (the front of the bus, verbatim GTFS). BART = color dot + `Color · headsign` (`Red-N` → `Red`). Stop line = `stop · <computed bound> · live|scheduled`. Direction is the compass bearing between the leg's from/to coordinates, not GTFS direction_id.
- Walk cap 1200 s each end (~1 mile).
- Return list: work → from a clock time (Settings, default 17:30); other places → arrival + 120 min. Three rows.
- Refresh: on open, pull-down, tap the status pill, and when the app comes back to the foreground after >5 min. Clock re-renders every 30 s without refetching. Fetch age ≥2 min = amber "stale".
- Last good main trip snapshots to `localStorage` (`busboi-snap`) and renders on a failed fetch with an offline flag. No service worker (ios-pwa.md: don't by default).
- Look: system by default; Dawn/Native follow the OS; brand colors come from the feed's `routeColor`.

- **Emery Go-Round is in Transitous** (agency "Emery Go-Round", real-time true) — Hollis and Shellmound. No tripshot scraping, no PDF timetables. A place can carry `via` (a Transitous stop id) + `viaName`: the plan runs through that stop (his way), and the fastest direct trip is fetched alongside and tagged `direct` in amber in the then-list. Work and Bay Street go via `us-ca-SF-bayarea_mtc:macarthur-bart`. The return from a via-place also goes via it (the loops come back to MacArthur).
- Headsign equal to the line name is dropped (Hollis, not "Hollis · Hollis"); "(Timepoint)" and trailing NB/SB are stripped from stop names — direction comes from the computed bound.

## Gotchas

- Harness: cdnjs and cartocdn are blocked from the workspace; shot.js serves Leaflet from `node_modules` and tiles as a 1px PNG. Tile `<img>` loads still show as REQFAIL in the log — ignore.
- `/Plane/` is capitalized in the Pages path; manifest scope and start_url match that.
- Transitous returns times as UTC ISO; everything is converted through `new Date()` and rendered local.
- The "then" list and comparison are itineraries sorted by *start* (when you'd leave), which can put a later-arriving BART option before a bus; that's intentional — it's about when you leave.
- Workspace can't reach api.transitous.org (allowlist), so the Playwright pass mocks `/plan` from a fixture shaped like the real response. Real-world check happened through Chrome on 2026-10-26: home→work gave 72M · Point Richmond, 38 min, 0 transfers.

## Not built yet (scoped, answered, in order)

1. 511 live StopMonitoring per leg stop (key in Settings/localStorage; verify browser CORS first) + amber as-of chip.
2. Movies: theater tap → native time wheel + 5-min runtime stepper; exit = start + runtime + `exit` (15 GLK/Piedmont, 30 Bay Street); arrive-by outbound; three returns; last-train pill when exit after 10pm; held until exit passes.
3. Morning job: arrive work 9:00, weekdays minus US federal holidays, ntfy push at leave-by − 10, 9pm preview the night before. GitHub Action or the Pi.

## Log

- 2026-10-26 · build 7 · fares: per-boarding table in CONF (Emery Go-Round $0, AC Transit $2.50 / $0.50 second boarding, BART $2.40 floor shown with `+`, unknown agency $2.50 with `~`). Route chips, the caption and place rows show it (`free` when $0). Default route = cheapest within 15 min of the fastest, ties to soonest. Reason: the router optimizes minutes and would never surface the free shuttle on its own.

- 2026-10-26 · build 6 · via applies in both directions: leaving a via-place (at work → home) plans through MacArthur too, so the Hollis shows as a route from work; direct alternatives (any not touching the via) are merged in, tagged.

- 2026-10-26 · build 5 · CARTO basemaps went key-only mid-morning ("API KEY REQUIRED" tiles) → OSM standard tiles, dark look = CSS invert/hue-rotate on `.leaflet-tile-pane`. `maxTransfers=1` on every plan (Transitous honors it; client filters too) after the router served a 36→57→6 three-bus trip. Routes A · B · C: itineraries grouped by line signature (72/72M/72L fold to one corridor), each route shown by its soonest makeable departure; main = soonest route or the picked one; picks are by signature so they survive refresh; `then` = later departures of the shown route. Pin labels auto-side.

- 2026-10-26 · build 4 · the map: Leaflet 1.9.4 off cdnjs (loaded lazily), CARTO dark_all/light_all tiles following the look, leg geometry decoded from MOTIS `legGeometry` (encoded polyline, stated precision; straight line on mismatch), walk dotted, ride in the line's color, on/off pins with labels (tap → Apple Maps), green dot at the door, live blue dot via watchPosition while Home is visible. The Leaflet node is carried across the 30 s re-render (`#map-slot` swap + invalidateSize) and only redrawn when the itinerary key changes. `open in Apple Maps` pill = `maps.apple.com/?saddr=&daddr=&dirflg=r` (transit directions; Apple picks its own itinerary). Return trips: no map (his call).

- 2026-10-26 · build 3 · fixed return planning when standing at a known place (origin lost its kind/id → home→home → empty); vehicle legs read `on at X · off at Y`, both Apple Maps links (`maps.apple.com/?q=&ll=`); the then-list became tappable `rides` chips — tap one and the big number follows it, tap again for auto; picks reset on every fetch.

- 2026-10-26 · build 2 · via stops (his way through MacArthur for work and Bay Street), direct alternative tagged, Emery Go-Round naming cleanup, via field on add-place.

- 2026-10-26 · build 1 · scaffold, Transitous, five places, home number + comparison, legs, stacked return, place rows with their own minutes, settings/log/reload, add-place via Nominatim.
