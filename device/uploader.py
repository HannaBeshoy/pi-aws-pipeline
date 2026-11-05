#!/usr/bin/env python3
"""uploader.py - on the Raspberry Pi: send the sensor logger's readings to Amazon S3.

    python3 device/uploader.py --logs ~/sensor-logs --spool ~/.sensor-spool --bucket BUCKET \
        --device pi-01 [--tz Europe/Budapest] [--once | --interval 300] [--flush]

Reads the CSV files written by pi-serial-logger (time,seq,uptime_ms,temp_c,dist_cm,light_pct),
groups the readings by minute and, for every complete minute, writes them to the spool as
gzip-compressed JSON lines, then uploads the spool to

    s3://BUCKET/raw/device=pi-01/date=2026-11-05/pi-01_20261105T190000Z_20261105T190459Z.jsonl.gz

Designed for a device with an unreliable network:

- Nothing is lost when the network is down: files wait in the spool and are sent oldest first
  when it comes back (with exponential backoff in --interval mode).
- Nothing is sent twice after a crash: the spool file's name is made from the time range it
  holds, so redoing a step writes the same name again instead of a second copy; the read
  offsets are saved only after the spool file is safely written (write to .tmp, then rename).
- A minute is uploaded only when it is complete (a reading from a later minute has arrived),
  so the cloud side never sees one minute split over two files in normal operation. The
  unfinished minute is kept in pending.jsonl. --flush sends it anyway (at shutdown).
- Each upload carries a SHA-256 checksum that S3 verifies.
"""
import argparse, datetime as dt, glob, gzip, json, os, sys, time
from zoneinfo import ZoneInfo

FIELDS = ("time", "seq", "uptime_ms", "temp_c", "dist_cm", "light_pct")


def parse_line(line, tz):
    """One CSV line of the logger -> reading dict with UTC time, or None if malformed."""
    parts = line.strip().split(",")
    if len(parts) != 6 or parts[0] == "time":
        return None
    try:
        local = dt.datetime.fromisoformat(parts[0]).replace(tzinfo=tz)
        return {
            "ts": local.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "seq": int(parts[1]),
            "uptime_ms": int(parts[2]),
            "temp_c": float(parts[3]),
            "dist_cm": float(parts[4]) if parts[4] else None,
            "light_pct": float(parts[5]),
        }
    except ValueError:
        return None


def minute_of(r):
    return r["ts"][:16]  # 2026-11-05T19:00


def _atomic_write(path, data: bytes):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Uploader:
    def __init__(self, logs, spool, device, tz, s3=None, bucket=None, log=print):
        self.logs, self.spool, self.device, self.tz = logs, spool, device, tz
        self.s3, self.bucket, self.log = s3, bucket, log
        os.makedirs(spool, exist_ok=True)
        self.state_path = os.path.join(spool, "state.json")
        self.pending_path = os.path.join(spool, "pending.jsonl")
        self.stats = {"read": 0, "bad": 0, "spooled": 0, "uploaded": 0, "failed": 0}

    # --- reading the logger's files -------------------------------------------------
    def _load_state(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {"offsets": {}}

    def _load_pending(self):
        try:
            with open(self.pending_path) as f:
                return [json.loads(l) for l in f if l.strip()]
        except FileNotFoundError:
            return []

    def read_new(self, state):
        """New complete lines from every CSV since the saved offsets. Returns readings and offsets."""
        offsets = dict(state["offsets"])
        out = []
        for path in sorted(glob.glob(os.path.join(self.logs, "*.csv"))):
            name = os.path.basename(path)
            with open(path, "rb") as f:
                f.seek(offsets.get(name, 0))
                data = f.read()
            end = data.rfind(b"\n") + 1          # a half-written last line waits for the next run
            for raw in data[:end].decode(errors="replace").splitlines():
                if not raw or raw.startswith("time,"):
                    continue
                r = parse_line(raw, self.tz)
                if r is None:
                    self.stats["bad"] += 1
                else:
                    out.append(r)
            offsets[name] = offsets.get(name, 0) + end
        self.stats["read"] += len(out)
        return out, offsets

    # --- spooling -----------------------------------------------------------------------------
    def spool_step(self, flush=False):
        state = self._load_state()
        new, offsets = self.read_new(state)
        readings = self._load_pending() + new
        readings.sort(key=lambda r: (r["ts"], r["uptime_ms"]))
        if readings:
            last_minute = minute_of(readings[-1])
            done = readings if flush else [r for r in readings if minute_of(r) < last_minute]
            keep = [] if flush else [r for r in readings if minute_of(r) >= last_minute]
        else:
            done, keep = [], []
        if done:
            first, last = done[0]["ts"], done[-1]["ts"]
            name = f"{self.device}_{first.replace('-', '').replace(':', '')}_{last.replace('-', '').replace(':', '')}.jsonl.gz"
            body = "".join(json.dumps(dict(r, device=self.device), separators=(",", ":")) + "\n" for r in done)
            _atomic_write(os.path.join(self.spool, name), gzip.compress(body.encode(), mtime=0))
            self.stats["spooled"] += len(done)
        # only now that the readings are safe in the spool, move the offsets on
        _atomic_write(self.pending_path, "".join(json.dumps(r) + "\n" for r in keep).encode())
        _atomic_write(self.state_path, json.dumps({"offsets": offsets}).encode())
        return len(done)

    def spool_files(self):
        return sorted(p for p in glob.glob(os.path.join(self.spool, "*.jsonl.gz")))

    # --- uploading ----------------------------------------------------------------------------
    def key_for(self, path):
        name = os.path.basename(path)
        day = name.split("_")[1][:8]
        return f"raw/device={self.device}/date={day[:4]}-{day[4:6]}-{day[6:]}/{name}"

    def upload_all(self):
        """Upload the spool oldest first; stop at the first failure (order is kept). True if all sent."""
        for path in self.spool_files():
            with open(path, "rb") as f:
                body = f.read()
            try:
                self.s3.put_object(Bucket=self.bucket, Key=self.key_for(path), Body=body,
                                   ContentType="application/x-ndjson", ContentEncoding="gzip",
                                   ChecksumAlgorithm="SHA256")
            except Exception as e:  # network down, expired credentials, throttling ...
                self.stats["failed"] += 1
                self.log(f"upload failed, {len(self.spool_files())} file(s) wait in the spool: {e}")
                return False
            os.remove(path)
            self.stats["uploaded"] += 1
            self.log(f"uploaded {self.key_for(path)}")
        return True

    def run_once(self, flush=False):
        self.spool_step(flush)
        return self.upload_all()

    def run_forever(self, interval, max_backoff=900):
        delay = interval
        while True:
            ok = self.run_once()
            delay = interval if ok else min(max(delay * 2, interval), max_backoff)
            time.sleep(delay)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", required=True, help="the logger's CSV directory")
    ap.add_argument("--spool", required=True, help="where files wait until they are uploaded")
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--device", default="pi-01")
    ap.add_argument("--tz", default=None, help="time zone of the logger's times (default: this machine's)")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true")
    g.add_argument("--interval", type=float, default=300, help="seconds between uploads (cost vs delay: docs/COST.md)")
    ap.add_argument("--flush", action="store_true", help="also send the unfinished minute")
    a = ap.parse_args(argv)
    import boto3
    tz = ZoneInfo(a.tz) if a.tz else dt.datetime.now().astimezone().tzinfo
    up = Uploader(a.logs, a.spool, a.device, tz, boto3.client("s3", region_name=a.region), a.bucket)
    if a.once or a.flush:
        ok = up.run_once(flush=a.flush)
        print(json.dumps(up.stats))
        return 0 if ok else 1
    up.run_forever(a.interval)


if __name__ == "__main__":
    sys.exit(main())
