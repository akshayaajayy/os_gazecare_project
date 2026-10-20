"""GazeCare OS - request scheduling for a simulated hospital ward.

Run:  pip install flask && python app.py          ->  http://127.0.0.1:5000
      python app.py --experiments [--seeds 10]    ->  headless experiment suite (results saved to SQLite + CSV)

Five patients send care requests (HELP, PAIN, WASHROOM, MEDICINE, WATER, FOOD, REPOSITION, BLANKET).
The requests go into one bounded queue and are served by 1-3 caregivers under a chosen scheduling policy.
Every run is logged to SQLite (gazecare.db): runs, requests, events and experiment results.

How requests are produced: each patient has a profile (profiles.json, built by build_profiles.py from the
eye-tracking recordings) holding their selection speed, correction rate and tracker-dropout behaviour.
How OFTEN patients need care, which category they pick and the caregiver service times are modelling choices
(see CATS and LOADS below), not measurements.

Research prototype - not a medical device and not validated for patient care.
"""
import argparse
import csv
import heapq
import io
import json
import os
import random
import sqlite3
import statistics
import threading
import time

from flask import Flask, Response, jsonify, render_template, request

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("GAZECARE_DB", os.path.join(BASE, "gazecare.db"))
PROFILE_PATH = os.path.join(BASE, "profiles.json")

app = Flask(__name__)
DT = 0.05                           # simulation tick (sim-seconds)
LOCK = threading.RLock()            # mutual exclusion: guards the live simulation (queue, caregivers, counters)

# ---- request categories: priority (1 = most urgent), share of requests, caregiver service time range (sim-seconds) ----
CATS = {
    "HELP":       dict(pri=1, w=0.04, svc=(4, 6)),      # emergency
    "PAIN":       dict(pri=2, w=0.12, svc=(6, 10)),     # urgent
    "WASHROOM":   dict(pri=2, w=0.12, svc=(10, 15)),    # urgent
    "MEDICINE":   dict(pri=3, w=0.14, svc=(5, 8)),      # scheduled
    "WATER":      dict(pri=4, w=0.22, svc=(3, 5)),      # routine
    "FOOD":       dict(pri=4, w=0.14, svc=(8, 12)),     # routine
    "REPOSITION": dict(pri=4, w=0.12, svc=(8, 12)),     # routine
    "BLANKET":    dict(pri=4, w=0.10, svc=(4, 6)),      # routine
}
EMERG = ("HELP", "PAIN")            # "emergency" metrics cover these two categories
ALGOS = {"fcfs": "FCFS", "priority": "Priority", "priority_aging": "Priority + Aging"}
# mean seconds between care needs of ONE patient (5 patients, 2 caregivers -> roughly 20% / 75% / 100% / 150% busy)
LOADS = {"light": 48.0, "moderate": 25.0, "heavy": 18.0, "overload": 12.0}
AGING_FLOOR = 3          # aging never lifts a request above priority 3, so HELP and urgent (1-2) are never overtaken by it
PREEMPT_COST = 0.0       # extra service time a preempted request needs when it resumes (sim-seconds)
STARVE_AFTER = 60.0      # a request that waited longer than this for its first service counts as "starved"

DEFAULT_CFG = dict(algo="priority_aging", preempt=True, caregivers=2, capacity=20, load="moderate",
                   speed=10.0, duration=600.0, seed=1, aging=5.0)
# settings that define a run: changing one starts a fresh run so every logged run has a single configuration
RESTART_KEYS = ("algo", "preempt", "caregivers", "capacity", "load", "duration", "seed", "aging")


# =============================================================== SQLite logging
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT, status TEXT,
  algo TEXT, preempt INTEGER, caregivers INTEGER, capacity INTEGER, load TEXT, seed INTEGER, config TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS requests(run_id INTEGER, req_id INTEGER, patient TEXT, category TEXT, priority INTEGER,
  origin TEXT, arrival REAL, service REAL, first_start REAL, finish REAL, wait REAL, status TEXT,
  preemptions INTEGER, acknowledged INTEGER, PRIMARY KEY(run_id, req_id));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, sim_time REAL, wall TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS experiments(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, config TEXT, results TEXT);
CREATE TABLE IF NOT EXISTS experiment_runs(exp_id INTEGER, load TEXT, policy TEXT, seed INTEGER, metrics TEXT);
CREATE INDEX IF NOT EXISTS ix_req ON requests(run_id);
CREATE INDEX IF NOT EXISTS ix_ev ON events(run_id);
"""


class Store:
    """Small thread-safe wrapper around one SQLite connection (all access goes through one lock)."""

    def __init__(self, path):
        self.lock = threading.Lock()
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        with self.lock:
            self.con.execute("PRAGMA journal_mode=WAL")
            self.con.execute("PRAGMA synchronous=NORMAL")
            self.con.executescript(SCHEMA)
            self.con.commit()

    def run(self, sql, args=()):
        with self.lock:
            cur = self.con.execute(sql, args)
            self.con.commit()
            return cur.lastrowid

    def many(self, sql, rows):
        with self.lock:
            self.con.executemany(sql, rows)
            self.con.commit()

    def all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.con.execute(sql, args).fetchall()]


STORE = Store(DB_PATH)


# =============================================================== patient profiles and workload
def load_profiles():
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, encoding="utf-8") as fh:
            ps = json.load(fh)["patients"]
        if ps:
            return ps
    # fallback so the app still runs without profiles.json (run build_profiles.py to create it)
    return [dict(name=f"Patient {i + 1}", intervals=[3.0, 3.5, 4.0, 5.0], correction_rate=0.15, gap_prob=0.0,
                 gap_durations=[0.0]) for i in range(5)]


PROFILES = load_profiles()


def make_workload(seed, load, duration, profiles=None):
    """Deterministic request schedule for one run. It depends only on (seed, load, duration), never on the
    scheduling policy, so FCFS / Priority / Aging are all compared on the identical requests.

    Per patient: a care need arises after an exponential gap. The patient then needs a selection time (drawn from
    that patient's own recorded selection gaps, plus a tracker-dropout delay with the recorded probability) before the
    request reaches the queue. With probability = the patient's correction rate the first selection is a mistake: a
    wrong request is sent, then retracted after another selection gap, and the right one follows.
    """
    profiles = profiles or PROFILES
    names, weights = list(CATS), [CATS[c]["w"] for c in CATS]
    out = []
    for pi, p in enumerate(profiles):
        rng = random.Random(f"{seed}/{load}/{pi}")
        t = 0.0
        while True:
            t += rng.expovariate(1.0 / LOADS[load])
            if t >= duration:
                break
            cat = rng.choices(names, weights)[0]
            delay = rng.choice(p["intervals"])
            if rng.random() < p["gap_prob"]:
                delay += rng.choice(p["gap_durations"])
            arr = t + delay
            if rng.random() < p["correction_rate"]:
                wrong = rng.choice([c for c in names if c not in (cat, "HELP")])
                cancel = arr + rng.choice(p["intervals"])
                lo, hi = CATS[wrong]["svc"]
                out.append(dict(arr=arr, pat=p["name"], cat=wrong, svc=round(rng.uniform(lo, hi), 2), cancel=cancel))
                arr = cancel + rng.choice(p["intervals"])
            lo, hi = CATS[cat]["svc"]
            out.append(dict(arr=arr, pat=p["name"], cat=cat, svc=round(rng.uniform(lo, hi), 2), cancel=None))
    out.sort(key=lambda w: w["arr"])
    return out


def pct(vals, p):
    """Percentile with linear interpolation (same as numpy's default)."""
    if not vals:
        return 0.0
    v = sorted(vals)
    k = (len(v) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


# =============================================================== simulation
class Sim:
    def __init__(s, cfg=None, store=None, hist=True, workload=None):
        s.cfg = dict(DEFAULT_CFG)
        s.cfg.update(cfg or {})
        s.store, s.keep_hist, s.fixed_wl = store, hist, workload
        s.reset()

    # ---- lifecycle ----
    def reset(s):
        if s.store and s.run_id is not None and s.status == "running":
            s.close_run("aborted")
        c = s.cfg
        s.t, s.R, s.q = 0.0, {}, []
        s.cg = [None] * int(c["caregivers"])          # one slot per caregiver
        s.log, s.hist, s.nid, s.pre = [], [], 1, 0
        s.running, s.nh, s.run_id, s.status = False, 1.0, None, "idle"
        s.wl = s.fixed_wl if s.fixed_wl is not None else make_workload(int(c["seed"]), c["load"], float(c["duration"]))
        s.wi, s.cancels = 0, []                         # next workload index / heap of (cancel_time, request id)
        s.interrupt = threading.Event()                 # application-level "interrupt line" raised by HELP
        s.mrng = random.Random(int(c["seed"]) * 7919)   # service times of manually added requests

    def open_run(s):
        if s.store and s.run_id is None:
            c = s.cfg
            s.run_id = s.store.run(
                "INSERT INTO runs(started,status,algo,preempt,caregivers,capacity,load,seed,config) VALUES(?,?,?,?,?,?,?,?,?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"), "running", c["algo"], int(bool(c["preempt"])), int(c["caregivers"]),
                 int(c["capacity"]), c["load"], int(c["seed"]), json.dumps(c)))
        s.status = "running"

    def close_run(s, status):
        if s.store and s.run_id is not None:
            s.store.run("UPDATE runs SET finished=?, status=?, summary=? WHERE id=?",
                        (time.strftime("%Y-%m-%d %H:%M:%S"), status, json.dumps(s.metrics()), s.run_id))
        s.status = status

    run_id = None
    status = "idle"

    # ---- logging ----
    def note(s, msg):
        if not s.keep_hist and not s.store:     # headless experiment runs do not need the text log
            return
        s.log.append(dict(t=round(s.t, 2), wall=time.strftime("%H:%M:%S"), msg=msg))
        if s.store and s.run_id is not None:
            s.store.run("INSERT INTO events(run_id,sim_time,wall,message) VALUES(?,?,?,?)",
                        (s.run_id, round(s.t, 2), time.strftime("%H:%M:%S"), msg))

    def save(s, r):
        if s.store and s.run_id is not None:
            s.store.run("INSERT OR REPLACE INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (s.run_id, r["id"], r["pat"], r["cat"], r["pri"], r["origin"], round(r["arr"], 3), r["svc"],
                         r["start"], r["end"], r["wait"], r["status"], r["pre"], int(bool(r["ack"]))))

    # ---- requests ----
    def new(s, cat, patient, origin, svc=None):
        pri = CATS[cat]["pri"]
        if svc is None:
            svc = round(s.mrng.uniform(*CATS[cat]["svc"]), 2)
        r = dict(id=s.nid, cat=cat, pat=patient, pri=pri, origin=origin, arr=s.t, svc=svc, rem=svc, served=0.0,
                 start=None, end=None, wait=None, status="queued", ack=(cat != "HELP"), pre=0, seg=None)
        s.nid += 1
        s.R[r["id"]] = r
        if len(s.q) >= int(s.cfg["capacity"]):
            victim = None
            if pri == 1:    # emergencies are never refused: evict the least urgent, newest waiting request
                cand = max(s.q, key=lambda x: (x["pri"], x["arr"]))
                if cand["pri"] > 1:
                    victim = cand
            if victim:
                s.q.remove(victim)
                victim.update(status="dropped", end=s.t)
                s.save(victim)
                s.note(f"#{victim['id']} {victim['cat']} DROPPED (queue full, made room for HELP)")
                s.q.append(r)
            else:
                r.update(status="dropped", end=s.t)
                s.note(f"#{r['id']} {cat} ({patient}) DROPPED (queue full)")
        else:
            s.q.append(r)
        if r["status"] == "queued":
            s.note(f"#{r['id']} {cat} queued ({patient})")
            if cat == "HELP":
                s.interrupt.set()
        s.save(r)
        return r

    def key(s, r):
        a = s.cfg["algo"]
        if a == "fcfs":
            return (0, r["arr"], r["id"])
        p = r["pri"]
        if a == "priority_aging":     # gain one level per `aging` seconds spent waiting (not being served)
            waited = s.t - r["arr"] - r["served"]
            p = max(min(p, AGING_FLOOR), p - int(waited // float(s.cfg["aging"])))   # floor never demotes HELP
        return (p, r["arr"], r["id"])

    def effective(s, r):
        return s.key(r)[0]

    def begin(s, i, r):
        s.cg[i] = r
        r["status"], r["seg"] = "processing", s.t
        if r["start"] is None:
            r["start"], r["wait"] = s.t, s.t - r["arr"]
            s.note(f"#{r['id']} {r['cat']} started by caregiver {i + 1} (waited {r['wait']:.1f}s)")
        else:
            s.note(f"#{r['id']} {r['cat']} resumed by caregiver {i + 1}")
        s.save(r)

    def finish(s, i, r):
        s.cg[i] = None
        r["served"] += s.t - r["seg"]
        r.update(status="completed", end=s.t)
        s.save(r)
        s.note(f"#{r['id']} {r['cat']} completed (waited {r['wait']:.1f}s)")

    def cancel(s, rid):
        r = s.R.get(rid)
        if not r or r["status"] not in ("queued", "processing"):
            return
        if r["status"] == "queued":
            s.q.remove(r)
        else:
            r["served"] += s.t - r["seg"]            # service time already spent is wasted
            s.cg[s.cg.index(r)] = None
        r.update(status="cancelled", end=s.t)
        s.save(r)
        s.note(f"#{rid} {r['cat']} cancelled by patient")

    def dispatch(s):
        if s.cfg["preempt"] and s.cfg["algo"] != "fcfs":   # FCFS has no notion of urgency, so it never preempts
            while s.q and None not in s.cg:    # all caregivers busy: can a waiting request preempt one?
                best = min(s.q, key=s.key)
                i, v = max(enumerate(s.cg), key=lambda x: (x[1]["pri"], x[1]["seg"]))   # least urgent, most recent
                if best["pri"] >= v["pri"]:
                    break
                v["served"] += s.t - v["seg"]
                v["rem"] = v["svc"] - v["served"] + PREEMPT_COST * (v["pre"] + 1)
                v.update(status="queued", pre=v["pre"] + 1)
                s.pre += 1
                s.q.append(v)
                s.save(v)
                s.q.remove(best)
                s.note(f"#{v['id']} {v['cat']} PREEMPTED by #{best['id']} {best['cat']} ({v['rem']:.1f}s left)")
                s.begin(i, best)
        for i, c in enumerate(s.cg):
            if c is None and s.q:
                r = min(s.q, key=s.key)
                s.q.remove(r)
                s.begin(i, r)

    # ---- one tick ----
    def step(s):
        s.t += DT
        t = s.t
        while s.wi < len(s.wl) and s.wl[s.wi]["arr"] <= t:         # patients' requests arrive
            w = s.wl[s.wi]
            s.wi += 1
            r = s.new(w["cat"], w["pat"], "patient", w["svc"])
            if w["cancel"] is not None and r["status"] == "queued":
                heapq.heappush(s.cancels, (w["cancel"], r["id"]))
        while s.cancels and s.cancels[0][0] <= t:                  # patients retract mistaken requests
            s.cancel(heapq.heappop(s.cancels)[1])
        for i, r in enumerate(s.cg):                               # caregivers work
            if r is not None:
                if t - r["seg"] + r["served"] >= r["svc"] + PREEMPT_COST * r["pre"] - 1e-9:
                    s.finish(i, r)
        if s.interrupt.is_set():                                   # the interrupt handler runs before normal dispatch
            s.interrupt.clear()
            s.note("HELP interrupt raised: dispatcher notified")
        s.dispatch()
        if s.keep_hist and t >= s.nh:
            s.nh += 1
            m = s.metrics()
            s.hist.append(dict(t=round(t, 1), q=len(s.q), w=m["avg_wait"], th=m["throughput"], em=m["emerg_wait"]))

    def done_flag(s):
        return s.wi >= len(s.wl) and not s.q and not any(s.cg)

    def run_to_end(s, limit=100000.0):
        s.open_run()
        while not s.done_flag() and s.t < limit:
            s.step()
        s.close_run("finished")

    # ---- metrics (all times are simulated seconds) ----
    def metrics(s):
        R = list(s.R.values())
        done = [r for r in R if r["status"] == "completed"]
        w = [r["wait"] for r in done]
        avg = lambda x: round(sum(x) / len(x), 3) if x else 0.0
        em = [r["wait"] for r in done if r["cat"] in EMERG]
        hp = [r["wait"] for r in done if r["cat"] == "HELP"]
        rt = [r["wait"] for r in done if r["pri"] == 4]
        started = [r for r in R if r["start"] is not None]
        starved = sum(1 for r in started if r["wait"] > STARVE_AFTER) + \
            sum(1 for r in R if r["start"] is None and r["status"] == "queued" and s.t - r["arr"] > STARVE_AFTER)
        per = {}
        for r in done:
            per.setdefault(r["pat"], []).append(r["wait"])
        pa = {k: round(sum(v) / len(v), 3) for k, v in sorted(per.items())}
        xs = list(pa.values())
        jain = round(sum(xs) ** 2 / (len(xs) * sum(x * x for x in xs)), 3) if xs and sum(x * x for x in xs) > 0 else 1.0
        busy = sum(r["served"] for r in R) + sum(s.t - r["seg"] for r in s.cg if r)   # includes work lost to cancellations
        return dict(generated=len(R), completed=len(done), dropped=sum(r["status"] == "dropped" for r in R),
                    cancelled=sum(r["status"] == "cancelled" for r in R), qlen=len(s.q), capacity=int(s.cfg["capacity"]),
                    avg_wait=avg(w), med_wait=round(pct(w, 50), 3), p95_wait=round(pct(w, 95), 3),
                    max_wait=round(max(w), 3) if w else 0.0, emerg_wait=avg(em), help_wait=avg(hp),
                    routine_wait=avg(rt), routine_max=round(max(rt), 3) if rt else 0.0, starved=starved,
                    preemptions=s.pre, throughput=round(len(done) / s.t, 4) if s.t > 0 else 0.0,
                    util=round(min(1.0, busy / (s.t * len(s.cg))), 3) if s.t > 0 else 0.0,
                    fairness=jain, per_patient=pa)


SIM = Sim(store=STORE)


# =============================================================== background clock for the live simulation
def ticker():
    acc = 0.0
    while True:
        time.sleep(0.1)
        with LOCK:
            if not SIM.running:
                acc = 0.0
                continue
            acc += 0.1 * float(SIM.cfg["speed"])
            while acc >= DT:
                SIM.step()
                acc -= DT
                if SIM.done_flag():
                    SIM.running = False
                    SIM.note("run finished")
                    SIM.close_run("finished")
                    break


threading.Thread(target=ticker, daemon=True).start()


def pub(r):
    d = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items() if k not in ("rem", "seg")}
    d["eff"] = SIM.effective(r) if r["status"] == "queued" else r["pri"]
    d["left"] = round(r["svc"] - r["served"] - (SIM.t - r["seg"] if r["status"] == "processing" else 0), 2)
    return d


def state(all_=False):
    with LOCK:
        s = SIM
        rs = list(s.R.values())
        out = dict(t=round(s.t, 2), running=s.running, finished=s.done_flag() and s.t > 0, cfg=s.cfg, m=s.metrics(),
                   run_id=s.run_id, queue=[pub(r) for r in sorted(s.q, key=s.key)],
                   caregivers=[pub(r) if r else None for r in s.cg],
                   incoming=[pub(r) for r in rs if s.t - r["arr"] < 3][-6:],
                   done=[pub(r) for r in rs if r["status"] == "completed"][-8:][::-1],
                   lost=[pub(r) for r in rs if r["status"] in ("dropped", "cancelled")][-8:][::-1],
                   alerts=[pub(r) for r in rs if r["cat"] == "HELP" and not r["ack"] and r["status"] in ("queued", "processing")],
                   log=s.log[-40:][::-1], hist=s.hist[-300:], patients=[p["name"] for p in PROFILES],
                   cats=list(CATS))
        if all_:
            out["all"] = [pub(r) for r in rs][-300:][::-1]
        return out


# =============================================================== API
CAST = dict(algo=str, preempt=lambda v: v in (True, "true", "1", 1, "on"), caregivers=int, capacity=int, load=str,
            speed=float, duration=float, seed=int, aging=float)


@app.post("/api/control")
def control():
    d = request.get_json(silent=True) or {}
    a = d.get("action")
    with LOCK:
        if a == "start" and not SIM.done_flag():
            SIM.open_run()
            SIM.running = True
            SIM.note("run started")
        elif a == "pause" and SIM.running:
            SIM.running = False
            SIM.note("run paused")
        elif a == "reset":
            SIM.reset()
            SIM.note("simulation reset")
        elif a == "config":
            restart = False
            for k, c in CAST.items():
                if k in d and d[k] != "":
                    try:
                        v = c(d[k])
                    except (TypeError, ValueError):
                        return jsonify(error=f"bad value for {k}"), 400
                    if (k == "algo" and v not in ALGOS) or (k == "load" and v not in LOADS) or \
                            (k in ("caregivers", "capacity") and not 1 <= v <= 100) or (k == "caregivers" and v > 3) or \
                            (k == "speed" and not 0.5 <= v <= 200) or (k == "aging" and v <= 0) or (k == "duration" and v < 30):
                        return jsonify(error=f"bad value for {k}"), 400
                    if SIM.cfg[k] != v:
                        SIM.cfg[k] = v
                        restart = restart or k in RESTART_KEYS
            if restart:
                SIM.reset()
                SIM.note("settings changed: new run")
        return jsonify(state())


@app.post("/api/request")
def manual():
    d = request.get_json(silent=True) or {}
    cat, pat = d.get("cat", "HELP"), d.get("patient", PROFILES[0]["name"])
    if cat not in CATS or pat not in [p["name"] for p in PROFILES]:
        return jsonify(error="unknown category or patient"), 400
    with LOCK:
        if SIM.done_flag() and SIM.t > 0:
            return jsonify(error="run finished: press Reset to start a new one"), 409
        SIM.open_run()
        SIM.new(cat, pat, "manual")
        return jsonify(state(True))


@app.post("/api/request/<int:i>/ack")
def ack(i):
    with LOCK:
        r = SIM.R.get(i)
        if r and not r["ack"]:
            r["ack"] = True
            SIM.save(r)
            SIM.note(f"#{i} {r['cat']} acknowledged by operator")
        return jsonify(state(True))


@app.get("/api/state")
def api_state():
    return jsonify(state(request.args.get("all") == "1"))


# ---- logs ----
@app.get("/api/runs")
def runs():
    rows = STORE.all("SELECT id,started,finished,status,algo,preempt,caregivers,load,summary FROM runs ORDER BY id DESC LIMIT 30")
    for r in rows:
        sm = json.loads(r.pop("summary") or "{}")
        r.update({k: sm.get(k) for k in ("generated", "completed", "avg_wait", "emerg_wait")})
    return jsonify(rows)


def csv_response(rows, name):
    o = io.StringIO()
    if rows:
        w = csv.DictWriter(o, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return Response(o.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})


@app.get("/export/requests.csv")
def export_requests():
    run = request.args.get("run", type=int)
    q = "SELECT * FROM requests" + (" WHERE run_id=?" if run else "") + " ORDER BY run_id, req_id"
    return csv_response(STORE.all(q, (run,) if run else ()), "requests.csv")


@app.get("/export/events.csv")
def export_events():
    run = request.args.get("run", type=int)
    q = "SELECT * FROM events" + (" WHERE run_id=?" if run else "") + " ORDER BY id"
    return csv_response(STORE.all(q, (run,) if run else ()), "events.csv")


# ---- experiments ----
POLICIES = [("FCFS", "fcfs", False), ("Priority", "priority", False), ("Priority + Aging", "priority_aging", False),
            ("Priority + Preemption", "priority", True), ("Priority + Aging + Preemption", "priority_aging", True)]
METRIC_KEYS = ["generated", "completed", "dropped", "cancelled", "avg_wait", "med_wait", "p95_wait", "max_wait",
               "emerg_wait", "help_wait", "routine_wait", "routine_max", "starved", "preemptions", "throughput",
               "util", "fairness"]
EXP = dict(running=False, done=0, total=0, error=None)


def run_experiments(seeds=10, duration=600.0, caregivers=2, capacity=20, aging=5.0, loads=None, progress=None):
    """Every policy sees exactly the same request schedule for a given (load, seed)."""
    loads = loads or list(LOADS)
    raw = []
    for load in loads:
        for seed in range(1, seeds + 1):
            wl = make_workload(seed, load, duration)
            for name, algo, pre in POLICIES:
                sim = Sim(dict(algo=algo, preempt=pre, caregivers=caregivers, capacity=capacity, load=load, seed=seed,
                               duration=duration, aging=aging), store=None, hist=False, workload=wl)
                sim.run_to_end()
                m = sim.metrics()
                raw.append(dict(load=load, policy=name, seed=seed, **{k: m[k] for k in METRIC_KEYS}))
                if progress:
                    progress()
    agg = []
    for load in loads:
        for name, _, _ in POLICIES:
            rows = [r for r in raw if r["load"] == load and r["policy"] == name]
            row = dict(load=load, policy=name, seeds=len(rows))
            for k in METRIC_KEYS:
                vals = [r[k] for r in rows]
                row[k] = round(statistics.mean(vals), 3)
                row[k + "_sd"] = round(statistics.stdev(vals), 3) if len(vals) > 1 else 0.0
            agg.append(row)
    cfg = dict(seeds=seeds, duration=duration, caregivers=caregivers, capacity=capacity, aging=aging, loads=loads)
    exp_id = STORE.run("INSERT INTO experiments(ts,config,results) VALUES(?,?,?)",
                       (time.strftime("%Y-%m-%d %H:%M:%S"), json.dumps(cfg), json.dumps(agg)))
    STORE.many("INSERT INTO experiment_runs VALUES(?,?,?,?,?)",
               [(exp_id, r["load"], r["policy"], r["seed"], json.dumps({k: r[k] for k in METRIC_KEYS})) for r in raw])
    return exp_id, cfg, agg, raw


def experiment_job(cfg):
    try:
        def tick():
            EXP["done"] += 1
        run_experiments(progress=tick, **cfg)
    except Exception as e:                      # surface the failure to the page instead of dying silently
        EXP["error"] = str(e)
    finally:
        EXP["running"] = False


@app.post("/api/experiment")
def experiment():
    d = request.get_json(silent=True) or {}
    try:
        cfg = dict(seeds=int(d.get("seeds", 10)), duration=float(d.get("duration", 600)),
                   caregivers=int(d.get("caregivers", 2)), capacity=int(d.get("capacity", 20)))
    except (TypeError, ValueError):
        return jsonify(error="bad value"), 400
    if not (1 <= cfg["seeds"] <= 50 and 60 <= cfg["duration"] <= 3600 and 1 <= cfg["caregivers"] <= 3 and 1 <= cfg["capacity"] <= 100):
        return jsonify(error="value out of range"), 400
    with LOCK:
        if EXP["running"]:
            return jsonify(error="an experiment is already running"), 409
        EXP.update(running=True, done=0, total=cfg["seeds"] * len(LOADS) * len(POLICIES), error=None)
    threading.Thread(target=experiment_job, args=(cfg,), daemon=True).start()
    return jsonify(EXP)


@app.get("/api/experiment/status")
def experiment_status():
    return jsonify(EXP)


@app.get("/api/experiments")
def experiments():
    rows = STORE.all("SELECT id,ts,config,results FROM experiments ORDER BY id DESC LIMIT 1")
    return jsonify([dict(id=r["id"], ts=r["ts"], cfg=json.loads(r["config"]), results=json.loads(r["results"])) for r in rows])


@app.get("/export/experiments.csv")
def export_experiments():
    rows = STORE.all("SELECT e.exp_id, e.load, e.policy, e.seed, e.metrics FROM experiment_runs e "
                     "WHERE e.exp_id=(SELECT MAX(id) FROM experiments) ORDER BY e.load, e.seed, e.policy")
    flat = []
    for r in rows:
        m = json.loads(r.pop("metrics"))
        flat.append({**r, **m})
    return csv_response(flat, "experiment_runs.csv")


# ---- pages ----
@app.get("/")
def dashboard():
    return render_template("dashboard.html", page="dashboard")


@app.get("/requests")
def requests_():
    return render_template("requests.html", page="requests")


@app.get("/performance")
def performance():
    return render_template("performance.html", page="performance")


@app.get("/methodology")
def methodology():
    return render_template("methodology.html", page="methodology", cats=CATS, loads=LOADS,
                           aging_floor=AGING_FLOOR, starve=STARVE_AFTER)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--experiments", action="store_true", help="run the experiment suite headless and exit")
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--port", type=int, default=5000)
    a = ap.parse_args()
    if a.experiments:
        os.makedirs(os.path.join(BASE, "results"), exist_ok=True)
        t0 = time.time()
        exp_id, cfg, agg, raw = run_experiments(seeds=a.seeds)
        for name, rows in (("experiment_runs.csv", raw), ("experiment_summary.csv", agg)):
            with open(os.path.join(BASE, "results", name), "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
        print(f"experiment #{exp_id}: {len(raw)} runs in {time.time() - t0:.1f}s -> results/*.csv and {DB_PATH}")
        print(f"{'load':9} {'policy':30} {'avg':>7} {'emerg':>7} {'HELP':>6} {'rt.max':>7} {'starved':>7} {'drop':>5} {'preempt':>7} {'fair':>5}")
        for r in agg:
            print(f"{r['load']:9} {r['policy']:30} {r['avg_wait']:7.1f} {r['emerg_wait']:7.1f} {r['help_wait']:6.1f} "
                  f"{r['routine_max']:7.1f} {r['starved']:7.1f} {r['dropped']:5.1f} {r['preemptions']:7.1f} {r['fairness']:5.2f}")
    else:
        app.run(host="127.0.0.1", port=a.port, debug=False, threaded=True)
