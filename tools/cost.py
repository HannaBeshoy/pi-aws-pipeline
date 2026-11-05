#!/usr/bin/env python3
"""cost.py - monthly cost of the pipeline, from list prices and the measured file sizes.

    python3 tools/cost.py            # prints the tables of docs/COST.md
    python3 tools/cost.py --devices 10 --interval 60

Prices: us-east-1 (the AWS Academy region), list prices without the free tier, as published
in November 2026. They change: check them in the AWS Pricing Calculator before relying on them.
The size of one reading in S3 is measured by compressing a simulated hour, not assumed.
"""
import argparse, datetime as dt, gzip, json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "device"))
import sim_readings  # noqa: E402

PRICES = {  # USD, us-east-1
    "s3_put_per_1000": 0.005,
    "s3_get_per_1000": 0.0004,
    "s3_standard_gb_month": 0.023,
    "s3_glacier_ir_gb_month": 0.004,
    "lambda_per_million_requests": 0.20,
    "lambda_gb_second_arm64": 0.0000133334,
    "ddb_per_million_wru": 0.625,
    "ddb_per_million_rru": 0.125,
    "ddb_gb_month": 0.25,
    "cw_custom_metric_month": 0.30,
    "cw_alarm_month": 0.10,
    "cw_logs_ingest_gb": 0.50,
}
FREE_TIER = "Always free each month: 1 million Lambda requests and 400,000 GB-s, 25 GB of DynamoDB storage, 10 custom metrics and 10 alarms in CloudWatch, 5 GB of log ingestion."
DAYS = 30
HZ = 5


def bytes_per_reading():
    rs = []
    for when, line in sim_readings.lines(dt.datetime(2026, 11, 5, 20), minutes=60, hz=HZ):
        t, seq, up, temp, dist, light = line.split(",")
        rs.append(json.dumps({"ts": t + "Z", "seq": int(seq), "uptime_ms": int(up), "temp_c": float(temp),
                              "dist_cm": float(dist) if dist else None, "light_pct": float(light),
                              "device": "pi-01"}, separators=(",", ":")))
    raw = ("\n".join(rs) + "\n").encode()
    return len(raw) / len(rs), len(gzip.compress(raw)) / len(rs)


def monthly(devices=1, interval_s=300, lambda_s=0.4, lambda_mb=256, keep_months=12, per_reading_ddb=False):
    p = PRICES
    _, gz = bytes_per_reading()
    readings = devices * HZ * 86400 * DAYS
    files = devices * 86400 * DAYS / interval_s
    minutes = devices * 1440 * DAYS
    gb_month = readings * gz / 1e9
    c = {}
    c["S3 requests (PUT by the Pi, GET by Lambda)"] = files * (p["s3_put_per_1000"] + p["s3_get_per_1000"]) / 1000
    # steady state after keep_months: the newest month in Standard, the rest in Glacier Instant Retrieval
    c["S3 storage (1 month Standard, then Glacier IR)"] = gb_month * (p["s3_standard_gb_month"] + (keep_months - 1) * p["s3_glacier_ir_gb_month"])
    c["Lambda (requests and duration)"] = files * p["lambda_per_million_requests"] / 1e6 + files * lambda_s * lambda_mb / 1024 * p["lambda_gb_second_arm64"]
    if per_reading_ddb:
        wru, rru, items = readings, 0, readings * 90 / DAYS
        item_kb = 0.2
    else:  # per minute: one consistent read + one write, plus LATEST once per file
        wru, rru, items = minutes + files, minutes, minutes * 90 / DAYS
        item_kb = 0.5
    c["DynamoDB requests"] = wru * p["ddb_per_million_wru"] / 1e6 + rru * p["ddb_per_million_rru"] / 1e6
    c["DynamoDB storage (90 days)"] = items * item_kb / 1e6 * p["ddb_gb_month"]
    c["CloudWatch (2 custom metrics, 2 alarms, logs)"] = 2 * p["cw_custom_metric_month"] + 2 * p["cw_alarm_month"] + files * 600 / 1e9 * p["cw_logs_ingest_gb"]
    return c, {"readings": readings, "files": files, "gb_month": gb_month, "gz_bytes": gz}


def table(c):
    lines = ["| Item | USD / month |", "| --- | ---: |"]
    lines += [f"| {k} | {v:.4f} |" for k, v in c.items()]
    lines.append(f"| **total** | **{sum(c.values()):.2f}** |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--devices", type=int, default=1)
    ap.add_argument("--interval", type=float, default=300)
    a = ap.parse_args()
    raw, gz = bytes_per_reading()
    print(f"one reading: {raw:.0f} bytes as JSON, {gz:.1f} bytes gzip-compressed ({raw / gz:.1f}x)\n")
    c, info = monthly(a.devices, a.interval)
    print(f"{a.devices} device(s), {HZ} readings/s, upload every {a.interval:.0f} s: "
          f"{info['readings'] / 1e6:.1f} M readings, {info['files']:.0f} files, {info['gb_month']:.2f} GB a month\n")
    print(table(c))
    print("\nOne DynamoDB item per reading instead of per minute:\n")
    print(table(monthly(a.devices, a.interval, per_reading_ddb=True)[0]))
    print("\nTotal by number of devices and upload interval:\n")
    print("| Devices | every 60 s | every 300 s |\n| ---: | ---: | ---: |")
    for d in (1, 10, 100):
        print(f"| {d} | {sum(monthly(d, 60)[0].values()):.2f} | {sum(monthly(d, 300)[0].values()):.2f} |")
    print("\n" + FREE_TIER)


if __name__ == "__main__":
    main()
