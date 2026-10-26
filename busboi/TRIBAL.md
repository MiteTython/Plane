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

## Gotchas

- `/Plane/` is capitalized in the Pages path; manifest scope and start_url match that.
- Transitous returns times as UTC ISO; everything is converted through `new Date()` and rendered local.
- The "then" list and comparison are itineraries sorted by *start* (when you'd leave), which can put a later-arriving BART option before a bus; that's intentional — it's about when you leave.
- Workspace can't reach api.transitous.org (allowlist), so the Playwright pass mocks `/plan` from a fixture shaped like the real response. Real-world check happened through Chrome on 2026-10-26: home→work gave 72M · Point Richmond, 38 min, 0 transfers.

## Not built yet (scoped, answered, in order)

1. 511 live StopMonitoring per leg stop (key in Settings/localStorage; verify browser CORS first) + amber as-of chip.
2. Movies: theater tap → native time wheel + 5-min runtime stepper; exit = start + runtime + `exit` (15 GLK/Piedmont, 30 Bay Street); arrive-by outbound; three returns; last-train pill when exit after 10pm; held until exit passes.
3. Morning job: arrive work 9:00, weekdays minus US federal holidays, ntfy push at leave-by − 10, 9pm preview the night before. GitHub Action or the Pi.

## Log

- 2026-10-26 · build 1 · scaffold, Transitous, five places, home number + comparison, legs, stacked return, place rows with their own minutes, settings/log/reload, add-place via Nominatim.
