"""Build patient profiles (profiles.json) from the eye-tracking ET.csv files in ./data.

Run:  python build_profiles.py [data_dir]

Per file (one file = one patient) we extract, from the typing part of the recording only:
  intervals       seconds between consecutive confirmed selections (a selection = the typed text changed)
  correction_rate share of selections that deleted text (backspace / clear), Jeffreys-smoothed
  gap_prob        share of intervals during which the tracker lost the eye for > 0.5 s
  gap_durations   lengths (s) of those tracker gaps

The columns are looked up by name and the script stops with a clear message if they are missing.
The original CSV files are only read, never modified.
"""
import csv
import glob
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
GAP = 0.5  # seconds between samples that counts as a tracker gap


def profile_from_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        rd = csv.DictReader(fh)
        need = {"TimeStamp", "sentence"}
        if not need <= set(rd.fieldnames or []):
            raise ValueError(f"{os.path.basename(path)}: needs columns {sorted(need)}, found {rd.fieldnames}")
        rows = [(float(r["TimeStamp"]), r["sentence"]) for r in rd if r["TimeStamp"]]
    if len(rows) < 2:
        raise ValueError(f"{os.path.basename(path)}: too few rows")
    # typing part = rows after the idle menu; the label 'Typing' is only a state marker, not text
    typing = [(t, s) for t, s in rows if s != "MainMenu"]
    events, prev = [], None            # (time, old_text, new_text)
    for t, s in typing:
        if s in ("", "Typing"):        # no text yet / state marker
            if s == "" and prev not in (None, ""):
                events.append((t, prev, ""))   # the whole text was cleared: a deletion selection
                prev = ""
            continue
        if prev is None:               # first letter: start of the sequence, not a change of text
            events.append((t, "", s)); prev = s; continue
        if s != prev:
            events.append((t, prev, s)); prev = s
    if len(events) < 3:
        raise ValueError(f"{os.path.basename(path)}: fewer than 3 selections found")
    times = [e[0] for e in events]
    intervals = [round(b - a, 3) for a, b in zip(times, times[1:])]
    deletions = sum(1 for _, old, new in events[1:] if len(new) < len(old))
    n = len(events) - 1
    # gaps between consecutive typing samples
    ts = [t for t, _ in typing]
    gaps = [(a, b) for a, b in zip(ts, ts[1:]) if b - a > GAP]
    gap_durations = [round(b - a, 3) for a, b in gaps]
    with_gap = sum(1 for a, b in zip(times, times[1:]) if any(a <= g0 and g1 <= b for g0, g1 in gaps))
    return dict(
        source_file=os.path.basename(path), n_selections=len(events), typing_seconds=round(ts[-1] - ts[0], 1),
        intervals=intervals, deletions=deletions,
        correction_rate=round((deletions + 0.5) / (n + 1), 4),          # Jeffreys smoothing: never exactly 0
        gap_prob=round(with_gap / len(intervals), 4), gap_durations=gap_durations or [0.0],
    )


def main(data_dir):
    files = sorted(glob.glob(os.path.join(data_dir, "*ET*.csv")))
    if not files:
        sys.exit(f"No ET csv files found in {data_dir}")
    patients, problems = [], []
    for f in files:
        try:
            p = profile_from_csv(f)
        except Exception as e:           # report, never skip silently
            problems.append(str(e)); continue
        p["name"] = f"Patient {len(patients) + 1}"
        patients.append(p)
    out = os.path.join(BASE, "profiles.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(dict(patients=patients, problems=problems), fh, indent=1, ensure_ascii=False)
    print(f"wrote {out}")
    print(f"{'patient':10} {'file':16} {'select':>6} {'typing_s':>8} {'median_gap':>10} {'p90_gap':>8} {'deletions':>9} {'corr_rate':>9} {'gap_prob':>8}")
    for p in patients:
        iv = sorted(p["intervals"])
        print(f"{p['name']:10} {p['source_file']:16} {p['n_selections']:6d} {p['typing_seconds']:8.1f} "
              f"{iv[len(iv)//2]:10.2f} {iv[int(.9*(len(iv)-1))]:8.2f} {p['deletions']:9d} {p['correction_rate']:9.3f} {p['gap_prob']:8.3f}")
    for pr in problems:
        print("UNUSABLE:", pr)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "data"))
