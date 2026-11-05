"""The device side: reading the logger's files, the spool, and uploads over a bad network."""
import datetime as dt, gzip, json, os
from zoneinfo import ZoneInfo

import boto3, pytest
from moto import mock_aws

from helpers import ROOT  # noqa: F401  (sets sys.path)
import sim_readings
from uploader import Uploader, parse_line

BUD = ZoneInfo("Europe/Budapest")
START = dt.datetime(2026, 11, 5, 20, 0, 0)


class FlakyS3:
    """Stands in for S3: fails while .down is True, records every upload."""
    def __init__(self):
        self.down, self.objects, self.calls = False, {}, 0

    def put_object(self, **kw):
        self.calls += 1
        if self.down:
            raise ConnectionError("network is unreachable")
        self.objects[kw["Key"]] = kw["Body"]


def readings_in(objects):
    out = []
    for body in objects.values():
        out += [json.loads(l) for l in gzip.decompress(body).decode().splitlines()]
    return out


def make(tmp_path, s3=None):
    return Uploader(str(tmp_path / "logs"), str(tmp_path / "spool"), "pi-01", BUD, s3 or FlakyS3(), "b", log=lambda m: None)


def test_parse_line_converts_local_time_to_utc():
    r = parse_line("2026-11-05T20:00:01,7,1400,23.5,55.2,70", BUD)          # CET = UTC+1 in November
    assert r == {"ts": "2026-11-05T19:00:01Z", "seq": 7, "uptime_ms": 1400, "temp_c": 23.5, "dist_cm": 55.2, "light_pct": 70.0}
    assert parse_line("2026-07-05T20:00:01,7,1400,23.5,,70", BUD)["ts"] == "2026-07-05T18:00:01Z"   # CEST, no echo
    assert parse_line("2026-07-05T20:00:01,7,1400,23.5,,70", BUD)["dist_cm"] is None


@pytest.mark.parametrize("bad", ["time,seq,uptime_ms,temp_c,dist_cm,light_pct", "2026-11-05T20:00:01,7,1400,23.5,55",
                                 "2026-11-05T20:00:01,x,1400,23.5,55,70", "garbage", ""])
def test_parse_line_rejects_malformed(bad):
    assert parse_line(bad, BUD) is None


def test_only_complete_minutes_are_spooled(tmp_path):
    sim_readings.write(tmp_path / "logs", START, minutes=3.5)               # 20:00:00 .. 20:03:29
    up = make(tmp_path)
    up.spool_step()
    rs = [json.loads(l) for f in up.spool_files() for l in gzip.decompress(open(f, "rb").read()).decode().splitlines()]
    assert {r["ts"][:16] for r in rs} == {"2026-11-05T19:00", "2026-11-05T19:01", "2026-11-05T19:02"}
    assert len(rs) == 3 * 300                                               # 5 per second, all kept
    pending = open(tmp_path / "spool" / "pending.jsonl").read().splitlines()
    assert len(pending) == 150                                              # the unfinished minute waits
    up.spool_step(flush=True)
    assert len(open(tmp_path / "spool" / "pending.jsonl").read()) == 0


def test_a_half_written_line_waits_for_the_next_run(tmp_path):
    os.makedirs(tmp_path / "logs")
    p = tmp_path / "logs" / "2026-11-05.csv"
    p.write_text("time,seq,uptime_ms,temp_c,dist_cm,light_pct\n2026-11-05T20:00:00,0,0,23.0,50.0,70\n2026-11-05T20:00:00,1,2")
    up = make(tmp_path)
    up.spool_step(flush=True)
    assert up.stats["read"] == 1 and up.stats["bad"] == 0
    with open(p, "a") as f:
        f.write("00,23.1,50.0,70\n")                                        # the writer finishes the line
    up.spool_step(flush=True)
    assert up.stats["read"] == 2


def test_nothing_is_lost_while_the_network_is_down(tmp_path):
    s3 = FlakyS3()
    up = make(tmp_path, s3)
    s3.down = True
    for k in range(3):                                                      # three runs, ten minutes each, offline
        sim_readings.write(tmp_path / "logs", START + dt.timedelta(minutes=10 * k), minutes=10, seed=k)
        assert up.run_once() is False
    waiting = up.spool_files()
    assert len(waiting) == 3 and not s3.objects
    s3.down = False
    assert up.run_once(flush=True) is True
    assert not up.spool_files()
    rs = readings_in(s3.objects)
    assert len(rs) == 3 * 10 * 60 * 5
    assert len({(r["ts"], r["uptime_ms"], r["seq"]) for r in rs}) == len(rs)    # no duplicates
    keys = list(s3.objects)
    assert keys == sorted(keys)                                             # sent oldest first


def test_a_crash_before_the_offsets_are_saved_does_not_duplicate(tmp_path):
    sim_readings.write(tmp_path / "logs", START, minutes=5)
    up = make(tmp_path)
    up.spool_step()
    first = up.spool_files()
    # pretend the program died after writing the spool file but before saving the offsets:
    os.remove(tmp_path / "spool" / "state.json")
    os.remove(tmp_path / "spool" / "pending.jsonl")
    up2 = make(tmp_path)
    up2.spool_step()
    assert up2.spool_files() == first                                       # the same name again, not a second file


def test_upload_key_layout_and_content_with_s3(tmp_path):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="sensor-data")
        sim_readings.write(tmp_path / "logs", START, minutes=2)
        up = make(tmp_path, s3)
        up.bucket = "sensor-data"
        assert up.run_once(flush=True)
        keys = [o["Key"] for o in s3.list_objects_v2(Bucket="sensor-data")["Contents"]]
        assert keys == ["raw/device=pi-01/date=2026-11-05/pi-01_20261105T190000Z_20261105T190159Z.jsonl.gz"]
        obj = s3.get_object(Bucket="sensor-data", Key=keys[0], ChecksumMode="ENABLED")
        assert "ChecksumSHA256" in obj
        rs = [json.loads(l) for l in gzip.decompress(obj["Body"].read()).decode().splitlines()]
        assert len(rs) == 600 and all(r["device"] == "pi-01" for r in rs)
