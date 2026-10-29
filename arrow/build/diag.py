"""
diag — always-on diagnostics for the arrow pipeline.

Every stage logs what came in, what went out, counts, timing, pass/fail.
Every passing stage writes a baseline (rotated, last KEEP, timestamped) and
diffs its counts against the previous one. Every failing stage dumps the
corpse — inputs, counts so far, raw evidence, traceback, and the last good
baseline it should have matched — and says expected vs got plus the dump path.

Triage contract: read the first red line, open the one file it names.
"""
import datetime
import json
import os
import shutil
import sys
import time
import traceback

DATA = os.environ.get("ARROW_DATA", "/data")
LOG_DIR = os.path.join(DATA, "logs")
BASE_DIR = os.path.join(DATA, "baselines")
DUMP_DIR = os.path.join(DATA, "dumps")
KEEP = 5
DRIFT = 0.25          # count moved more than 25% vs last good run -> WARN line

RUN_ID = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
RED, AMBER, GREEN, DIM, OFF = "\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[0m"
if not sys.stdout.isatty() and not os.environ.get("FORCE_COLOR"):
    RED = AMBER = GREEN = DIM = OFF = ""

SUMMARY = []          # one entry per stage this run: (name, status, ms, note)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _mkdirs():
    for d in (LOG_DIR, BASE_DIR, DUMP_DIR):
        os.makedirs(d, exist_ok=True)


def emit(stage, status, payload=None, color=""):
    """One line to the console, one JSON line to logs/run-<RUN_ID>.jsonl."""
    _mkdirs()
    rec = {"t": _now(), "run": RUN_ID, "stage": stage, "status": status}
    if payload is not None:
        rec["data"] = payload
    with open(os.path.join(LOG_DIR, f"run-{RUN_ID}.jsonl"), "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    short = "" if payload is None else json.dumps(payload, default=str)
    if len(short) > 900:
        short = short[:900] + " …"
    print(f"{color}{rec['t']} {stage} {status}{OFF} {DIM}{short}{OFF}", flush=True)


class StageFailure(Exception):
    def __init__(self, stage, expected, got, dump=None):
        self.stage, self.expected, self.got, self.dump = stage, expected, got, dump
        msg = f"[{stage}] expected {expected}, got {got}"
        if dump:
            msg += f" — dump: {dump}"
        super().__init__(msg)


def last_good(stage):
    """Newest baseline for a stage, or None."""
    d = os.path.join(BASE_DIR, stage)
    if not os.path.isdir(d):
        return None
    files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    if not files:
        return None
    path = os.path.join(d, files[-1])
    try:
        with open(path) as f:
            doc = json.load(f)
        doc["_path"] = path
        return doc
    except Exception as e:  # a corrupt baseline is itself evidence
        emit(stage, "WARN", {"baseline_unreadable": path, "error": str(e)}, AMBER)
        return None


def baseline_file(stage, name):
    """Path of a raw file kept next to the newest baseline (e.g. a fetched payload), or None."""
    d = os.path.join(BASE_DIR, stage)
    if not os.path.isdir(d):
        return None
    hits = sorted(f for f in os.listdir(d) if f.endswith("-" + name))
    return os.path.join(d, hits[-1]) if hits else None


def _rotate(dirpath):
    files = sorted(os.listdir(dirpath))
    stamps = sorted({f.split("-")[0].split(".")[0] for f in files})
    for old in stamps[:-KEEP]:
        for f in files:
            if f.startswith(old):
                try:
                    os.remove(os.path.join(dirpath, f))
                except OSError:
                    pass


class Stage:
    """
    with Stage("arrowize", inputs={...}) as st:
        st.count("ways", n)            # numbers, diffed against the last good run
        st.keep("http", {...})         # raw evidence: goes into the dump on failure, baseline on success
        st.keep_file("raw.json", path) # copy a raw payload next to the baseline (small files only)
        st.check(cond, expected, got)  # boundary assertion; fails loudly at the seam
        st.warn("text")                # amber, non-fatal
    degradable=True: a failure is dumped and logged red as DEGRADED, then swallowed;
    the caller reads st.ok and decides how to fall back.
    """

    def __init__(self, name, inputs=None, degradable=False):
        self.name = name
        self.inputs = inputs or {}
        self.degradable = degradable
        self.counts = {}
        self.evidence = {}
        self.files = {}
        self.warnings = []
        self.ok = None
        self.dump = None
        self.t0 = None

    # recording -----------------------------------------------------------
    def count(self, key, value):
        self.counts[key] = value

    def add(self, key, n=1):
        self.counts[key] = self.counts.get(key, 0) + n

    def keep(self, key, value):
        self.evidence[key] = value

    def keep_file(self, name, path):
        self.files[name] = path

    def warn(self, text):
        self.warnings.append(text)
        emit(self.name, "WARN", text, AMBER)

    def check(self, cond, expected, got):
        if not cond:
            raise StageFailure(self.name, expected, got)

    def progress(self, text):
        emit(self.name, "…", text, DIM)

    # lifecycle -------------------------------------------------------------
    def __enter__(self):
        self.t0 = time.time()
        emit(self.name, "START", {"inputs": self.inputs})
        return self

    def __exit__(self, et, ev, tb):
        ms = int((time.time() - self.t0) * 1000)
        if et is None:
            self.ok = True
            drift = self._write_baseline(ms)
            emit(self.name, "PASS", {"ms": ms, "counts": self.counts}, GREEN)
            for line in drift:
                emit(self.name, "WARN", line, AMBER)
            SUMMARY.append((self.name, "PASS", ms, f"{len(self.warnings)} warn" if self.warnings else ""))
            return False

        self.ok = False
        if isinstance(ev, StageFailure):
            expected, got = ev.expected, ev.got
        else:
            expected, got = "clean run", f"{et.__name__}: {ev}"
        self.dump = self._write_dump(ms, expected, got, tb)
        status = "DEGRADED" if self.degradable else "FAIL"
        emit(self.name, status, {"ms": ms, "expected": expected, "got": got, "dump": self.dump}, RED)
        SUMMARY.append((self.name, status, ms, f"expected {expected}, got {got} — dump: {self.dump}"))
        tb_text = "".join(traceback.format_tb(tb)) if tb and not isinstance(ev, StageFailure) else ""
        if tb_text:
            print(tb_text, file=sys.stderr, flush=True)
        annotate("warning" if self.degradable else "error", f"{self.name} {status}",
                 f"expected {expected}\ngot {got}\ndump {self.dump}\n\n{tb_text[-3000:]}\n"
                 f"counts so far {json.dumps(self.counts, default=str)[:1500]}")
        if self.degradable:
            return True     # swallow; caller falls back
        raise StageFailure(self.name, expected, got, self.dump) from ev

    # files -----------------------------------------------------------------
    def _write_baseline(self, ms):
        d = os.path.join(BASE_DIR, self.name)
        os.makedirs(d, exist_ok=True)
        prev = last_good(self.name)
        doc = {"stage": self.name, "run": RUN_ID, "at": _now(), "ms": ms, "inputs": self.inputs,
               "counts": self.counts, "evidence": self.evidence, "warnings": self.warnings}
        with open(os.path.join(d, f"{RUN_ID}.json"), "w") as f:
            json.dump(doc, f, indent=1, default=str)
        for name, src in self.files.items():
            try:
                shutil.copyfile(src, os.path.join(d, f"{RUN_ID}-{name}"))
            except OSError as e:
                emit(self.name, "WARN", {"baseline_copy_failed": name, "error": str(e)}, AMBER)
        _rotate(d)
        lines = []
        if prev:
            for k, v in self.counts.items():
                pv = prev.get("counts", {}).get(k)
                if isinstance(v, (int, float)) and isinstance(pv, (int, float)) and pv:
                    if abs(v - pv) / abs(pv) > DRIFT:
                        lines.append(f"drift {k}: {pv} → {v} (last good {prev.get('run')})")
        return lines

    def _write_dump(self, ms, expected, got, tb):
        _mkdirs()
        path = os.path.join(DUMP_DIR, f"{RUN_ID}-{self.name}.json")
        prev = last_good(self.name)
        doc = {"stage": self.name, "run": RUN_ID, "at": _now(), "ms": ms,
               "expected": expected, "got": got,
               "traceback": "".join(traceback.format_tb(tb)) if tb else None,
               "inputs": self.inputs, "counts_so_far": self.counts, "evidence": self.evidence,
               "warnings": self.warnings,
               "last_good_baseline": prev.get("_path") if prev else None,
               "last_good_counts": prev.get("counts") if prev else None}
        try:
            with open(path, "w") as f:
                json.dump(doc, f, indent=1, default=str)
        except OSError as e:
            print(f"{RED}could not write dump {path}: {e}{OFF}", file=sys.stderr)
        return path


def annotate(level, title, text):
    """GitHub Actions: the first red line goes on the run page itself, where it reads without
    downloading the log or the diagnostics artifact. No-op anywhere else."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return

    def esc(s, prop=False):
        s = s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        return s.replace(":", "%3A").replace(",", "%2C") if prop else s

    print(f"::{level} title={esc('arrow build · ' + title, True)}::{esc(text[:6000])}", flush=True)


def print_summary():
    print("", flush=True)
    print(f"run {RUN_ID} — log: {os.path.join(LOG_DIR, f'run-{RUN_ID}.jsonl')}")
    for name, status, ms, note in SUMMARY:
        color = GREEN if status == "PASS" else RED
        print(f"  {color}{status:<8}{OFF} {name:<14} {ms/1000:>8.1f}s  {note}")
    print("", flush=True)
