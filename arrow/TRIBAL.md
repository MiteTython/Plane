# arrow — tribal knowledge

**Answers:** how do I get there turning left only on a green arrow, and around the hills?
**Lives:** `github.com/MiteTython/Plane` → `arrow/` → https://mitetython.github.io/Plane/arrow/
**Data:** localStorage only — `arrow-cfg` (server, look, flat, recents), `arrow-base` (last good route), `arrow-lastfail`
**Server:** the `arrow-server` Docker bundle (prep → GraphHopper 11.0 → probe → cloudflared). Not in this repo.
**Built from:** app-builder skill, 2026-10-29
**Current:** build 1

## How it works

1. Destination: search (Photon, LA bbox, biased to the map center; Nominatim fallback only after typing stops) or press-and-hold the map.
2. Origin: the phone's location, or a place picked from the From row.
3. Two GraphHopper calls in parallel: `arrow` (or `arrow_flat` with Avoid hills) and plain `car`. `curbside=right` at both ends so the route pulls away and arrives without a left across traffic.
4. Pins: where arrow's route leaves car's (Apple would cut across there), after each turn inside those stretches, 80 m after the start, 80 m before the end. None on freeways or ramps, ≥70 m apart, ≤12 total (≤6 when the two routes match).
5. Hand-off: `maps.apple.com/directions?source&destination&waypoint…&mode=driving` (unified Maps URLs, iOS 18.4+). Pins arrive in Apple Maps as stops.

## Decisions and why

- 2026-10-29 — Apple Maps layout (full-bleed map, translucent bottom sheet, floating controls top right) instead of the house tab nav and page labels — the ask was "looks and functions like Apple or Google Maps". Settings floor, log, dump, BUILD, motion block all kept.
- 2026-10-29 — MapLibre GL 5.24.0 from jsDelivr + OpenFreeMap `liberty` / `dark` vector styles: free, no key, closest to Apple's look. (busboi learned CARTO went key-only; OpenFreeMap has no key to revoke.)
- 2026-10-29 — Strict: every left without an arrow is blocked, including one-way and T-stem lefts at signals. The server can allow those ("geo" lefts) with a config change; not built into the app.
- 2026-10-29 — The app calls GraphHopper directly (its CORS filter is on by default). No proxy.
- 2026-10-29 — No service worker (ios-pwa.md). No offline snapshot: a stale route isn't worth showing.
- 2026-10-29 — Error cards map GraphHopper's own messages: "Connection between locations not found" → no way without an unprotected left; "Cannot find point" → no road there; "out of bounds" → outside LA County.

## Gotchas

- Manifest or status-bar meta changed → delete the icon, re-add. Reload won't do it.
- The quick-tunnel URL changes every time the server restarts. Re-send `https://mitetython.github.io/Plane/arrow/?server=<url>` or paste it in Settings.
- Legacy `maps.apple.com/?daddr=` links regressed in 2025 (Apple dev forums); the unified `/directions` URL is the supported one.
- Apple Maps tops out around 13 stops per route.
- `.search .clr[hidden]` needs its own display:none — a class selector with display beats the hidden attribute.

## Session log

**2026-10-29** — build 1. Search, press-and-hold pin, From picker, Avoid hills switch, route card (time, miles, lefts on arrows, steepest climb, extra minutes vs the plain route), protected lefts marked green on the map, pins drawn as white dots, Open in Apple Maps + Copy link, Settings (server + Test, look, build, reload, log, dump), `?server=` link. Not yet run against a live server or on a phone. Open: first real route Sherman Oaks → DTLA; check how Apple Maps handles the stops in the car.
