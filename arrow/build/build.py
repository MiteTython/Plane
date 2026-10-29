#!/usr/bin/env python3
"""
build — the LA County road graph for the arrow phone app.

Runs in GitHub Actions (.github/workflows/arrow-data.yml): monthly, and whenever this
folder changes. Writes arrow/data/la-graph.bin.gz + manifest.json. The app downloads
that once, keeps it, and routes on the phone. No server anywhere.

Stages (each logged, baselined and dumped on failure by diag.py):
  fetch_osm   Geofabrik SoCal extract, md5-checked
  extract     osmium: clip to LA County, keep highways and turn restrictions
  ladot       LADOT double-left lane signs (whitelist source; falls back to the last good copy)
  graph       split roads at junctions, classify every left, build the turn masks
  elevation   AWS Terrain Tiles (skadi, 1 arc-second, USGS 3DEP in the US) -> grades per edge
  write       binary graph, gzip, manifest; read back for the probe
  probe       canned routes on the written file: car, arrow, arrow_flat must all route,
              arrow never cheaper than car, arrow differs from car somewhere

How a left gets blocked (right-hand traffic):
  Every movement at a junction gets a signed turn angle from the bearings 20 m along each
  leg (negative = left). A leftward movement is a chord across a circle around the junction,
  lanes offset to the right. If it crosses the chord of a stream with right of way it's
  unprotected:
    permissive  crosses oncoming straight/right traffic (green ball, yield)
    yield       unsignalized, crosses traffic of equal or higher road class
    geo         signalized, nobody oncoming (one-way approach, T stem, off-ramp) — strict blocks it
  Divided roads: the oncoming carriageway meets the median connector one node later, so a
  short connector into an opposing one-way counts as oncoming.
  Exempt: freeway merges/diverges, roundabouts, following the main road around a bend, and
  whitelisted protected approaches: OSM double lefts at a signal, LADOT double-left lane
  signs, and arrow/build/whitelist.csv.

Binary format (little-endian), see write_graph() and the app's worker:
  192-byte header: magic "ARW1", format, nodes, edges, geometry points, bbox, 16 section slots
  sections: node lat/lon (i32, 1e-6°), node flags (u8: 1 = signal), edge u/v (u32),
  length (u16 m), speed (u8 km/h), flags (u8: 1 fwd, 2 bwd, 4 roundabout, 8 no-snap,
  class << 4), grades (i8 x3: avg fwd %, max climb fwd %, max climb bwd %), geometry start
  (u32, edges+1), geometry (i16 pairs, 1e-5° deltas from the previous point, first from u),
  turn masks (u16 per directed edge e*2+dir; bit k = may leave the head node by its k-th
  incident edge, incident edges in ascending edge id): car, arrow, protected-left.
"""
import argparse
import csv
import gzip
import hashlib
import heapq
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from array import array
from collections import defaultdict

import numpy as np
import osmium

import diag

HERE = os.path.dirname(os.path.abspath(__file__))
OSM_URL = os.environ.get("OSM_URL", "https://download.geofabrik.de/north-america/us/california/socal-latest.osm.pbf")
BBOX = (-118.96, 33.70, -117.64, 34.83)          # LA County, Catalina left out
LADOT_URL = "http://maps.lacity.org/lahub/rest/services/LADOT/MapServer/18/query"
# 1588 Double Turn Left, Symbol, No U Turn · 1589 Double Left or U Turn, Symbol
# 1598 Two Lanes ONLY, Left Arrow; Left Arrow, Straight Arrow · 1616 Two Lanes ONLY, Left Arrows
LADOT_STK = ("1588", "1589", "1598", "1616")
SKADI = "https://elevation-tiles-prod.s3.amazonaws.com/skadi/N{lat:02d}/N{lat:02d}W{lon:03d}.hgt.gz"
UA = "arrow-build/1 (github.com/MiteTython/Plane; personal no-unprotected-left router)"
FORMAT = 1
MAGIC = 0x31575241                                  # b"ARW1" read little-endian

# --- roads ---------------------------------------------------------------------
CLS = {"motorway": 0, "motorway_link": 1, "trunk": 2, "trunk_link": 3, "primary": 4, "primary_link": 5,
       "secondary": 6, "secondary_link": 7, "tertiary": 8, "tertiary_link": 9, "unclassified": 10,
       "residential": 11, "living_street": 12, "road": 13}
CAR_HW = set(CLS)                                   # service roads are left out: no lot cut-throughs,
                                                    # no lefts into driveways; Apple does the last 50 m
SPEED = {"motorway": 97, "motorway_link": 56, "trunk": 72, "trunk_link": 48, "primary": 56, "primary_link": 40,
         "secondary": 52, "secondary_link": 40, "tertiary": 48, "tertiary_link": 35, "unclassified": 40,
         "residential": 35, "living_street": 16, "road": 32}
FREEWAY = {"motorway", "motorway_link", "trunk_link"}
NOSNAP = {"motorway", "motorway_link", "trunk_link"}
RANK = {"motorway": 10, "trunk": 9, "primary": 8, "secondary": 7, "tertiary": 6,
        "unclassified": 5, "residential": 5, "road": 5, "living_street": 4,
        "motorway_link": 3, "trunk_link": 3, "primary_link": 3, "secondary_link": 3, "tertiary_link": 3}
LEFT_TOKENS = {"left", "sharp_left"}
LANE_KEYS = ("turn:lanes", "turn:lanes:forward", "turn:lanes:backward")

# --- geometry knobs ---------------------------------------------------------------
PROBE_M = 20.0          # a leg's bearing = node -> the point 20 m along it
SLIGHT = 20.0           # within 20° of the straightest leg = going straight
HAIRPIN = 165.0         # |turn| beyond this = U-turn-like
ONCOMING = 60.0         # an inbound leg within ±60° of dead ahead is oncoming traffic
CONTINUATION = 100.0    # following the main road around a bend, up to 100°
SIGNAL_R = 35.0         # a traffic signal within 35 m makes a junction signalized
MEDIAN_M = 45.0         # a connector shorter than this into an opposing one-way = median crossing
POCKET_M = 150.0        # a separately mapped left pocket, up to 150 m
EPS = 0.01              # lane offset on the chord circle, degrees
MIN_COMPONENT = 300     # road islands smaller than this (edges) are dropped
SIMPLIFY_M = 3.0        # geometry tolerance for the phone
M_LAT = 110_574.0
M_LON0 = 111_320.0

# --- routing constants shared with the app's worker ------------------------------
VMAX = 30.6             # m/s, fastest edge (110 km/h cap) -> admissible A* heuristic
TURN = {"signal": 8.0, "right": 4.0, "left": 8.0, "uturn": 25.0}


# =============================================================================
# plumbing
# =============================================================================
def http(url, st, key, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            st.keep(key, {"url": url, "status": r.status, "headers": dict(r.headers),
                          "body_head": body[:400].decode("utf-8", "replace") if len(body) < 2_000_000 else f"{len(body)} bytes"})
            return body
    except urllib.error.HTTPError as e:
        body = e.read()[:4000].decode("utf-8", "replace")
        st.keep(key, {"url": url, "status": e.code, "headers": dict(e.headers or {}), "body": body})
        raise diag.StageFailure(st.name, f"HTTP 200 from {url}", f"HTTP {e.code}: {body[:200]}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        st.keep(key, {"url": url, "error": repr(e)})
        raise diag.StageFailure(st.name, f"a response from {url}", repr(e))


def download(url, dest, st):
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    md5, n, last, t0 = hashlib.md5(), 0, 0, time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            st.keep("download", {"url": url, "status": r.status, "headers": dict(r.headers)})
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                md5.update(chunk)
                n += len(chunk)
                if n - last >= 200 << 20:
                    st.progress(f"{n >> 20} of {total >> 20} MB, {time.time() - t0:.0f}s")
                    last = n
    except urllib.error.HTTPError as e:
        st.keep("download", {"url": url, "status": e.code, "body": e.read()[:2000].decode("utf-8", "replace")})
        raise diag.StageFailure(st.name, f"HTTP 200 from {url}", f"HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise diag.StageFailure(st.name, f"the whole file from {url}", f"{repr(e)} after {n} bytes")
    if total:
        st.check(n == total, f"{total} bytes (Content-Length)", f"{n} bytes")
    os.replace(tmp, dest)
    return md5.hexdigest(), n


def sh(cmd, st, key):
    p = subprocess.run(cmd, capture_output=True, text=True)
    st.keep(key, {"cmd": cmd, "rc": p.returncode, "stdout": p.stdout[-4000:], "stderr": p.stderr[-4000:]})
    st.check(p.returncode == 0, f"exit 0 from {' '.join(cmd[:2])}", f"exit {p.returncode}: {p.stderr.strip()[-400:]}")
    return p.stdout


# =============================================================================
# geometry
# =============================================================================
def dist_m(lat1, lon1, lat2, lon2):
    k = math.cos(math.radians((lat1 + lat2) * 0.5))
    return math.hypot((lon2 - lon1) * M_LON0 * k, (lat2 - lat1) * M_LAT)


def bearing(lat1, lon1, lat2, lon2):
    """Degrees clockwise from north, 0..360."""
    k = math.cos(math.radians((lat1 + lat2) * 0.5))
    return math.degrees(math.atan2((lon2 - lon1) * M_LON0 * k, (lat2 - lat1) * M_LAT)) % 360.0


def turn(heading_in, heading_out):
    """Signed turn, -180..180. Negative = left, positive = right."""
    return (heading_out - heading_in + 540.0) % 360.0 - 180.0


def seg_dist(plat, plon, alat, alon, blat, blon):
    k = math.cos(math.radians(plat))
    ax, ay = (alon - plon) * M_LON0 * k, (alat - plat) * M_LAT
    bx, by = (blon - plon) * M_LON0 * k, (blat - plat) * M_LAT
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
    return math.hypot(ax + t * dx, ay + t * dy)


class Grid:
    def __init__(self, cell_deg=0.0005):
        self.c = cell_deg
        self.d = defaultdict(list)

    def add(self, lat, lon, item=None):
        self.d[(int(lat // self.c), int(lon // self.c))].append((lat, lon, item))

    def near(self, lat, lon, r_m):
        rl = r_m / M_LAT
        rn = r_m / (M_LON0 * math.cos(math.radians(lat)))
        for iy in range(int((lat - rl) // self.c), int((lat + rl) // self.c) + 1):
            for ix in range(int((lon - rn) // self.c), int((lon + rn) // self.c) + 1):
                for (a, b, it) in self.d.get((iy, ix), ()):
                    if dist_m(lat, lon, a, b) <= r_m:
                        yield a, b, it


# =============================================================================
# the road graph
# =============================================================================
class Way:
    __slots__ = ("id", "refs", "lat", "lon", "hw", "oneway", "name", "ref", "rb", "tags", "speed", "nosnap", "flat")


def oneway_of(tags):
    v = tags.get("oneway", "")
    if v in ("yes", "true", "1"):
        return 1
    if v in ("-1", "reverse"):
        return -1
    if v in ("no", "false", "0"):
        return 0
    if tags.get("junction") in ("roundabout", "circular"):
        return 1
    return 0


def car_forbidden(tags):
    for k in ("motorcar", "motor_vehicle", "vehicle", "access"):
        v = tags.get(k)
        if v is not None:
            return v in ("no", "private")
    return False


def parse_speed(v):
    """maxspeed -> km/h. Bare numbers in the US are almost always mph mapped without the unit."""
    if not v:
        return None
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(mph|km/h|kmh|kph)?\s*$", v.lower())
    if not m:
        return None
    x = float(m.group(1))
    return x if m.group(2) in ("km/h", "kmh", "kph") else x * 1.609344


class Leg:
    """One road piece touching node N, seen from N."""
    __slots__ = ("key", "piece", "alpha", "hin", "arr", "dep", "rank", "hw", "way", "name", "ref",
                 "rb", "oneway", "far", "node", "lat", "lon")


class Ctx:
    def __init__(self):
        self.ways = {}
        self.J = set()
        self.P_way = array("q")
        self.P_i0 = array("l")
        self.P_i1 = array("l")
        self.way_pieces = {}
        self.legs_at = defaultdict(list)   # node -> [leg key = piece*2 + end], ascending piece
        self.signals = Grid()
        self.jgrid = Grid(0.001)
        self._len = {}
        self._legs = {}

    def node_of_key(self, key):
        p = key >> 1
        w = self.ways[self.P_way[p]]
        return w.refs[self.P_i1[p] if key & 1 else self.P_i0[p]]

    def piece_len(self, p):
        if p in self._len:
            return self._len[p]
        w = self.ways[self.P_way[p]]
        s = 0.0
        for i in range(self.P_i0[p], self.P_i1[p]):
            s += dist_m(w.lat[i], w.lon[i], w.lat[i + 1], w.lon[i + 1])
        self._len[p] = s
        return s

    def leg(self, key):
        p, e = key >> 1, key & 1
        w = self.ways[self.P_way[p]]
        i0, i1 = self.P_i0[p], self.P_i1[p]
        if e == 0:
            i_from, i_to, step = i0, i1, 1
            arr, dep = w.oneway != 1, w.oneway != -1
        else:
            i_from, i_to, step = i1, i0, -1
            arr, dep = w.oneway != -1, w.oneway != 1
        lat0, lon0 = w.lat[i_from], w.lon[i_from]
        plat, plon = w.lat[i_to], w.lon[i_to]
        acc, i = 0.0, i_from
        while i != i_to:
            j = i + step
            d = dist_m(w.lat[i], w.lon[i], w.lat[j], w.lon[j])
            if acc + d >= PROBE_M and d > 0:
                f = (PROBE_M - acc) / d
                plat = w.lat[i] + f * (w.lat[j] - w.lat[i])
                plon = w.lon[i] + f * (w.lon[j] - w.lon[i])
                break
            acc += d
            i = j
        L = Leg()
        L.key, L.piece = key, p
        L.alpha = bearing(lat0, lon0, plat, plon)
        L.hin = (L.alpha + 180.0) % 360.0
        L.arr, L.dep = arr, dep
        L.rank = RANK.get(w.hw, 5)
        L.hw, L.way, L.name, L.ref, L.rb, L.oneway = w.hw, w.id, w.name, w.ref, w.rb, w.oneway
        L.node = w.refs[i_from]
        L.far = w.refs[i_to]
        L.lat, L.lon = lat0, lon0
        return L

    def legs(self, N):
        got = self._legs.get(N)
        if got is None:
            got = [self.leg(k) for k in self.legs_at.get(N, ())]
            if len(self._legs) < 100_000:
                self._legs[N] = got
        return got

    def leg_polyline(self, L, along=100.0):
        p = L.piece
        w = self.ways[self.P_way[p]]
        i0, i1 = self.P_i0[p], self.P_i1[p]
        idx = range(i0, i1 + 1) if (L.key & 1) == 0 else range(i1, i0 - 1, -1)
        pts, acc, prev = [], 0.0, None
        for i in idx:
            if prev is not None:
                acc += dist_m(w.lat[prev], w.lon[prev], w.lat[i], w.lon[i])
            pts.append((w.lat[i], w.lon[i]))
            prev = i
            if acc >= along:
                break
        return pts

    def signalized(self, lat, lon):
        for _ in self.signals.near(lat, lon, SIGNAL_R):
            return True
        return False


def read_signals(path, ctx, st):
    n = 0
    fp = osmium.FileProcessor(path, osmium.osm.NODE).with_filter(
        osmium.filter.TagFilter(("highway", "traffic_signals")))
    for node in fp:
        loc = node.location
        if loc.valid():
            ctx.signals.add(loc.lat, loc.lon, node.id)
            n += 1
    st.count("signals", n)


def read_graph(path, ctx, st):
    restrictions, chunks = [], []
    n_total = n_bad = n_forbidden = n_service = 0
    fp = (osmium.FileProcessor(path).with_locations()
          .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY | osmium.osm.RELATION)))
    t0 = time.time()
    for o in fp:
        if o.is_way():
            n_total += 1
            if n_total % 250_000 == 0:
                st.progress(f"read {n_total} ways, {len(ctx.ways)} car ways, {time.time()-t0:.0f}s")
            t = o.tags
            hw = t.get("highway")
            if hw == "service":
                n_service += 1
                continue
            if hw not in CAR_HW or t.get("area") == "yes":
                continue
            if car_forbidden(t):
                n_forbidden += 1
                continue
            refs, la, lo = array("q"), array("d"), array("d")
            ok = True
            for nr in o.nodes:
                loc = nr.location
                if not loc.valid():
                    ok = False
                    break
                refs.append(nr.ref)
                la.append(loc.lat)
                lo.append(loc.lon)
            if not ok or len(refs) < 2:
                n_bad += 1
                continue
            w = Way()
            w.id, w.refs, w.lat, w.lon, w.hw = o.id, refs, la, lo, hw
            w.tags = {k: t[k] for k in LANE_KEYS if k in t}
            w.oneway = oneway_of(t)
            w.name = t.get("name") or ""
            w.ref = t.get("ref") or ""
            w.rb = t.get("junction") in ("roundabout", "circular")
            ms = parse_speed(t.get("maxspeed"))
            w.speed = int(max(8, min(110, round(ms * 0.9 if ms else SPEED[hw]))))
            br = t.get("bridge", "no") not in ("no", "") or t.get("tunnel", "no") not in ("no", "")
            w.flat = br                                  # elevation interpolated end to end
            w.nosnap = hw in NOSNAP or br
            ctx.ways[o.id] = w
            closed = refs[0] == refs[-1]
            arr_np = np.frombuffer(refs, dtype=np.int64)
            chunks.append(arr_np[:-1] if closed else arr_np)
        elif o.is_relation():
            t = o.tags
            if t.get("type") != "restriction":
                continue
            restrictions.append((o.id, dict(t), [(m.type, m.ref, m.role) for m in o.members]))
    st.count("ways_in_file", n_total)
    st.count("car_ways", len(ctx.ways))
    st.count("service_ways_left_out", n_service)
    st.count("car_forbidden_ways", n_forbidden)
    st.count("ways_missing_nodes", n_bad)
    st.count("existing_restrictions", len(restrictions))
    st.check(len(ctx.ways) > 10_000, "> 10,000 car ways in the extract", len(ctx.ways))
    st.check(n_bad < 0.01 * max(1, len(ctx.ways)), "< 1% of car ways with missing node locations",
             f"{n_bad} — the extract was cut without --strategy smart")
    allrefs = np.concatenate(chunks)
    uniq, counts = np.unique(allrefs, return_counts=True)
    ctx.J = set(uniq[counts >= 2].tolist())
    st.count("junctions", len(ctx.J))
    return restrictions


def build_pieces(ctx, st):
    J = ctx.J
    loops = 0
    for wid, w in ctx.ways.items():
        refs = w.refs
        n = len(refs)
        cut = [0] + [i for i in range(1, n - 1) if refs[i] in J] + [n - 1]
        segs = []
        for a, b in zip(cut, cut[1:]):
            if refs[a] == refs[b]:
                if refs[a] in J and b - a >= 2:      # loop hanging off a junction: give it two ends
                    m = (a + b) // 2
                    segs += [(a, m), (m, b)]
                else:
                    loops += 1                       # isolated ring with no way in: unreachable
            else:
                segs.append((a, b))
        idxs = []
        for a, b in segs:
            p = len(ctx.P_way)
            ctx.P_way.append(wid)
            ctx.P_i0.append(a)
            ctx.P_i1.append(b)
            idxs.append(p)
            ctx.legs_at[refs[a]].append(p * 2)
            ctx.legs_at[refs[b]].append(p * 2 + 1)
        ctx.way_pieces[wid] = idxs
    for N in ctx.J:
        keys = ctx.legs_at.get(N)
        if not keys:
            continue
        k = keys[0]
        w = ctx.ways[ctx.P_way[k >> 1]]
        i = ctx.P_i1[k >> 1] if k & 1 else ctx.P_i0[k >> 1]
        ctx.jgrid.add(w.lat[i], w.lon[i], N)
    st.count("pieces", len(ctx.P_way))
    st.count("isolated_rings_dropped", loops)


# =============================================================================
# classification
# =============================================================================
def same_road(A, B):
    return A.way == B.way or (A.name and A.name == B.name) or (A.ref and A.ref == B.ref)


def leftish_moves(A, legs):
    """(all departures, leftward/U-like departures) from approach A."""
    deps = [(B, turn(A.hin, B.alpha)) for B in legs if B is not A and B.dep]
    if not deps:
        return deps, []
    s_leg, _ = min(deps, key=lambda t: abs(t[1]))
    out = []
    for B, d in deps:
        if 0.0 <= d < HAIRPIN:
            continue
        if -SLIGHT < d < 0.0 and B is s_leg:
            continue
        out.append((B, d))
    return deps, out


def median_crossing(ctx, A, B):
    """Short connector B leading into a one-way carriageway that flows against us."""
    if ctx.piece_len(B.piece) > MEDIAN_M or B.far not in ctx.J:
        return False
    for D in ctx.legs(B.far):
        if D.piece == B.piece or not D.arr or D.oneway == 0:
            continue
        if abs(turn(A.hin, D.hin)) >= 150.0:
            return True
    return False


def conflict_class(ctx, A, B, legs, signalized):
    p1 = A.alpha - EPS
    span = (B.alpha + EPS - p1) % 360.0

    def inside(q):
        c = (q - p1) % 360.0
        return 0.0 < c < span

    hit_on = hit_other = False
    for X in legs:
        if X is A or not X.arr:
            continue
        if not (signalized or X.rank >= A.rank):
            continue                                 # lower-class traffic yields to us
        qx = inside(X.alpha - EPS)
        x_on = abs(turn(A.hin, X.alpha)) <= ONCOMING
        for Y in legs:
            if Y is X or Y is B or not Y.dep:
                continue
            if qx == inside(Y.alpha + EPS):
                continue                             # chords don't cross
            if x_on and turn(X.hin, Y.alpha) > -SLIGHT:
                hit_on = True
                break
            hit_other = True
        if hit_on:
            break
    if not hit_on and median_crossing(ctx, A, B):
        hit_on = True
    if hit_on:
        return "permissive"
    if hit_other:
        return "geo" if signalized else "yield"
    return None


def classify_junction(ctx, legs, signalized, wl_app, wl_move, stats, blocked, protected):
    for A in legs:
        if not A.arr:
            continue
        deps, lefts = leftish_moves(A, legs)
        for B, d in lefts:
            stats["leftish"] += 1
            if A.hw in FREEWAY and B.hw in FREEWAY:
                stats["ex_freeway"] += 1
                continue
            if A.rb and B.rb:
                stats["ex_roundabout"] += 1
                continue
            if d > -CONTINUATION and same_road(A, B) and all(
                    abs(dc) > abs(d) - 5.0 for (C, dc) in deps if C is not B and C.rank >= B.rank):
                stats["ex_continuation"] += 1
                continue
            if A.key in wl_app or (A.key, B.key) in wl_move:
                stats["ex_whitelist"] += 1
                protected.append((A.key, B.key))
                continue
            k = conflict_class(ctx, A, B, legs, signalized)
            if k is None:
                stats["ex_no_conflict"] += 1
                continue
            stats["blocked_" + k] += 1
            blocked.append((A.key, B.key, k))


# =============================================================================
# whitelist sources
# =============================================================================
def approach_has_left(A, legs):
    return bool(leftish_moves(A, legs)[1])


def whitelist_keys(ctx, A, legs):
    """The approach that actually makes the left: A, or (divided road) the median connector after it."""
    if approach_has_left(A, legs):
        return [A.key]
    keys = []
    for B, d in leftish_moves(A, legs)[0]:
        if abs(d) >= SLIGHT or ctx.piece_len(B.piece) > MEDIAN_M or B.far not in ctx.J:
            continue
        far = ctx.legs(B.far)
        C = next((L for L in far if L.piece == B.piece), None)
        if C is not None and C.arr and approach_has_left(C, far):
            keys.append(C.key)
    return keys


def resolve_osm_lanes(ctx, st):
    out = []
    for wid, w in ctx.ways.items():
        t = w.tags
        cands = []
        if w.oneway == 1:
            v = t.get("turn:lanes") or t.get("turn:lanes:forward")
            if v:
                cands.append(("fwd", v))
        elif w.oneway == -1:
            v = t.get("turn:lanes") or t.get("turn:lanes:backward")
            if v:
                cands.append(("bwd", v))
        else:
            if t.get("turn:lanes:forward"):
                cands.append(("fwd", t["turn:lanes:forward"]))
            if t.get("turn:lanes:backward"):
                cands.append(("bwd", t["turn:lanes:backward"]))
        for direction, lanes in cands:
            nleft = sum(1 for lane in lanes.split("|")
                        if any(tok.strip() in LEFT_TOKENS for tok in lane.split(";")))
            if nleft < 2:
                continue
            pcs = ctx.way_pieces.get(wid)
            if not pcs:
                continue
            key = pcs[-1] * 2 + 1 if direction == "fwd" else pcs[0] * 2
            N = ctx.node_of_key(key)
            if N not in ctx.J:
                st.add("osm_dual_left_not_at_junction")
                continue
            legs = ctx.legs(N)
            A = next((L for L in legs if L.key == key), None)
            if A is None or not A.arr:
                continue
            if not ctx.signalized(A.lat, A.lon):
                st.add("osm_dual_left_unsignalized")
                continue
            for k in whitelist_keys(ctx, A, legs):
                out.append({"N": ctx.node_of_key(k), "key": k, "src": "osm", "why": f"turn:lanes {lanes}"})
    st.count("wl_osm_dual_left", len(out))
    return out


def best_approach(ctx, lat, lon, radius, max_d, heading=None):
    best = None
    for (_, _, N) in ctx.jgrid.near(lat, lon, radius):
        legs = ctx.legs(N)
        if not legs or not ctx.signalized(legs[0].lat, legs[0].lon):
            continue
        for A in legs:
            if not A.arr:
                continue
            if heading is not None and abs(turn(heading, A.hin)) > 40.0:
                continue
            keys = whitelist_keys(ctx, A, legs)
            if not keys:
                continue
            if heading is not None:
                d = dist_m(lat, lon, A.lat, A.lon)
            else:
                pts = ctx.leg_polyline(A)
                d = (min(seg_dist(lat, lon, a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:]))
                     if len(pts) > 1 else dist_m(lat, lon, pts[0][0], pts[0][1]))
            if best is None or d < best[0]:
                best = (d, N, A, keys)
    if best and best[0] <= max_d:
        return best
    return None


def resolve_ladot(ctx, signs, st):
    out, miss = [], 0
    for s in signs:
        hit = best_approach(ctx, s["lat"], s["lon"], radius=70.0, max_d=30.0)
        if hit is None:
            miss += 1
            continue
        d, N, A, keys = hit
        for k in keys:
            out.append({"N": ctx.node_of_key(k), "key": k, "src": "ladot",
                        "why": f"sign {s.get('stk')} {s.get('desc')}", "d_m": round(d, 1)})
    st.count("wl_ladot_signs_in", len(signs))
    st.count("wl_ladot_matched", len(out))
    st.count("wl_ladot_unmatched", miss)
    if signs and len(out) < 0.3 * len(signs):
        st.warn(f"only {len(out)} of {len(signs)} LADOT double-left signs matched an approach")
    return out


HEADINGS = {"NB": 0, "NORTHBOUND": 0, "NEB": 45, "EB": 90, "EASTBOUND": 90, "SEB": 135,
            "SB": 180, "SOUTHBOUND": 180, "SWB": 225, "WB": 270, "WESTBOUND": 270, "NWB": 315}


def read_hand(path, st):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, newline="") as f:
        lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    for i, r in enumerate(csv.DictReader(lines)):
        try:
            lat, lon = float(r["lat"]), float(r["lon"])
            a = (r.get("approach") or "").strip().upper()
            h = float(a) if a.replace(".", "", 1).isdigit() else HEADINGS[a]
            rows.append({"lat": lat, "lon": lon, "heading": h, "note": r.get("note", "")})
        except (KeyError, ValueError) as e:
            st.warn(f"whitelist.csv row {i + 2} unreadable ({e}): {dict(r)}")
    return rows


def resolve_hand(ctx, rows, st):
    out = []
    for r in rows:
        hit = best_approach(ctx, r["lat"], r["lon"], radius=45.0, max_d=45.0, heading=r["heading"])
        if hit is None:
            st.warn(f"whitelist.csv: no signalized approach heading {r['heading']:.0f}° with a left "
                    f"within 45 m of {r['lat']:.6f},{r['lon']:.6f} ({r['note']})")
            continue
        d, N, A, keys = hit
        for k in keys:
            out.append({"N": ctx.node_of_key(k), "key": k, "src": "hand", "why": r["note"], "d_m": round(d, 1)})
    st.count("wl_hand_rows", len(rows))
    st.count("wl_hand_matched", len(out))
    return out


def pocket_moves(ctx, entries):
    """A left pocket mapped as its own one-way piece: allow the slip into it too."""
    moves = set()
    for e in entries:
        A = next((L for L in ctx.legs(e["N"]) if L.key == e["key"]), None)
        if A is None or A.dep or A.oneway == 0 or ctx.piece_len(A.piece) > POCKET_M:
            continue
        P = A.far
        if P not in ctx.J:
            continue
        legs = ctx.legs(P)
        Ap = next((L for L in legs if L.piece == A.piece), None)
        if Ap is None or not Ap.dep:
            continue
        for U in legs:
            if U is Ap or not U.arr or U.rank < 4:
                continue
            if A.name and U.name and U.name != A.name:
                continue
            if any(B is Ap for B, _ in leftish_moves(U, legs)[1]):
                moves.add((U.key, Ap.key))
    return moves


# =============================================================================
# real-world OSM turn restrictions -> rules on leg keys
# =============================================================================
def _fits(kind, d):
    if kind == "left_turn":
        return d <= -SLIGHT
    if kind == "right_turn":
        return d >= SLIGHT
    if kind == "straight_on":
        return abs(d) < 45.0
    if kind == "u_turn":
        return abs(d) >= 135.0
    return False


def osm_rules(ctx, restrictions, st):
    rules, s = [], defaultdict(int)
    for rid, tags, members in restrictions:
        val = (tags.get("restriction") or tags.get("restriction:motorcar") or tags.get("restriction:motor_vehicle")
               or tags.get("restriction:vehicle"))
        if not val:
            s["not_for_cars"] += 1
            continue
        if any(x.strip() in ("motorcar", "motor_vehicle", "vehicle") for x in tags.get("except", "").split(";")):
            s["cars_excepted"] += 1
            continue
        if val.endswith("_on_red") or not (val.startswith("no_") or val.startswith("only_")):
            s["ignored_value"] += 1
            continue
        kind = "no" if val.startswith("no_") else "only"
        shape = val.split("_", 1)[1]
        vias_n = [m[1] for m in members if m[0] == "n" and m[2] == "via"]
        if len(vias_n) != 1 or any(m[0] == "w" and m[2] == "via" for m in members):
            s["via_way_skipped"] += 1
            continue
        V = vias_n[0]
        if V not in ctx.legs_at:
            s["via_not_in_graph"] += 1
            continue
        legs = ctx.legs(V)
        froms = [m[1] for m in members if m[0] == "w" and m[2] == "from"]
        tos = [m[1] for m in members if m[0] == "w" and m[2] == "to"]
        if not froms or not tos or (kind == "only" and (len(froms) > 1 or len(tos) > 1)):
            s["malformed"] += 1
            continue
        for fw in froms:
            for tw in tos:
                As = [L for L in legs if L.way == fw and L.arr]
                Bs = [L for L in legs if L.way == tw and L.dep]
                pairs = [(A, B) for A in As for B in Bs if A is not B]
                if len(pairs) > 1:
                    pairs = [(A, B) for A, B in pairs if _fits(shape, turn(A.hin, B.alpha))]
                if len(pairs) != 1:
                    s["unresolved"] += 1
                    continue
                A, B = pairs[0]
                rules.append((A.key, B.key, kind))
                s["applied_" + kind] += 1
    for k, v in s.items():
        st.count("osm_restr_" + k, v)
    return rules


# =============================================================================
# turn masks, islands, compaction
# =============================================================================
def build_masks(ctx, blocked, protected, rules, st):
    P = len(ctx.P_way)
    car = np.zeros(2 * P, dtype=np.uint16)
    legpos = np.full(2 * P, -1, dtype=np.int32)
    ways, Pw = ctx.ways, ctx.P_way

    def ow(k):
        return ways[Pw[k >> 1]].oneway

    def arr_ok(k):
        return ow(k) != 1 if (k & 1) == 0 else ow(k) != -1

    def dep_ok(k):
        return ow(k) != -1 if (k & 1) == 0 else ow(k) != 1

    wide = dead = 0
    for N, keys in ctx.legs_at.items():
        n = len(keys)
        for i, k in enumerate(keys):
            legpos[k] = i
        if n == 1:                                    # dead end: turning around is the only move
            k = keys[0]
            if arr_ok(k) and dep_ok(k):
                car[k ^ 1] = 1
            dead += 1
            continue
        if n > 16:
            wide += 1
        bits = 0
        for i, k in enumerate(keys[:16]):
            if dep_ok(k):
                bits |= 1 << i
        for i, k in enumerate(keys):
            if arr_ok(k):
                car[k ^ 1] = (bits & ~(1 << i)) & 0xFFFF if i < 16 else bits
    for akey, bkey, kind in rules:
        i = int(legpos[bkey])
        if 0 <= i < 16:
            a = akey ^ 1
            car[a] = int(car[a]) & (0xFFFF ^ (1 << i)) if kind == "no" else int(car[a]) & (1 << i)
    arrow = car.copy()
    wl = np.zeros_like(car)
    for akey, bkey, _ in blocked:
        i = int(legpos[bkey])
        if 0 <= i < 16:
            a = akey ^ 1
            arrow[a] = int(arrow[a]) & (0xFFFF ^ (1 << i))
    for akey, bkey in protected:
        i = int(legpos[bkey])
        if 0 <= i < 16:
            a = akey ^ 1
            wl[a] = int(wl[a]) | (1 << i)
    st.count("dead_ends", dead)
    st.count("nodes_over_16_roads", wide)
    st.count("arrow_moves_open", int(np.unpackbits(arrow.view(np.uint8)).sum()))
    st.count("car_moves_open", int(np.unpackbits(car.view(np.uint8)).sum()))
    return car, arrow, wl


def compact(ctx, st):
    """Drop road islands, give kept pieces and nodes dense ids (piece order preserved)."""
    P = len(ctx.P_way)
    U = np.empty(P, dtype=np.int64)
    V = np.empty(P, dtype=np.int64)
    for p in range(P):
        w = ctx.ways[ctx.P_way[p]]
        U[p] = w.refs[ctx.P_i0[p]]
        V[p] = w.refs[ctx.P_i1[p]]
    nodes, inv = np.unique(np.concatenate([U, V]), return_inverse=True)
    u, v = inv[:P], inv[P:]
    parent = np.arange(len(nodes))

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    for p in range(P):
        a, b = find(u[p]), find(v[p])
        if a != b:
            parent[a] = b
    roots = np.array([find(x) for x in u])
    rid, rcount = np.unique(roots, return_counts=True)
    big = set(rid[rcount >= MIN_COMPONENT].tolist())
    keep = np.array([r in big for r in roots])
    st.count("islands_dropped", int(len(rid) - len(big)))
    st.count("edges_in_islands", int((~keep).sum()))
    st.count("largest_component_edges", int(rcount.max()))
    kept_nodes = np.unique(np.concatenate([u[keep], v[keep]]))
    newnode = np.full(len(nodes), -1, dtype=np.int64)
    newnode[kept_nodes] = np.arange(len(kept_nodes))
    return keep, newnode[u], newnode[v], nodes[kept_nodes]


# =============================================================================
# elevation
# =============================================================================
class DEM:
    """2x2 skadi tiles stitched: rows from 35°N down, columns from 119°W east, 3600 per degree."""

    def __init__(self, cache, st):
        self.a = np.zeros((7201, 7201), dtype=np.float32)
        for lat in (34, 33):
            for lon in (119, 118):
                url = SKADI.format(lat=lat, lon=lon)
                path = os.path.join(cache, os.path.basename(url))
                if not os.path.exists(path):
                    body = http(url, st, f"tile_N{lat}W{lon}", timeout=180)
                    with open(path, "wb") as f:
                        f.write(body)
                with gzip.open(path, "rb") as f:
                    raw = f.read()
                st.check(len(raw) == 3601 * 3601 * 2, f"a 1 arc-second HGT tile ({3601*3601*2} bytes)",
                         f"{len(raw)} bytes in {os.path.basename(url)}")
                t = np.frombuffer(raw, dtype=">i2").reshape(3601, 3601).astype(np.float32)
                voids = int((t == -32768).sum())
                if voids:
                    st.add("dem_voids", voids)
                    t[t == -32768] = np.nan
                ro, co = (34 - lat) * 3600, (119 - lon) * 3600
                self.a[ro:ro + 3601, co:co + 3601] = t
        for _ in range(8):                            # fill voids from neighbours
            m = np.isnan(self.a)
            if not m.any():
                break
            pad = np.pad(self.a, 1, mode="edge")
            neigh = np.stack([pad[:-2, 1:-1], pad[2:, 1:-1], pad[1:-1, :-2], pad[1:-1, 2:]])
            self.a[m] = np.nanmean(neigh, axis=0)[m]
        self.a[np.isnan(self.a)] = 0.0
        st.count("dem_min_m", float(np.min(self.a)))
        st.count("dem_max_m", float(np.max(self.a)))

    def at(self, lat, lon):
        r = np.clip((35.0 - lat) * 3600.0, 0, 7199.999)
        c = np.clip((lon + 119.0) * 3600.0, 0, 7199.999)
        r0, c0 = r.astype(np.int64), c.astype(np.int64)
        fr, fc = r - r0, c - c0
        a = self.a
        return ((a[r0, c0] * (1 - fc) + a[r0, c0 + 1] * fc) * (1 - fr) +
                (a[r0 + 1, c0] * (1 - fc) + a[r0 + 1, c0 + 1] * fc) * fr)


def grades(dem, lat, lon, flat_ends):
    """(avg % forward, max climb % forward, max climb % backward) for one edge's geometry."""
    lat, lon = np.asarray(lat, dtype=np.float64), np.asarray(lon, dtype=np.float64)
    k = math.cos(math.radians(float(lat[0])))
    seg = np.hypot(np.diff(lon) * M_LON0 * k, np.diff(lat) * M_LAT)
    L = float(seg.sum())
    if L < 1.0:
        return 0, 0, 0
    if L < 40.0:                                    # short edge: read the slope over 40 m more of street
        dlat, dlon = lat[-1] - lat[0], lon[-1] - lon[0]
        ext = 20.0 / L
        za, zb = dem.at(np.array([lat[0] - dlat * ext]), np.array([lon[0] - dlon * ext]))[0], \
                 dem.at(np.array([lat[-1] + dlat * ext]), np.array([lon[-1] + dlon * ext]))[0]
        if flat_ends:
            za, zb = dem.at(lat[:1], lon[:1])[0], dem.at(lat[-1:], lon[-1:])[0]
            g = (zb - za) / L * 100.0
        else:
            g = (zb - za) / (L + 40.0) * 100.0
        g = float(np.clip(g, -40, 40))
        return int(round(g)), int(round(max(g, 0))), int(round(max(-g, 0)))
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    s = np.arange(0.0, L, 10.0)
    s = np.append(s, L)
    slat, slon = np.interp(s, cum, lat), np.interp(s, cum, lon)
    z = dem.at(slat, slon).astype(np.float64)
    if flat_ends:                                   # bridge/tunnel: straight line between the ends
        z = np.interp(s, [0.0, L], [z[0], z[-1]])
    elif len(z) >= 5:
        kern = np.ones(9) / 9.0                     # ~90 m moving average
        zp = np.pad(z, 4, mode="edge")
        z = np.convolve(zp, kern, mode="valid")
    avg = (z[-1] - z[0]) / L * 100.0
    w = 4                                           # 40 m windows
    if len(z) > w:
        dz = (z[w:] - z[:-w]) / (s[w:] - s[:-w]) * 100.0
        up_f, up_b = float(max(dz.max(), 0.0)), float(max((-dz).max(), 0.0))
    else:
        up_f, up_b = max(avg, 0.0), max(-avg, 0.0)
    clip = lambda x: int(round(float(np.clip(x, -40, 40))))
    return clip(avg), clip(up_f), clip(up_b)


# =============================================================================
# geometry for the phone
# =============================================================================
def simplify(pts, tol):
    """Douglas–Peucker on [(lat, lon)], endpoints kept."""
    if len(pts) <= 2:
        return pts
    k = math.cos(math.radians(pts[0][0]))
    xy = [(p[1] * M_LON0 * k, p[0] * M_LAT) for p in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy) or 1e-9
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            d = abs((xy[i][0] - ax) * dy - (xy[i][1] - ay) * dx) / L
            if d > best:
                best, bi = d, i
        if best > tol:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k2 in zip(pts, keep) if k2]


def encode_interior(u_lat6, u_lon6, interior):
    """1e-5° int16 deltas from the previous point, starting at u (1e-6° ints). Long gaps get split."""
    out = []
    pla, plo = u_lat6, u_lon6
    for lat, lon in interior:
        tla, tlo = int(round(lat * 1e6)), int(round(lon * 1e6))
        while True:
            dla, dlo = round((tla - pla) / 10), round((tlo - plo) / 10)
            steps = max(1, math.ceil(max(abs(dla), abs(dlo)) / 32000))
            sla, slo = round(dla / steps), round(dlo / steps)
            out.append((sla, slo))
            pla += sla * 10
            plo += slo * 10
            if steps == 1:
                break
    return out


# =============================================================================
# binary file
# =============================================================================
SECTIONS = [("lat", "<i4"), ("lon", "<i4"), ("nflag", "u1"), ("eu", "<u4"), ("ev", "<u4"), ("elen", "<u2"),
            ("espd", "u1"), ("eflag", "u1"), ("egrade", "i1"), ("gstart", "<u4"), ("geom", "<i2"),
            ("mcar", "<u2"), ("marr", "<u2"), ("mwl", "<u2")]


def write_graph(path, arrs, n_nodes, n_edges, n_geom):
    header = bytearray(192)
    off = 192
    body = []
    for i, (name, dt) in enumerate(SECTIONS):
        b = np.ascontiguousarray(arrs[name].astype(dt)).tobytes()
        pad = (-off) % 4
        if pad:
            body.append(b"\0" * pad)
            off += pad
        header[64 + i * 8: 64 + i * 8 + 8] = np.array([off, len(b)], dtype="<u4").tobytes()
        body.append(b)
        off += len(b)
    w, s, e, n = BBOX
    head = (np.array([MAGIC, FORMAT, n_nodes, n_edges, n_geom], dtype="<u4").tobytes() +
            np.array([round(s * 1e6), round(w * 1e6), round(n * 1e6), round(e * 1e6)], dtype="<i4").tobytes() +
            np.array([len(SECTIONS)], dtype="<u4").tobytes())
    assert len(head) == 40
    header[0:40] = head                              # same length: the section table stays at byte 64
    assert len(header) == 192
    raw = bytes(header) + b"".join(body)
    with open(path, "wb") as f:
        f.write(raw)
    return raw


def read_graph_bin(raw):
    """Mirror of the app's parser — the probe routes on what the phone will read."""
    hdr = np.frombuffer(raw[:20], dtype="<u4")
    if int(hdr[0]) != MAGIC or int(hdr[1]) != FORMAT:
        raise ValueError(f"bad header {hdr[:2]}")
    N, E, G = int(hdr[2]), int(hdr[3]), int(hdr[4])
    want = {"lat": N, "lon": N, "nflag": N, "eu": E, "ev": E, "elen": E, "espd": E, "eflag": E,
            "egrade": 3 * E, "gstart": E + 1, "geom": 2 * G, "mcar": 2 * E, "marr": 2 * E, "mwl": 2 * E}
    out = {"N": N, "E": E, "G": G}
    for i, (name, dt) in enumerate(SECTIONS):
        off, ln = np.frombuffer(raw[64 + i * 8: 72 + i * 8], dtype="<u4")
        a = np.frombuffer(raw, dtype=dt, count=int(ln) // np.dtype(dt).itemsize, offset=int(off))
        if len(a) != want[name]:
            raise ValueError(f"section {name}: {len(a)} items, expected {want[name]}")
        out[name] = a
    return out


# =============================================================================
# probe: the app's router, in Python, on the written file
# =============================================================================
class Router:
    def __init__(self, g):
        self.g = g
        N, E = g["N"], g["E"]
        self.lat = (g["lat"] / 1e6).tolist()
        self.lon = (g["lon"] / 1e6).tolist()
        eu, ev = g["eu"].astype(np.int64), g["ev"].astype(np.int64)
        self.eu, self.ev = eu.tolist(), ev.tolist()
        node = np.concatenate([eu, ev])
        de = np.concatenate([np.arange(E) * 2, np.arange(E) * 2 + 1])
        order = np.lexsort((de >> 1, node))
        self.adj = de[order].tolist()
        self.astart = np.concatenate([[0], np.cumsum(np.bincount(node, minlength=N))]).tolist()
        speed = np.maximum(g["espd"].astype(np.float64), 5.0) / 3.6
        t = np.maximum(g["elen"].astype(np.float64), 1.0) / speed
        self.t = np.repeat(t, 2)
        gr = g["egrade"].astype(np.int64).reshape(-1, 3)
        avg = np.stack([gr[:, 0], -gr[:, 0]], axis=1).reshape(-1)
        up = np.stack([gr[:, 1], gr[:, 2]], axis=1).reshape(-1)
        fac = np.where(up >= 15, 50.0, np.where(avg >= 10, 20.0, np.where(avg >= 7, 1 / 0.3, np.where(avg >= 5, 1.25, 1.0))))
        self.wflat = (self.t * fac).tolist()
        self.t = self.t.tolist()
        self.up = up.tolist()
        fl = g["eflag"]
        self.fwd = ((fl & 1) > 0).tolist()
        self.bwd = ((fl & 2) > 0).tolist()
        self.nosnap = ((fl & 8) > 0)
        self.sig = (g["nflag"] & 1).tolist()
        self.elen = g["elen"].tolist()
        self.masks = {"car": g["mcar"].tolist(), "arrow": g["marr"].tolist(), "arrow_flat": g["marr"].tolist()}
        self.mwl = g["mwl"].tolist()
        # headings per directed edge from the geometry (first/last segment), as the app does
        gs, geom = g["gstart"].tolist(), g["geom"].astype(np.int64).tolist()
        la6, lo6 = g["lat"].tolist(), g["lon"].tolist()
        self.dep, self.arrh = [0.0] * (2 * E), [0.0] * (2 * E)
        for e in range(E):
            ua, uo, va, vo = la6[self.eu[e]], lo6[self.eu[e]], la6[self.ev[e]], lo6[self.ev[e]]
            j0, j1 = gs[e], gs[e + 1]
            if j1 > j0:
                fa, fo = ua + geom[2 * j0] * 10, uo + geom[2 * j0 + 1] * 10
                la, lo = ua, uo
                for j in range(j0, j1):
                    la += geom[2 * j] * 10
                    lo += geom[2 * j + 1] * 10
            else:
                fa, fo, la, lo = va, vo, ua, uo
            self.dep[2 * e] = bearing(ua / 1e6, uo / 1e6, fa / 1e6, fo / 1e6)
            self.arrh[2 * e] = bearing(la / 1e6, lo / 1e6, va / 1e6, vo / 1e6)
            self.dep[2 * e + 1] = bearing(va / 1e6, vo / 1e6, la / 1e6, lo / 1e6)
            self.arrh[2 * e + 1] = bearing(fa / 1e6, fo / 1e6, ua / 1e6, uo / 1e6)
        snap_ok = np.zeros(N, dtype=bool)
        good = ~self.nosnap
        snap_ok[eu[good]] = True
        snap_ok[ev[good]] = True
        self.snap_ok = snap_ok
        self.latn, self.lonn = g["lat"] / 1e6, g["lon"] / 1e6

    def head(self, de):
        return self.eu[de >> 1] if de & 1 else self.ev[de >> 1]

    def nearest(self, lat, lon):
        k = math.cos(math.radians(lat))
        d = ((self.latn - lat) * M_LAT) ** 2 + ((self.lonn - lon) * M_LON0 * k) ** 2
        d[~self.snap_ok] = np.inf
        return int(np.argmin(d))

    def turn_cost(self, a, b, n):
        c = TURN["signal"] if self.sig[n] else 0.0
        if (a >> 1) == (b >> 1):
            return c + TURN["uturn"]
        d = turn(self.arrh[a], self.dep[b])
        if d > 35:
            c += TURN["right"]
        elif d < -35:
            c += TURN["left"]
        return c

    def route(self, frm, to, profile):
        src, dst = self.nearest(*frm), self.nearest(*to)
        mask = self.masks[profile]
        w = self.wflat if profile == "arrow_flat" else self.t
        tl, tn = self.lat[dst], self.lon[dst]
        k = math.cos(math.radians(tl))
        h = lambda n: math.hypot((self.lat[n] - tl) * M_LAT, (self.lon[n] - tn) * M_LON0 * k) * 0.98 / VMAX
        g, par, heap = {}, {}, []
        for i in range(self.astart[src], self.astart[src + 1]):
            de = self.adj[i]
            e = de >> 1
            if not (self.bwd[e] if de & 1 else self.fwd[e]):
                continue
            g[de], par[de] = w[de], -1
            heapq.heappush(heap, (w[de] + h(self.head(de)), de))
        best, last, pops = math.inf, -1, 0
        while heap:
            f, de = heapq.heappop(heap)
            if f >= best:
                break
            n = self.head(de)
            gc = g[de]
            if f > gc + h(n) + 1e-6:
                continue
            pops += 1
            if n == dst and gc < best:
                best, last = gc, de
                continue
            m = mask[de]
            a0, a1 = self.astart[n], self.astart[n + 1]
            for i in range(min(a1 - a0, 16)):
                if not (m >> i) & 1:
                    continue
                b = self.adj[a0 + i]
                ng = gc + self.turn_cost(de, b, n) + w[b]
                if ng < g.get(b, math.inf):
                    g[b], par[b] = ng, de
                    heapq.heappush(heap, (ng + h(self.head(b)), b))
        if last < 0:
            return None
        des = []
        x = last
        while x != -1:
            des.append(x)
            x = par[x]
        des.reverse()
        wl = 0
        for a, b in zip(des, des[1:]):
            n = self.head(a)
            pos = self.adj[self.astart[n]:self.astart[n + 1]].index(b)
            if (self.mwl[a] >> pos) & 1:
                wl += 1
        secs = sum(self.t[d] for d in des) + sum(self.turn_cost(a, b, self.head(a)) for a, b in zip(des, des[1:]))
        climb = max((self.up[d] for d in des if self.elen[d >> 1] >= 40), default=0)
        return {"min": round(secs / 60, 1), "km": round(sum(self.elen[d >> 1] for d in des) / 1000, 2),
                "cost": round(best, 1), "protected_lefts": wl, "max_climb": climb, "edges": len(des),
                "pops": pops, "des": des}


# =============================================================================
# self-test — runs every build; a flipped sign fails here first
# =============================================================================
def selftest():
    ctx = Ctx()
    c = (34.0, -118.0)
    dlat, dlon = 100.0 / M_LAT, 100.0 / (M_LON0 * math.cos(math.radians(34.0)))
    arms = {"N": (c[0] + dlat, c[1]), "E": (c[0], c[1] + dlon), "S": (c[0] - dlat, c[1]), "W": (c[0], c[1] - dlon)}

    def expect(cond, what, got):
        if not cond:
            raise diag.StageFailure("selftest", what, got)

    def build(spec, signal):
        ctx.ways.clear()
        ctx.legs_at.clear()
        ctx._legs.clear()
        ctx._len.clear()
        ctx.P_way, ctx.P_i0, ctx.P_i1 = array("q"), array("l"), array("l")
        ctx.signals = Grid()
        if signal:
            ctx.signals.add(c[0], c[1], 1)
        for i, (arm, (hw, ow, name)) in enumerate(spec.items(), start=1):
            w = Way()
            w.id, w.hw, w.oneway, w.name, w.ref, w.rb, w.tags = i, hw, ow, name, "", False, {}
            w.refs = array("q", [1000, 2000 + i])
            w.lat = array("d", [c[0], arms[arm][0]])
            w.lon = array("d", [c[1], arms[arm][1]])
            ctx.ways[i] = w
            ctx.P_way.append(i)
            ctx.P_i0.append(0)
            ctx.P_i1.append(1)
            ctx.legs_at[1000].append((i - 1) * 2)
        ctx.J = {1000}
        legs = ctx.legs(1000)
        name_of = {L.key: arm for arm, L in zip(spec.keys(), legs)}
        stats, blocked, prot = defaultdict(int), [], []
        classify_junction(ctx, legs, ctx.signalized(c[0], c[1]), set(), set(), stats, blocked, prot)
        return {(name_of[a], name_of[b]): k for a, b, k in blocked}

    t = bearing(c[0], c[1], *arms["N"]), bearing(c[0], c[1], *arms["E"])
    expect(abs(t[0]) < 1 or abs(t[0] - 360) < 1, "north arm bearing ≈ 0°", t[0])
    expect(abs(t[1] - 90) < 1, "east arm bearing ≈ 90°", t[1])
    expect(turn(0, 270) < 0 < turn(0, 90), "turn(N→W) < 0 < turn(N→E)", (turn(0, 270), turn(0, 90)))
    g = build({"N": ("residential", 0, "a"), "E": ("residential", 0, "b"),
               "S": ("residential", 0, "a"), "W": ("residential", 0, "b")}, signal=False)
    expect(g.get(("S", "W")) == "permissive", "S→W (left across oncoming) blocked as permissive", g)
    expect(("S", "E") not in g and ("S", "N") not in g, "S→E (right) and S→N (straight) open", g)
    expect(len(g) == 4, "exactly 4 blocked lefts at a plain 4-way", g)
    g = build({"N": ("secondary", 1, "a"), "E": ("secondary", 0, "b"),
               "S": ("secondary", -1, "a"), "W": ("secondary", 0, "b")}, signal=True)
    expect(g.get(("S", "W")) == "geo", "one-way S→W at a signal = geo", g)
    expect(("S", "E") not in g, "S→E right open", g)
    g = build({"N": ("residential", 0, "minor"), "W": ("primary", 0, "Main"),
               "S": ("primary", 0, "Main"), "E": ("residential", 0, "minor2")}, signal=False)
    expect(("S", "W") not in g, "S→W along the main road (a bend) open", g)
    return "ok"


# =============================================================================
# stages
# =============================================================================
def stage_fetch(work):
    dest = os.path.join(work, "socal-latest.osm.pbf")
    with diag.Stage("fetch_osm", inputs={"url": OSM_URL}) as st:
        want = http(OSM_URL + ".md5", st, "md5_response").decode("utf-8", "replace").split()
        st.check(want and re.fullmatch(r"[0-9a-f]{32}", want[0]), "a 32-hex md5 from Geofabrik", want[:3])
        got, n = download(OSM_URL, dest, st)
        st.check(got == want[0], f"md5 {want[0]}", got)
        st.check(n > 100_000_000, "a SoCal extract over 100 MB", f"{n} bytes")
        st.count("bytes", n)
        st.keep("md5", got)
    return dest


def stage_extract(src, work):
    clip, roads = os.path.join(work, "la.osm.pbf"), os.path.join(work, "la-roads.osm.pbf")
    bbox = ",".join(str(x) for x in BBOX)
    with diag.Stage("extract", inputs={"src": src, "bbox": bbox}) as st:
        sh(["osmium", "extract", "-b", bbox, "--strategy", "smart", "--overwrite", "-o", clip, src], st, "extract")
        sh(["osmium", "tags-filter", "--overwrite", "-o", roads, clip, "w/highway", "r/type=restriction"], st, "tags_filter")
        out = sh(["osmium", "fileinfo", "-e", "-j", roads], st, "fileinfo")
        try:
            data = json.loads(out).get("data", {})
        except json.JSONDecodeError as e:
            raise diag.StageFailure(st.name, "JSON from osmium fileinfo -j", f"{e}: {out[:200]}")
        count = data.get("count", {})
        for k in ("nodes", "ways", "relations"):
            st.count(k, count.get(k, 0))
        ts = (data.get("timestamp") or {}).get("last")
        st.keep("osm_timestamp", ts)
        st.check(count.get("ways", 0) > 50_000, "over 50,000 highway ways in the LA clip", count.get("ways"))
        os.remove(clip)
    return roads, ts


def stage_ladot(work, baseline_dir):
    out = os.path.join(work, "ladot_dual_left.json")
    keep = os.path.join(baseline_dir, "ladot_dual_left.json")
    where = "STKNUMBER IN (%s)" % ",".join(f"'{s}'" for s in LADOT_STK)
    with diag.Stage("ladot", inputs={"url": LADOT_URL, "where": where}, degradable=True) as st:
        q = {"where": where, "outFields": "OBJECTID,STKNUMBER,DESCRIPTIO", "returnGeometry": "true",
             "outSR": "4326", "f": "json", "resultOffset": 0, "resultRecordCount": 1000}
        feats = []
        for _ in range(20):
            body = http(LADOT_URL + "?" + urllib.parse.urlencode(q), st, "ladot_response")
            try:
                j = json.loads(body)
            except json.JSONDecodeError as e:
                raise diag.StageFailure(st.name, "JSON from the LADOT MapServer", f"{e}: {body[:200]!r}")
            st.check(isinstance(j, dict) and isinstance(j.get("features"), list),
                     "ArcGIS JSON with a features list", j.get("error") if isinstance(j, dict) else type(j))
            feats += j["features"]
            if not j.get("exceededTransferLimit"):
                break
            q["resultOffset"] += len(j["features"])
        signs = []
        for f in feats:
            g, a = f.get("geometry") or {}, f.get("attributes") or {}
            if isinstance(g.get("x"), (int, float)) and isinstance(g.get("y"), (int, float)):
                signs.append({"id": a.get("OBJECTID"), "stk": a.get("STKNUMBER"), "desc": a.get("DESCRIPTIO"),
                              "lat": g["y"], "lon": g["x"]})
        st.count("signs", len(signs))
        st.check(len(signs) >= 50, "at least 50 double-left lane signs (243 on 2026-10-28)", len(signs))
        bad = [s for s in signs if not (33.3 < s["lat"] < 34.9 and -119.0 < s["lon"] < -117.6)]
        st.check(not bad, "every sign inside LA in lat/lon (outSR=4326 honored)", bad[:3])
        with open(out, "w") as fh:
            json.dump(signs, fh)
        os.makedirs(baseline_dir, exist_ok=True)
        with open(keep, "w") as fh:
            json.dump(signs, fh)
    if not st.ok:
        if os.path.exists(keep):
            with open(keep) as fh, open(out, "w") as oh:
                oh.write(fh.read())
            diag.emit("ladot", "FALLBACK", {"using_last_good": keep}, diag.AMBER)
        else:
            with open(out, "w") as fh:
                json.dump([], fh)
            diag.emit("ladot", "FALLBACK", "no last-good copy; building without LADOT signs", diag.AMBER)
    with open(out) as fh:
        return json.load(fh)


def stage_graph(roads, signs, hand_csv):
    with diag.Stage("graph", inputs={"roads": roads, "signs": len(signs), "hand": hand_csv}) as st:
        st.count("selftest", selftest())
        ctx = Ctx()
        t0 = time.time()
        read_signals(roads, ctx, st)
        restrictions = read_graph(roads, ctx, st)
        st.progress(f"read in {time.time()-t0:.0f}s")
        build_pieces(ctx, st)
        entries = resolve_osm_lanes(ctx, st) + resolve_ladot(ctx, signs, st) + resolve_hand(ctx, read_hand(hand_csv, st), st)
        wl_app = {e["key"] for e in entries}
        wl_move = pocket_moves(ctx, entries)
        st.count("wl_approaches", len(wl_app))
        st.count("wl_pocket_moves", len(wl_move))
        ctx._legs.clear()
        stats, blocked, protected = defaultdict(int), [], []
        sig_nodes = set()
        t1, n = time.time(), 0
        for N, keys in ctx.legs_at.items():
            if len(keys) < 3:
                continue
            legs = [ctx.leg(k) for k in keys]
            sig = ctx.signalized(legs[0].lat, legs[0].lon)
            if sig:
                sig_nodes.add(N)
            classify_junction(ctx, legs, sig, wl_app, wl_move, stats, blocked, protected)
            n += 1
            if n % 100_000 == 0:
                st.progress(f"classified {n} junctions, {len(blocked)} blocked, {time.time()-t1:.0f}s")
        for k, v in sorted(stats.items()):
            st.count(k, v)
        ratio = len(blocked) / max(1, stats["leftish"])
        st.count("blocked_share_of_leftish", round(ratio, 4))
        st.check(0.4 <= ratio <= 0.995, "40–99.5% of leftward movements blocked (a flipped sign lands near 0)",
                 f"{ratio:.3f} ({len(blocked)} of {stats['leftish']})")
        rules = osm_rules(ctx, restrictions, st)
        car, arrow, wl = build_masks(ctx, blocked, protected, rules, st)
        keep, u, v, osm_nodes = compact(ctx, st)
        st.check(keep.sum() > 0.8 * len(keep), "over 80% of road pieces in the main network",
                 f"{int(keep.sum())} of {len(keep)}")
        feats = []
        for e in entries:
            L = ctx.leg(e["key"])
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(L.lon, 6), round(L.lat, 6)]},
                          "properties": {"src": e["src"], "why": e["why"], "approach_heading": round(L.hin), "street": L.name}})
        sample = []
        for akey, bkey, k in blocked[:: max(1, len(blocked) // 50)][:50]:
            A = ctx.leg(akey)
            sample.append({"lat": round(A.lat, 6), "lon": round(A.lon, 6), "from": A.name,
                           "to": ctx.ways[ctx.P_way[bkey >> 1]].name, "class": k})
        st.keep("blocked_sample", sample)
        return ctx, car, arrow, wl, keep, u, v, osm_nodes, sig_nodes, feats, sample, dict(st.counts)


def stage_elevation(ctx, keep, cache):
    with diag.Stage("elevation", inputs={"tiles": "N33-34 W118-119 skadi"}) as st:
        dem = DEM(cache, st)
        P = len(ctx.P_way)
        gr = np.zeros((P, 3), dtype=np.int8)
        t0 = time.time()
        for p in range(P):
            if not keep[p]:
                continue
            w = ctx.ways[ctx.P_way[p]]
            i0, i1 = ctx.P_i0[p], ctx.P_i1[p]
            gr[p] = grades(dem, w.lat[i0:i1 + 1], w.lon[i0:i1 + 1], w.flat)
            if p % 100_000 == 0 and p:
                st.progress(f"{p} of {P} edges, {time.time()-t0:.0f}s")
        kept = gr[keep]
        st.count("edges_over_10pct_climb", int(((kept[:, 1] >= 10) | (kept[:, 2] >= 10)).sum()))
        st.count("edges_over_15pct_climb", int(((kept[:, 1] >= 15) | (kept[:, 2] >= 15)).sum()))
        st.check(int(np.abs(kept[:, 0]).max()) <= 40, "average grades within ±40%", int(np.abs(kept[:, 0]).max()))
        st.check(((kept[:, 1] >= 15) | (kept[:, 2] >= 15)).mean() < 0.15,
                 "under 15% of edges with a 15%+ climb (more = a misread DEM)",
                 f"{((kept[:, 1] >= 15) | (kept[:, 2] >= 15)).mean():.3f}")
        return gr


def stage_write(ctx, car, arrow, wl, keep, u, v, osm_nodes, sig_nodes, gr, data_dir):
    raw_path = os.path.join(data_dir, "la-graph.bin")
    with diag.Stage("write", inputs={"dir": data_dir}) as st:
        kept = np.flatnonzero(keep)
        E = len(kept)
        node_lat = np.zeros(len(osm_nodes), dtype=np.float64)
        node_lon = np.zeros(len(osm_nodes), dtype=np.float64)
        nflag = np.zeros(len(osm_nodes), dtype=np.uint8)
        idx_of = {int(x): i for i, x in enumerate(osm_nodes.tolist())}
        eu, ev = u[keep], v[keep]
        elen = np.zeros(E, dtype=np.uint16)
        espd = np.zeros(E, dtype=np.uint8)
        eflag = np.zeros(E, dtype=np.uint8)
        gstart = np.zeros(E + 1, dtype=np.uint32)
        geom = []
        long_edges = 0
        for j, p in enumerate(kept):
            w = ctx.ways[ctx.P_way[p]]
            i0, i1 = ctx.P_i0[p], ctx.P_i1[p]
            pts = list(zip(w.lat[i0:i1 + 1], w.lon[i0:i1 + 1]))
            a, b = int(eu[j]), int(ev[j])
            node_lat[a], node_lon[a] = pts[0]
            node_lat[b], node_lon[b] = pts[-1]
            L = sum(dist_m(*pts[i], *pts[i + 1]) for i in range(len(pts) - 1))
            if L > 65000:
                long_edges += 1
            elen[j] = max(1, min(65535, int(round(L))))
            espd[j] = w.speed
            cls = CLS[w.hw]
            eflag[j] = (1 if w.oneway != -1 else 0) | (2 if w.oneway != 1 else 0) | (4 if w.rb else 0) | \
                       (8 if w.nosnap else 0) | (cls << 4)
            interior = simplify(pts, SIMPLIFY_M)[1:-1]
            gstart[j] = len(geom)
            geom += encode_interior(int(round(pts[0][0] * 1e6)), int(round(pts[0][1] * 1e6)), interior)
        gstart[E] = len(geom)
        for N in sig_nodes:
            i = idx_of.get(int(N))
            if i is not None:
                nflag[i] |= 1
        de = np.empty(2 * E, dtype=np.int64)
        de[0::2], de[1::2] = kept * 2, kept * 2 + 1
        arrs = {"lat": np.round(node_lat * 1e6), "lon": np.round(node_lon * 1e6), "nflag": nflag,
                "eu": eu, "ev": ev, "elen": elen, "espd": espd, "eflag": eflag,
                "egrade": gr[kept].reshape(-1), "gstart": gstart,
                "geom": np.array(geom, dtype=np.int64).reshape(-1) if geom else np.zeros(0, dtype=np.int64),
                "mcar": car[de], "marr": arrow[de], "mwl": wl[de]}
        raw = write_graph(raw_path, arrs, len(osm_nodes), E, len(geom))
        g = read_graph_bin(raw)                        # round trip, as the phone reads it
        st.check(g["E"] == E and g["N"] == len(osm_nodes), "the file reads back with the counts written",
                 (g["E"], g["N"]))
        sha = hashlib.sha256(raw).hexdigest()
        gz_path = os.path.join(data_dir, "la-graph.bin.gz")
        with open(gz_path + ".tmp", "wb") as z:      # mtime 0, no name in the header: same roads, same bytes
            z.write(gzip.compress(raw, compresslevel=9, mtime=0))
        os.replace(gz_path + ".tmp", gz_path)
        os.remove(raw_path)
        st.count("nodes", len(osm_nodes))
        st.count("edges", E)
        st.count("geom_points", len(geom))
        st.count("bytes_raw", len(raw))
        st.count("bytes_gz", os.path.getsize(gz_path))
        st.count("signal_nodes", int((nflag & 1).sum()))
        st.count("protected_left_moves", int(np.unpackbits(arrs["mwl"].view(np.uint8)).sum()))
        st.check(long_edges == 0, "no edge longer than 65 km (u16 metres)", long_edges)
        st.check(os.path.getsize(gz_path) < 60_000_000, "a download under 60 MB for the phone", os.path.getsize(gz_path))
        return g, sha, os.path.getsize(gz_path), len(raw)


def stage_probe(g, probes):
    with diag.Stage("probe", inputs={"probes": [p["name"] for p in probes]}) as st:
        t0 = time.time()
        R = Router(g)
        st.count("router_init_s", round(time.time() - t0, 1))
        rows, differs = [], 0
        for p in probes:
            row = {"name": p["name"]}
            for prof in ("car", "arrow", "arrow_flat"):
                t1 = time.time()
                r = R.route(p["from"], p["to"], prof)
                st.check(r is not None, f"{p['name']}: a {prof} route", "no route")
                r["s"] = round(time.time() - t1, 1)
                row[prof] = r
            car, arw, flat = row["car"], row["arrow"], row["arrow_flat"]
            st.check(arw["cost"] >= car["cost"] - 0.5, f"{p['name']}: arrow never cheaper than car ({car['cost']})",
                     arw["cost"])
            if arw["des"] != car["des"]:
                differs += 1
            if flat["max_climb"] > arw["max_climb"] + 1:
                st.warn(f"{p['name']}: arrow_flat climbs {flat['max_climb']}% vs arrow {arw['max_climb']}%")
            for prof in ("car", "arrow", "arrow_flat"):
                row[prof].pop("des")
                st.count(f"{p['name']}.{prof}.min", row[prof]["min"])
            diag.emit("probe", "ROUTE", row)
            rows.append(row)
        st.check(differs >= 1, "arrow's route differs from car's on at least one probe (masks loaded)",
                 "identical on every probe")
        st.keep("routes", rows)
        return rows


PROBES = [
    {"name": "sherman-oaks→dtla", "from": [34.15130, -118.44890], "to": [34.05370, -118.24270]},
    {"name": "dtla→sherman-oaks", "from": [34.05370, -118.24270], "to": [34.15130, -118.44890]},
    {"name": "santa-monica→pasadena", "from": [34.01600, -118.49600], "to": [34.14780, -118.14430]},
    {"name": "long-beach→burbank", "from": [33.77010, -118.19370], "to": [34.18080, -118.30900]},
]


def main():
    ap = argparse.ArgumentParser(description="arrow map data builder")
    ap.add_argument("--data", default=os.path.join(HERE, "..", "data"), help="the app's data folder (committed)")
    ap.add_argument("--work", default=os.environ.get("ARROW_WORK", "/tmp/arrow-work"))
    a = ap.parse_args()
    data_dir = os.path.abspath(a.data)
    work = os.path.abspath(a.work)
    os.makedirs(work, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)
    baseline_dir = os.path.join(data_dir, "baseline")
    prev = None
    mpath = os.path.join(data_dir, "manifest.json")
    if os.path.exists(mpath):
        with open(mpath) as f:
            prev = json.load(f)
    try:
        src = stage_fetch(work)
        roads, osm_ts = stage_extract(src, work)
        signs = stage_ladot(work, baseline_dir)
        (ctx, car, arrow, wl, keep, u, v, osm_nodes, sig_nodes, feats, sample, gcounts) = \
            stage_graph(roads, signs, os.path.join(HERE, "whitelist.csv"))
        gr = stage_elevation(ctx, keep, work)
        g, sha, gz_bytes, raw_bytes = stage_write(ctx, car, arrow, wl, keep, u, v, osm_nodes, sig_nodes, gr, data_dir)
        rows = stage_probe(g, PROBES)
        with diag.Stage("publish", inputs={"previous": prev.get("version") if prev else None}) as st:
            if prev:
                pe = prev.get("edges", 0)
                st.check(g["E"] >= 0.75 * pe, f"edges within 25% of the last good build ({pe})", g["E"])
            unchanged = bool(prev and prev.get("sha256") == sha)
            st.count("unchanged", int(unchanged))
        if unchanged:                                         # same roads phones already hold: write nothing,
            diag.emit("publish", "UNCHANGED", {"version": prev.get("version"), "sha256": sha[:16]})   # so nothing commits
            diag.print_summary()
            return
        with diag.Stage("publish_files", inputs={"previous": prev.get("version") if prev else None}) as st:
            version = diag.RUN_ID
            manifest = {"format": FORMAT, "version": version, "file": "la-graph.bin.gz", "sha256": sha,
                        "bytes_gz": gz_bytes, "bytes_raw": raw_bytes, "nodes": g["N"], "edges": g["E"],
                        "geom_points": g["G"], "bbox": list(BBOX), "osm_timestamp": osm_ts,
                        "built": diag._now(), "turn_costs_s": TURN, "vmax_mps": VMAX,
                        "whitelist": {k: gcounts.get(k) for k in ("wl_osm_dual_left", "wl_ladot_matched",
                                                                  "wl_hand_matched", "wl_approaches")},
                        "probes": [{"name": r["name"], **{p: {k: r[p][k] for k in ("min", "km", "protected_lefts", "max_climb")}
                                                           for p in ("car", "arrow", "arrow_flat")}} for r in rows]}
            report = {"version": version, "counts": gcounts, "blocked_sample": sample, "probes": rows}
            with open(os.path.join(data_dir, "whitelist.geojson"), "w") as f:
                json.dump({"type": "FeatureCollection", "features": feats}, f)
            with open(os.path.join(data_dir, "report.json"), "w") as f:
                json.dump(report, f, indent=1)
            with open(mpath + ".tmp", "w") as f:
                json.dump(manifest, f, indent=1)
            os.replace(mpath + ".tmp", mpath)
            st.count("edges", g["E"])
            st.count("bytes_gz", gz_bytes)
    except diag.StageFailure as e:
        diag.print_summary()
        print(f"{diag.RED}{e}{diag.OFF}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:                            # outside any stage: still leave the first red line
        import traceback
        tb = traceback.format_exc()
        diag.print_summary()
        print(tb, file=sys.stderr)
        diag.annotate("error", "crash outside a stage", f"{type(e).__name__}: {e}\n\n{tb[-3000:]}")
        sys.exit(1)
    diag.print_summary()


if __name__ == "__main__":
    main()
