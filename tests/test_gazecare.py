"""Automated tests. Run from the project folder:  python -m unittest discover -s tests -v"""
import copy
import csv
import os
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_tmp = tempfile.mkdtemp()
os.environ["GAZECARE_DB"] = os.path.join(_tmp, "test.db")      # never touch the real database

import app as A                      # noqa: E402
import build_profiles as BP          # noqa: E402

DT = A.DT


def wl(*items):
    """items: (arrival, category, service[, cancel_time]) -> workload list for one test patient."""
    return [dict(arr=i[0], pat="Patient 1", cat=i[1], svc=i[2], cancel=i[3] if len(i) > 3 else None) for i in items]


def run(items, algo="priority", preempt=False, caregivers=1, capacity=20, aging=5.0, until=None):
    s = A.Sim(dict(algo=algo, preempt=preempt, caregivers=caregivers, capacity=capacity, aging=aging),
              store=None, hist=True, workload=wl(*items))
    if until is None:
        s.run_to_end()
    else:
        while s.t < until:
            s.step()
    return s


def starts(s):
    return [r["cat"] for r in sorted((r for r in s.R.values() if r["start"] is not None), key=lambda r: (r["start"], r["id"]))]


class Scheduling(unittest.TestCase):
    # one caregiver is kept busy by a long FOOD request, then three requests wait
    BUSY = [(0.1, "FOOD", 3.0), (0.2, "WATER", 1.0), (0.3, "HELP", 1.0), (0.4, "PAIN", 1.0)]

    def test_fcfs_serves_in_arrival_order(self):
        self.assertEqual(starts(run(self.BUSY, "fcfs")), ["FOOD", "WATER", "HELP", "PAIN"])

    def test_priority_serves_most_urgent_first(self):
        self.assertEqual(starts(run(self.BUSY, "priority")), ["FOOD", "HELP", "PAIN", "WATER"])

    def test_priority_ties_go_to_older_request(self):
        s = run([(0.1, "FOOD", 3.0), (0.2, "PAIN", 1.0), (0.3, "WASHROOM", 1.0)], "priority")
        self.assertEqual(starts(s), ["FOOD", "PAIN", "WASHROOM"])

    def test_aging_lets_old_routine_request_beat_newer_medicine(self):
        items = [(0.1, "FOOD", 6.0), (0.2, "REPOSITION", 1.0), (2.5, "MEDICINE", 1.0)]
        self.assertEqual(starts(run(items, "priority", aging=1.0)), ["FOOD", "MEDICINE", "REPOSITION"])
        self.assertEqual(starts(run(items, "priority_aging", aging=1.0)), ["FOOD", "REPOSITION", "MEDICINE"])

    def test_aging_value_calculation(self):
        s = A.Sim(dict(algo="priority_aging", aging=5.0), store=None, workload=wl())
        r = s.new("WATER", "Patient 1", "manual")            # level 4, arrives at t=0
        for waited, expected in [(0, 4), (4.9, 4), (5.0, 3), (7.0, 3), (100.0, A.AGING_FLOOR)]:
            s.t = waited
            self.assertEqual(s.effective(r), expected, f"waited {waited}")

    def test_aging_never_overtakes_help_or_demotes_it(self):
        s = A.Sim(dict(algo="priority_aging", aging=1.0), store=None, workload=wl())
        old = s.new("WATER", "Patient 1", "manual")
        s.t = 500.0
        help_ = s.new("HELP", "Patient 1", "manual")
        self.assertEqual(s.effective(help_), 1)
        self.assertEqual(s.effective(old), A.AGING_FLOOR)
        self.assertLess(s.key(help_), s.key(old))

    def test_aging_does_not_count_time_in_service_as_waiting(self):
        s = A.Sim(dict(algo="priority_aging", aging=5.0), store=None, workload=wl())
        r = s.new("WATER", "Patient 1", "manual")
        r["served"] = 4.0
        s.t = 8.0                                            # 8 s since arrival, 4 s of it in service -> waited 4 s
        self.assertEqual(s.effective(r), 4)


class Preemption(unittest.TestCase):
    ITEMS = [(0.05, "FOOD", 4.0), (1.0, "HELP", 1.0)]

    def test_help_preempts_routine_service(self):
        s = run(self.ITEMS, "priority", preempt=True)
        help_, food = s.R[2], s.R[1]
        self.assertLessEqual(help_["wait"], DT + 1e-9)                      # served immediately
        self.assertEqual(food["pre"], 1)
        self.assertEqual(s.pre, 1)
        self.assertAlmostEqual(food["served"], 4.0, delta=2 * DT)           # remaining time conserved, nothing lost
        self.assertAlmostEqual(food["end"], 0.05 + 4.0 + 1.0, delta=3 * DT)  # delayed by exactly HELP's service

    def test_without_preemption_help_waits_for_the_routine_request(self):
        s = run(self.ITEMS, "priority", preempt=False)
        self.assertEqual(s.pre, 0)
        self.assertAlmostEqual(s.R[2]["wait"], 0.05 + 4.0 - 1.0, delta=2 * DT)

    def test_fcfs_never_preempts(self):
        self.assertEqual(run(self.ITEMS, "fcfs", preempt=True).pre, 0)

    def test_equal_or_lower_priority_does_not_preempt(self):
        s = run([(0.05, "PAIN", 3.0), (1.0, "WASHROOM", 1.0), (1.2, "WATER", 1.0)], "priority", preempt=True)
        self.assertEqual(s.pre, 0)

    def test_preempts_least_urgent_of_several_caregivers(self):
        s = run([(0.05, "WATER", 5.0), (0.06, "PAIN", 5.0), (1.0, "HELP", 1.0)], "priority", preempt=True, caregivers=2)
        self.assertEqual(s.R[1]["pre"], 1)       # WATER (level 4) interrupted, PAIN (level 2) untouched
        self.assertEqual(s.R[2]["pre"], 0)

    def test_help_raises_interrupt_event_once(self):
        s = A.Sim(dict(), store=None, workload=wl((0.05, "HELP", 1.0)))
        s.step()
        s.step()
        self.assertFalse(s.interrupt.is_set())                               # raised and consumed by the handler
        self.assertTrue(any("interrupt" in l["msg"] for l in s.log))


class QueueAndCancel(unittest.TestCase):
    def test_full_queue_drops_new_routine_request(self):
        s = run([(0.05, "FOOD", 9.0), (0.1, "WATER", 1.0), (0.2, "WATER", 1.0), (0.3, "BLANKET", 1.0)],
                "priority", capacity=2, until=1.0)
        self.assertEqual([r["status"] for r in s.R.values()], ["processing", "queued", "queued", "dropped"])

    def test_full_queue_evicts_least_urgent_to_admit_help(self):
        s = run([(0.05, "FOOD", 9.0), (0.1, "WATER", 1.0), (0.2, "BLANKET", 1.0), (0.3, "HELP", 1.0)],
                "priority", capacity=2, until=1.0)
        self.assertEqual(s.R[3]["status"], "dropped")            # newest of the least urgent
        self.assertEqual(s.R[4]["status"], "queued")
        self.assertEqual(s.metrics()["dropped"], 1)

    def test_help_is_dropped_only_if_queue_holds_only_help(self):
        s = run([(0.05, "FOOD", 9.0), (0.1, "HELP", 1.0), (0.2, "HELP", 1.0), (0.3, "HELP", 1.0)],
                "priority", capacity=2, until=1.0)
        self.assertEqual(s.R[4]["status"], "dropped")

    def test_cancel_removes_queued_request(self):
        s = run([(0.05, "FOOD", 5.0), (0.1, "WATER", 1.0, 1.0)], "priority")
        self.assertEqual(s.R[2]["status"], "cancelled")
        self.assertEqual(s.metrics()["completed"], 1)

    def test_cancel_in_service_frees_caregiver_and_counts_wasted_work(self):
        s = run([(0.05, "FOOD", 10.0, 2.0), (0.1, "WATER", 1.0)], "priority")
        self.assertEqual(s.R[1]["status"], "cancelled")
        self.assertAlmostEqual(s.R[1]["served"], 2.0 - 0.05, delta=2 * DT)
        self.assertAlmostEqual(s.R[2]["start"], 2.0, delta=2 * DT)         # next request takes over right away

    def test_cancel_after_completion_is_ignored(self):
        s = run([(0.05, "WATER", 1.0, 5.0)], "priority")
        self.assertEqual(s.R[1]["status"], "completed")


class Metrics(unittest.TestCase):
    def test_percentile(self):
        self.assertAlmostEqual(A.pct([1, 2, 3, 4], 50), 2.5)
        self.assertAlmostEqual(A.pct([1, 2, 3, 4, 5], 95), 4.8)
        self.assertEqual(A.pct([], 50), 0.0)
        self.assertEqual(A.pct([7], 95), 7)

    def test_wait_is_time_to_first_service_not_response_time(self):
        s = run([(0.05, "FOOD", 2.0), (0.1, "WATER", 1.0)], "fcfs")
        m = s.metrics()
        self.assertAlmostEqual(s.R[2]["wait"], 0.05 + 2.0 - 0.1, delta=2 * DT)
        self.assertAlmostEqual(m["max_wait"], s.R[2]["wait"])
        self.assertEqual(m["completed"], 2)
        self.assertEqual(m["generated"], 2)

    def test_emergency_and_routine_wait_are_separated(self):
        s = run([(0.05, "FOOD", 3.0), (0.1, "HELP", 1.0), (0.1, "WATER", 1.0)], "priority")
        m = s.metrics()
        self.assertAlmostEqual(m["help_wait"], s.R[2]["wait"])
        self.assertAlmostEqual(m["emerg_wait"], s.R[2]["wait"])
        self.assertAlmostEqual(m["routine_wait"], (s.R[1]["wait"] + s.R[3]["wait"]) / 2)
        self.assertGreater(m["routine_max"], m["help_wait"])

    def test_starvation_indicator_counts_long_waits(self):
        s = A.Sim(dict(), store=None, workload=wl())
        r = s.new("WATER", "Patient 1", "manual")
        s.t = A.STARVE_AFTER + 1
        self.assertEqual(s.metrics()["starved"], 1)
        s.q.remove(r)
        r["status"] = "completed"
        r["start"], r["wait"] = s.t, 10.0
        self.assertEqual(s.metrics()["starved"], 0)

    def test_fairness_is_one_when_patients_wait_equally(self):
        s = A.Sim(dict(), store=None, workload=wl())
        for i, p in enumerate(("Patient 1", "Patient 2")):
            r = s.new("WATER", p, "manual")
            r.update(status="completed", start=3.0, wait=3.0, end=4.0)
            s.q.remove(r)
        self.assertEqual(s.metrics()["fairness"], 1.0)

    def test_utilisation_is_between_zero_and_one(self):
        m = run([(0.05, "FOOD", 3.0)], "priority").metrics()
        self.assertTrue(0 < m["util"] <= 1)


class Determinism(unittest.TestCase):
    def test_workload_is_reproducible_and_seed_dependent(self):
        a = A.make_workload(3, "moderate", 300)
        self.assertEqual(a, A.make_workload(3, "moderate", 300))
        self.assertNotEqual(a, A.make_workload(4, "moderate", 300))
        self.assertEqual(a, sorted(a, key=lambda w: w["arr"]))

    def test_policies_see_identical_requests_and_do_not_modify_workload(self):
        base = A.make_workload(5, "heavy", 300)
        snap = copy.deepcopy(base)
        results = {}
        for name, algo, pre in A.POLICIES:
            s = A.Sim(dict(algo=algo, preempt=pre, load="heavy", seed=5, duration=300), store=None, hist=False, workload=base)
            s.run_to_end()
            results[name] = [(r["cat"], r["pat"], r["arr"], r["svc"]) for r in s.R.values()]
        self.assertEqual(base, snap)
        first = next(iter(results.values()))
        for v in results.values():
            self.assertEqual(v, first)

    def test_same_run_twice_gives_same_metrics(self):
        base = A.make_workload(2, "heavy", 300)
        out = []
        for _ in range(2):
            s = A.Sim(dict(algo="priority_aging", preempt=True), store=None, hist=False, workload=base)
            s.run_to_end()
            out.append(s.metrics())
        self.assertEqual(out[0], out[1])

    def test_every_request_ends_in_exactly_one_final_state(self):
        for load in A.LOADS:
            for name, algo, pre in A.POLICIES:
                s = A.Sim(dict(algo=algo, preempt=pre), store=None, hist=False, workload=A.make_workload(1, load, 300))
                s.run_to_end()
                self.assertTrue(all(r["status"] in ("completed", "dropped", "cancelled") for r in s.R.values()), (load, name))
                self.assertEqual(sum(1 for r in s.R.values() if r["status"] == "processing"), 0)

    def test_priority_beats_fcfs_for_help_under_load(self):
        base = A.make_workload(1, "heavy", 600)
        got = {}
        for algo in ("fcfs", "priority"):
            s = A.Sim(dict(algo=algo, preempt=False), store=None, hist=False, workload=base)
            s.run_to_end()
            got[algo] = s.metrics()["emerg_wait"]
        self.assertLess(got["priority"], got["fcfs"])


class Workload(unittest.TestCase):
    P = lambda self, c, g: [dict(name="Patient 1", intervals=[3.0], correction_rate=c, gap_prob=g, gap_durations=[2.0])]

    def test_no_mistakes_means_no_cancellations(self):
        w = A.make_workload(1, "moderate", 600, self.P(0.0, 0.0))
        self.assertTrue(w and all(x["cancel"] is None for x in w))

    def test_every_selection_mistaken_creates_cancelled_wrong_request(self):
        w = A.make_workload(1, "moderate", 600, self.P(1.0, 0.0))
        wrong = [x for x in w if x["cancel"] is not None]
        self.assertEqual(len(wrong), len(w) - len(wrong))
        self.assertTrue(all(x["cat"] != "HELP" for x in wrong))
        self.assertTrue(all(x["cancel"] > x["arr"] for x in wrong))

    def test_selection_delay_comes_from_the_patient_profile(self):
        w = A.make_workload(1, "light", 600, self.P(0.0, 0.0))
        gaps = [b["arr"] - a["arr"] for a, b in zip(w, w[1:])]
        self.assertTrue(min(w, key=lambda x: x["arr"])["arr"] >= 3.0)      # every request is delayed by the 3 s selection time
        self.assertTrue(gaps)

    def test_tracker_gaps_add_delay(self):
        def prof(gap):
            return [dict(name="Patient 1", intervals=[3.0], correction_rate=0.0, gap_prob=1.0, gap_durations=[gap])]
        a = A.make_workload(1, "light", 600, prof(0.0))
        b = A.make_workload(1, "light", 600, prof(2.0))
        self.assertEqual(len(a), len(b))
        for x, y in zip(a, b):                                   # same random stream, every request 2 s later
            self.assertAlmostEqual(y["arr"] - x["arr"], 2.0)

    def test_shipped_profiles_are_valid(self):
        self.assertEqual(len(A.PROFILES), 5)
        for p in A.PROFILES:
            self.assertTrue(p["intervals"] and all(i > 0 for i in p["intervals"]))
            self.assertTrue(0 < p["correction_rate"] < 1 and 0 <= p["gap_prob"] <= 1)


class Logging(unittest.TestCase):
    def test_run_is_saved_to_sqlite(self):
        store = A.Store(os.path.join(_tmp, "log.db"))
        s = A.Sim(dict(algo="priority_aging", preempt=True, load="heavy", duration=120), store=store)
        s.run_to_end()
        run_row = store.all("SELECT * FROM runs")[0]
        self.assertEqual(run_row["status"], "finished")
        self.assertEqual(run_row["algo"], "priority_aging")
        reqs = store.all("SELECT * FROM requests WHERE run_id=?", (run_row["id"],))
        self.assertEqual(len(reqs), len(s.R))
        self.assertEqual({r["status"] for r in reqs} - {"completed", "dropped", "cancelled"}, set())
        for r in reqs:
            mem = s.R[r["req_id"]]
            self.assertEqual((r["status"], r["category"]), (mem["status"], mem["cat"]))
        self.assertGreater(store.all("SELECT COUNT(*) n FROM events")[0]["n"], len(reqs))
        import json
        self.assertEqual(json.loads(run_row["summary"])["completed"], s.metrics()["completed"])

    def test_reset_marks_unfinished_run_as_aborted(self):
        store = A.Store(os.path.join(_tmp, "abort.db"))
        s = A.Sim(dict(), store=store)
        s.open_run()
        s.step()
        s.reset()
        self.assertEqual(store.all("SELECT status FROM runs")[0]["status"], "aborted")


class Concurrency(unittest.TestCase):
    def test_store_survives_parallel_writers(self):
        store = A.Store(os.path.join(_tmp, "par.db"))
        def work(k):
            for i in range(50):
                store.run("INSERT INTO events(run_id,sim_time,wall,message) VALUES(?,?,?,?)", (1, i, "x", f"{k}-{i}"))
        ts = [threading.Thread(target=work, args=(k,)) for k in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(store.all("SELECT COUNT(*) n FROM events")[0]["n"], 400)

    def test_parallel_requests_get_unique_ids_and_none_are_lost(self):
        c0 = A.app.test_client()
        c0.post("/api/control", json=dict(action="reset"))
        errs = []
        def work():
            c = A.app.test_client()
            for _ in range(25):
                r = c.post("/api/request", json=dict(cat="WATER", patient="Patient 2"))
                if r.status_code != 200:
                    errs.append(r.status_code)
        ts = [threading.Thread(target=work) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        with A.LOCK:
            self.assertEqual(len(A.SIM.R), 200)
            self.assertEqual(A.SIM.nid, 201)
            rid = A.SIM.run_id
        n = A.STORE.all("SELECT COUNT(*) n FROM requests WHERE run_id=?", (rid,))[0]["n"]
        self.assertEqual(n, 200)
        c0.post("/api/control", json=dict(action="reset"))


class Api(unittest.TestCase):
    def setUp(self):
        self.c = A.app.test_client()
        self.c.post("/api/control", json=dict(action="reset"))

    def test_pages_render(self):
        for p in ("/", "/requests", "/performance", "/methodology"):
            r = self.c.get(p)
            self.assertEqual(r.status_code, 200, p)
        body = self.c.get("/").get_data(as_text=True).lower()
        self.assertNotIn("synthetic", body)
        self.assertNotIn("real data", body)

    def test_state_has_expected_fields(self):
        s = self.c.get("/api/state?all=1").get_json()
        for k in ("t", "running", "m", "queue", "caregivers", "alerts", "log", "hist", "cfg", "all", "patients", "cats"):
            self.assertIn(k, s)
        self.assertEqual(len(s["caregivers"]), s["cfg"]["caregivers"])
        self.assertEqual(len(s["cats"]), 8)

    def test_bad_settings_rejected(self):
        for bad in (dict(algo="lifo"), dict(caregivers=9), dict(speed=0), dict(load="huge"), dict(capacity="x")):
            r = self.c.post("/api/control", json=dict(action="config", **bad))
            self.assertEqual(r.status_code, 400, bad)

    def test_changing_a_setting_starts_a_new_run(self):
        self.c.post("/api/request", json=dict(cat="WATER", patient="Patient 1"))
        self.assertEqual(self.c.get("/api/state").get_json()["m"]["generated"], 1)
        s = self.c.post("/api/control", json=dict(action="config", algo="fcfs")).get_json()
        self.assertEqual(s["m"]["generated"], 0)
        self.assertEqual(s["cfg"]["algo"], "fcfs")
        self.c.post("/api/control", json=dict(action="config", algo="priority_aging"))

    def test_speed_change_does_not_restart(self):
        self.c.post("/api/request", json=dict(cat="WATER", patient="Patient 1"))
        self.c.post("/api/control", json=dict(action="config", speed="25"))
        self.assertEqual(self.c.get("/api/state").get_json()["m"]["generated"], 1)
        self.c.post("/api/control", json=dict(action="config", speed="10"))

    def test_manual_help_raises_alert_until_acknowledged(self):
        self.c.post("/api/request", json=dict(cat="HELP", patient="Patient 3"))
        s = self.c.get("/api/state").get_json()
        self.assertEqual(len(s["alerts"]), 1)
        self.c.post(f"/api/request/{s['alerts'][0]['id']}/ack")
        self.assertEqual(self.c.get("/api/state").get_json()["alerts"], [])

    def test_unknown_request_rejected(self):
        self.assertEqual(self.c.post("/api/request", json=dict(cat="PIZZA", patient="Patient 1")).status_code, 400)
        self.assertEqual(self.c.post("/api/request", json=dict(cat="HELP", patient="Nobody")).status_code, 400)

    def test_live_run_to_completion_is_logged_and_exportable(self):
        self.c.post("/api/control", json=dict(action="config", speed="200", duration="60", load="light"))
        self.c.post("/api/control", json=dict(action="start"))
        for _ in range(100):
            if self.c.get("/api/state").get_json()["finished"]:
                break
            time.sleep(0.1)
        s = self.c.get("/api/state").get_json()
        self.assertTrue(s["finished"])
        rid = s["run_id"]
        text = self.c.get(f"/export/requests.csv?run={rid}").get_data(as_text=True)
        rows = list(csv.DictReader(text.splitlines()))
        self.assertEqual(len(rows), s["m"]["generated"])
        runs = self.c.get("/api/runs").get_json()
        self.assertEqual(runs[0]["status"], "finished")
        self.c.post("/api/control", json=dict(action="config", speed="10", duration="600", load="moderate"))

    def test_experiment_endpoint_runs_all_policies_on_all_loads(self):
        r = self.c.post("/api/experiment", json=dict(seeds=1, duration=120, caregivers=2, capacity=20))
        self.assertEqual(r.status_code, 200)
        for _ in range(100):
            if not self.c.get("/api/experiment/status").get_json()["running"]:
                break
            time.sleep(0.1)
        e = self.c.get("/api/experiments").get_json()[0]
        self.assertEqual(len(e["results"]), len(A.LOADS) * len(A.POLICIES))
        text = self.c.get("/export/experiments.csv").get_data(as_text=True)
        self.assertEqual(len(list(csv.DictReader(text.splitlines()))), len(A.LOADS) * len(A.POLICIES))
        self.assertEqual(self.c.post("/api/experiment", json=dict(seeds=0)).status_code, 400)


class Profiles(unittest.TestCase):
    def write(self, rows, header="TimeStamp,x,y,character typing,sentence"):
        p = os.path.join(_tmp, f"p{time.time_ns()}_ET.csv")
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(header + "\n" + "\n".join(rows) + ("\n" if rows else ""))
        return p

    def test_selections_deletions_and_gaps_are_extracted(self):
        rows = ["0.0,-1,-1,EMPTY,MainMenu", "1.0,-1,-1,EMPTY,MainMenu", "2.0,0.5,0.5,x,Typing", "2.1,0.5,0.5,x,",
                "3.0,0.5,0.5,a,n", "6.0,0.5,0.5,a,nâ", "9.0,0.5,0.5,a,nâg", "12.0,0.5,0.5,a,nâ", "15.0,0.5,0.5,a,nân",
                "15.1,0.5,0.5,a,MainMenu"]
        p = BP.profile_from_csv(self.write(rows))
        self.assertEqual(p["n_selections"], 5)                        # n, nâ, nâg, nâ (deletion), nân
        self.assertEqual(p["intervals"], [3.0, 3.0, 3.0, 3.0])
        self.assertEqual(p["deletions"], 1)
        self.assertGreater(p["gap_prob"], 0)                          # 2.1 -> 3.0 is a 0.9 s gap
        self.assertAlmostEqual(p["correction_rate"], 1.5 / 5)

    def test_first_letter_is_not_counted_as_deletion(self):
        rows = ["0,0.5,0.5,x,Typing", "0.1,0.5,0.5,x,", "1,0.5,0.5,x,n", "4,0.5,0.5,x,nâ", "7,0.5,0.5,x,nân"]
        self.assertEqual(BP.profile_from_csv(self.write(rows))["deletions"], 0)

    def test_malformed_and_empty_files_are_reported_not_skipped(self):
        with self.assertRaises(ValueError):
            BP.profile_from_csv(self.write(["1,2,3"], header="a,b,c"))
        with self.assertRaises(ValueError):
            BP.profile_from_csv(self.write([]))
        with self.assertRaises(ValueError):
            BP.profile_from_csv(self.write(["0,0.5,0.5,x,Typing", "1,0.5,0.5,x,n"]))      # too few selections


if __name__ == "__main__":
    unittest.main()
