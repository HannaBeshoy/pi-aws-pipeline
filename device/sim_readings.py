#!/usr/bin/env python3
"""sim_readings.py - write log files in the format of pi-serial-logger, for testing without the Pi.

    python3 device/sim_readings.py OUT_DIR --start 2026-11-05T20:00:00 --minutes 30 [--hz 5]

One CSV per day, header time,seq,uptime_ms,temp_c,dist_cm,light_pct, local time to the second,
like the logger's writer. The temperature rises from 23 to 31 C over the run (to trigger the
alarm), the distance swings, and about 1 line in 50 has no distance (no echo).
"""
import argparse, datetime as dt, math, os, random


def lines(start, minutes, hz=5, seed=1):
    rnd = random.Random(seed)
    n = int(minutes * 60 * hz)
    for i in range(n):
        t = i / hz
        when = start + dt.timedelta(seconds=t)
        temp = 23 + 8 * (1 - math.exp(-t / 600)) + rnd.gauss(0, 0.1)
        dist = 55 + 25 * math.sin(2 * math.pi * t / 12) + rnd.gauss(0, 0.3)
        light = max(0.0, 70 - t / 60)
        d = "" if rnd.random() < 0.02 else f"{dist:.1f}"
        yield when, f"{when:%Y-%m-%dT%H:%M:%S},{i},{int(t * 1000)},{temp:.1f},{d},{light:.0f}"


def write(out_dir, start, minutes, hz=5, seed=1):
    os.makedirs(out_dir, exist_ok=True)
    files = {}
    for when, line in lines(start, minutes, hz, seed):
        path = os.path.join(out_dir, f"{when:%Y-%m-%d}.csv")
        if path not in files:
            files[path] = open(path, "a")
            if files[path].tell() == 0:
                files[path].write("time,seq,uptime_ms,temp_c,dist_cm,light_pct\n")
        files[path].write(line + "\n")
    for f in files.values():
        f.close()
    return sorted(files)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--start", default="2026-11-05T20:00:00")
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--hz", type=float, default=5)
    a = ap.parse_args()
    for p in write(a.out_dir, dt.datetime.fromisoformat(a.start), a.minutes, a.hz):
        print(p)
