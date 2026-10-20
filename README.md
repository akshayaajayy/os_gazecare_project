# GazeCare OS - ward request scheduling

A Flask + SQLite prototype. Five patients send care requests (HELP, PAIN, WASHROOM, MEDICINE, WATER, FOOD,
REPOSITION, BLANKET). The requests enter one bounded queue and one to three caregivers serve them under FCFS,
Priority, or Priority + Aging, with optional preemption. Every run is logged to SQLite.

**Research prototype. Not a medical device and not validated for patient care.**

## Windows setup and commands

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

python app.py                                          :: web app at http://127.0.0.1:5000
python -m unittest discover -s tests -v                :: automated tests (51)
python app.py --experiments                            :: experiment suite (about 5 s); writes results\*.csv and the database
python build_profiles.py "C:\path\to\ET_csv_folder"    :: rebuild profiles.json from your own ET.csv files (optional)
```

## Files

| File | Purpose |
|---|---|
| `app.py` | Simulation, scheduler, SQLite logging, API, experiment runner (your original `app.py`, extended) |
| `build_profiles.py` | Reads the eye-tracking CSVs in `data/` and writes `profiles.json` |
| `profiles.json` | One profile per patient: selection gaps, correction rate, tracker-dropout behaviour |
| `templates/`, `static/` | Four pages: Dashboard, Requests, Performance, Methodology |
| `tests/test_gazecare.py` | Automated tests |
| `results/` | `experiment_runs.csv` (every run) and `experiment_summary.csv` (averages) |
| `gazecare.db` | SQLite database (created on first run) |

## Database (`gazecare.db`)

`runs` (one row per run with its configuration and summary), `requests` (every request with arrival, first start,
finish, wait, status, preemptions, acknowledgement), `events` (the event log), `experiments` and `experiment_runs`.
The Requests page lists saved runs and downloads CSVs.

## How the dataset is used

The dataset files are eye-tracking recordings of five participants typing one phrase on an on-screen keyboard.
They contain no hospital requests. `build_profiles.py` takes from each file, for the typing part only:

* the seconds between consecutive confirmed selections (the participant's selection speed),
* the share of selections that deleted text (correction rate, smoothed so it is never exactly 0),
* how often the tracker lost the eyes for more than 0.5 s, and how long each gap lasted.

The simulator uses these per patient: a request reaches the queue after a selection time drawn from that patient's own
gaps; with the patient's correction rate the first selection is a mistake, so a wrong request is sent, retracted and
replaced; tracker gaps add delay. **Not taken from the data** (modelling choices in `CATS` and `LOADS` in `app.py`):
how often patients need care, which request they pick, the priority levels, and caregiver service times.
The website does not label data origin, as requested; the `origin` column in the database marks `patient` (generated)
versus `manual` (sent from the dashboard).

## Metrics (simulated seconds)

* **Wait**: arrival in the queue to the caregiver first starting on the request. Service time is not included.
* **HELP + PAIN wait**: mean wait of those two categories. **Routine** = level 4.
* **Waited > 60 s**: requests whose first-service wait exceeded 60 s. An indicator of starvation, not proof.
* **Fairness**: Jain's index over the patients' mean waits. It is noisy at light load, where there are few requests per patient.
* **Caregiver busy**: share of caregiver time spent serving, including work lost to cancellations.

## Results (10 runs per setting, 2 caregivers, queue size 20, 600 s of arrivals)

Every policy sees identical requests in each run. Means over runs; from `results/experiment_summary.csv`.

| Load | Policy | Avg wait | HELP wait | HELP+PAIN wait | Routine longest | Waited >60 s | Dropped | Preemptions |
|---|---|---|---|---|---|---|---|---|
| moderate | FCFS | 5.2 | 4.0 | 5.0 | 22.4 | 0.0 | 0.0 | 0 |
| moderate | Priority | 6.0 | 1.3 | 1.8 | 34.7 | 0.1 | 0.0 | 0 |
| moderate | Priority + Aging | 5.9 | 1.4 | 1.8 | 30.4 | 0.1 | 0.0 | 0 |
| moderate | Priority + Preemption | 5.7 | 0.0 | 0.2 | 36.4 | 0.1 | 0.0 | 27.9 |
| moderate | Priority + Aging + Preemption | 5.5 | 0.0 | 0.2 | 33.4 | 0.1 | 0.0 | 30.7 |
| heavy | FCFS | 29.0 | 26.9 | 26.6 | 59.9 | 14.4 | 1.5 | 0 |
| heavy | Priority | 33.2 | 2.2 | 2.8 | 105.7 | 38.8 | 2.7 | 0 |
| heavy | Priority + Aging | 32.2 | 2.5 | 2.9 | 79.8 | 35.7 | 2.6 | 0 |
| heavy | Priority + Preemption | 34.5 | 0.0 | 0.5 | 109.9 | 42.7 | 4.5 | 54.9 |
| heavy | Priority + Aging + Preemption | 32.8 | 0.0 | 0.4 | 83.5 | 38.4 | 3.5 | 63.9 |
| overload | FCFS | 60.6 | 64.3 | 61.0 | 86.6 | 123.2 | 85.8 | 0 |
| overload | Priority | 61.8 | 2.7 | 3.7 | 168.3 | 85.6 | 90.3 | 0 |
| overload | Priority + Aging | 61.3 | 2.5 | 3.8 | 137.2 | 102.6 | 88.8 | 0 |
| overload | Priority + Preemption | 60.0 | 0.0 | 1.0 | 177.2 | 82.6 | 91.8 | 71.4 |
| overload | Priority + Aging + Preemption | 60.1 | 0.0 | 1.0 | 134.5 | 102.2 | 91.4 | 80.4 |

What this shows:

* Priority scheduling cuts HELP and PAIN waits from tens of seconds (FCFS) to about 2-3 s under heavy load.
  Preemption brings HELP to zero wait at the price of 55-64 interruptions per run.
* Pure priority makes the longest routine wait much worse than FCFS (106 s vs 60 s, heavy). Aging reduces it to about
  80 s without raising emergency waits. It does not remove long waits: under overload the real fix is more caregivers.
* The aging floor decides the trade-off. With floor 3 (default), aged routine requests never overtake HELP, PAIN or
  WASHROOM. A floor of 2 shortens routine waits more (about 61 s longest, heavy) but lets them overtake PAIN
  (HELP+PAIN mean about 17-21 s). Change `AGING_FLOOR` in `app.py` to try it.
* The average wait barely differs between policies: scheduling mainly moves waiting time between categories.

## Limitations

* Request categories, levels, service times and need rates are assumptions, not clinical or measured values.
* Each patient profile comes from one recording with 11-28 selections, so the fitted values are rough.
* The simulation advances in 0.05 s ticks on one clock thread; web-server threads and the clock share one lock.
  Preemption is exact at tick resolution. There are no condition variables or blocking queues, no gaze-sample
  producer-consumer buffer, and no cache. The bounded queue holds requests, not gaze samples.
* The HELP interrupt is an application-level event flag, not a hardware interrupt.
* A wrong request is never HELP, so false emergencies are not modelled.
