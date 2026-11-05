"""The cloud side with moto (an AWS simulator): ingest Lambda, API Lambda, the whole chain."""
import datetime as dt, gzip, json, os, statistics
from zoneinfo import ZoneInfo

import boto3, pytest
from moto import mock_aws

from helpers import load_module
import sim_readings
from uploader import Uploader

ingest = load_module("ingest", "lambda/ingest/handler.py")
api = load_module("api", "lambda/api/handler.py")
BUCKET, TABLE = "sensor-pipeline-data", "sensor-pipeline-readings"
START = dt.datetime(2026, 11, 5, 20, 0, 0)


@pytest.fixture
def aws(monkeypatch):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "device", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "device", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}])
        sns = boto3.client("sns", region_name="us-east-1")
        sqs = boto3.client("sqs", region_name="us-east-1")
        topic = sns.create_topic(Name="alarms")["TopicArn"]
        q = sqs.create_queue(QueueName="inbox")["QueueUrl"]
        qarn = sqs.get_queue_attributes(QueueUrl=q, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=qarn)
        monkeypatch.setenv("TABLE", TABLE)
        monkeypatch.setenv("ALARM_TOPIC_ARN", topic)
        monkeypatch.setattr(ingest, "_s3", None)
        monkeypatch.setattr(api, "_table", None)
        yield {"s3": s3, "table": table, "sns": sns, "sqs": sqs, "queue": q}


def put(aws, key, readings):
    body = gzip.compress("".join(json.dumps(r) + "\n" for r in readings).encode())
    aws["s3"].put_object(Bucket=BUCKET, Key=key, Body=body)
    return {"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {"key": key}}}]}


def sim(minutes=3, start=START, seed=1):
    out = []
    for when, line in sim_readings.lines(start, minutes, seed=seed):
        t, seq, up, temp, dist, light = line.split(",")
        out.append({"device": "pi-01", "ts": (when - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "seq": int(seq), "uptime_ms": int(up), "temp_c": float(temp),
                    "dist_cm": float(dist) if dist else None, "light_pct": float(light)})
    return out


KEY = "raw/device=pi-01/date=2026-11-05/pi-01_a.jsonl.gz"


def minute_items(table):
    return sorted((i for i in table.scan()["Items"] if i["sk"].startswith("M#")), key=lambda i: i["sk"])


def test_summaries_match_the_readings(aws):
    rs = sim(3)
    out = ingest.handler(put(aws, KEY, rs))["processed"][0]
    assert out["readings"] == 900 and out["bad"] == 0 and out["merged"] == 3
    items = minute_items(aws["table"])
    assert [i["minute"] for i in items] == ["2026-11-05T19:00", "2026-11-05T19:01", "2026-11-05T19:02"]
    first = [r for r in rs if r["ts"].startswith("2026-11-05T19:00")]
    assert int(items[0]["count"]) == 300
    assert float(items[0]["temp_c"]["mean"]) == pytest.approx(statistics.mean(r["temp_c"] for r in first), abs=1e-3)
    assert float(items[0]["temp_c"]["max"]) == max(r["temp_c"] for r in first)
    dists = [r["dist_cm"] for r in first if r["dist_cm"] is not None]
    assert int(items[0]["dist_cm"]["n"]) == len(dists) < 300           # missing echoes are not counted as 0
    assert int(items[0]["expires"]) == int(dt.datetime(2027, 2, 3, 19, 0, tzinfo=dt.timezone.utc).timestamp())


def test_bad_readings_go_to_quarantine_with_the_reason(aws):
    rs = sim(1)
    rs[3]["temp_c"] = 900.0
    rs[4]["device"] = "pi-02"                         # pretending to be another device
    rs[5].pop("light_pct")
    body = gzip.compress(("".join(json.dumps(r) + "\n" for r in rs) + "not json\n").encode())
    aws["s3"].put_object(Bucket=BUCKET, Key=KEY, Body=body)
    out = ingest.handler({"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {"key": KEY}}}]})["processed"][0]
    assert out["bad"] == 4 and out["readings"] == 297
    q = json.loads(aws["s3"].get_object(Bucket=BUCKET, Key="quarantine/device=pi-01/date=2026-11-05/pi-01_a.bad.json")["Body"].read())
    reasons = sorted(b["reason"] for b in q["bad"])
    assert reasons == ["device does not match the upload path", "missing light_pct", "not JSON", "temp_c out of range"]
    assert int(minute_items(aws["table"])[0]["count"]) == 297


def test_the_same_event_twice_changes_nothing(aws):
    ev = put(aws, KEY, sim(2))
    ingest.handler(ev)
    before = minute_items(aws["table"])
    again = ingest.handler(ev)["processed"][0]
    assert again["merged"] == 0 and again["duplicate"] == 2
    assert minute_items(aws["table"]) == before


def test_a_minute_split_over_two_files_is_merged(aws):
    rs = [r for r in sim(1)]
    ingest.handler(put(aws, KEY, rs[:100]))                      # flushed at shutdown
    ingest.handler(put(aws, KEY.replace("_a", "_b"), rs[100:]))   # the rest after the restart
    item = minute_items(aws["table"])[0]
    assert int(item["count"]) == 300 and len(item["parts"]) == 2 and int(item["version"]) == 2
    assert float(item["temp_c"]["mean"]) == pytest.approx(statistics.mean(r["temp_c"] for r in rs), abs=1e-3)


def test_the_same_minute_uploaded_again_under_another_name_is_not_counted_twice(aws):
    rs = sim(1)
    ingest.handler(put(aws, KEY, rs))
    ingest.handler(put(aws, KEY.replace("_a", "_c"), rs))
    assert int(minute_items(aws["table"])[0]["count"]) == 300


def test_a_lost_race_is_retried(aws, monkeypatch):
    """Two Lambdas merging into the same minute: the second write sees a changed version and retries."""
    rs = sim(1)
    t = aws["table"]
    real_get, calls = t.get_item, {"n": 0}

    class Racy:
        def get_item(self, **kw):
            got = real_get(**kw)
            calls["n"] += 1
            if calls["n"] == 1:   # another Lambda writes between our read and our write
                part = ingest.summarise(rs[150:])["2026-11-05T19:00"]
                ingest.put_minute(t, "pi-01", "2026-11-05T19:00", part, 0)
            return got
        def put_item(self, **kw):
            return t.put_item(**kw)

    part = ingest.summarise(rs[:150])["2026-11-05T19:00"]
    assert ingest.put_minute(Racy(), "pi-01", "2026-11-05T19:00", part, 0) == "merged"
    assert calls["n"] == 2 and int(minute_items(t)[0]["count"]) == 300


def test_latest_is_not_overwritten_by_an_older_file(aws):
    rs = sim(2)
    ingest.handler(put(aws, KEY.replace("_a", "_new"), rs[300:]))
    ingest.handler(put(aws, KEY.replace("_a", "_old"), rs[:300]))
    latest = aws["table"].get_item(Key={"device": "pi-01", "sk": "LATEST"})["Item"]
    assert latest["ts"] == rs[-1]["ts"] and int(latest["uptime_ms"]) == rs[-1]["uptime_ms"]


def test_alarm_is_published_once_per_file(aws, monkeypatch):
    monkeypatch.setenv("ALARM_TEMP_C", "24")
    out = ingest.handler(put(aws, KEY, sim(3)))["processed"][0]
    assert out["alarm"]
    msgs = aws["sqs"].receive_message(QueueUrl=aws["queue"], MaxNumberOfMessages=10)["Messages"]
    assert len(msgs) == 1 and "temperature above 24.0 C" in json.loads(msgs[0]["Body"])["Subject"]


def test_objects_outside_raw_are_ignored(aws):
    assert ingest.handler(put(aws, "quarantine/x.jsonl.gz", sim(1)))["processed"] == []


def test_api(aws):
    ingest.handler(put(aws, KEY, sim(3)))
    r = api.handler({"queryStringParameters": {"device": "pi-01"}})
    assert r["statusCode"] == 200 and json.loads(r["body"])["ts"] == "2026-11-05T19:02:59Z"
    r = api.handler({"queryStringParameters": {"device": "pi-01", "from": "2026-11-05T19:01", "to": "2026-11-05T19:05"}})
    rows = json.loads(r["body"])["minutes"]
    assert [x["minute"] for x in rows] == ["2026-11-05T19:01", "2026-11-05T19:02"] and rows[0]["count"] == 300
    assert {"temp_c_min", "temp_c_mean", "temp_c_max", "dist_cm_mean"} <= rows[0].keys()
    assert api.handler({"queryStringParameters": {"device": "pi-99"}})["statusCode"] == 404
    for bad in ({"device": "pi 01"}, {"device": "pi-01", "from": "yesterday", "to": "2026-11-05T19:05"},
                {"device": "pi-01", "from": "2026-11-05T19:05"}, {"device": "pi-01", "from": "2026-11-05T19:05", "to": "2026-11-05T19:01"}):
        assert api.handler({"queryStringParameters": bad})["statusCode"] == 400


def test_end_to_end_device_to_api(aws, tmp_path):
    """sim logger files -> uploader -> S3 -> (S3 event) ingest -> DynamoDB -> api: 30 minutes, nothing lost."""
    sim_readings.write(tmp_path / "logs", START, minutes=30)
    up = Uploader(str(tmp_path / "logs"), str(tmp_path / "spool"), "pi-01", ZoneInfo("Europe/Budapest"),
                  aws["s3"], BUCKET, log=lambda m: None)
    assert up.run_once(flush=True)
    keys = [o["Key"] for o in aws["s3"].list_objects_v2(Bucket=BUCKET, Prefix="raw/")["Contents"]]
    for k in keys:                                         # what the S3 notification does in AWS
        ingest.handler({"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {"key": k}}}]})
    rows = json.loads(api.handler({"queryStringParameters": {"device": "pi-01", "from": "2026-11-05T19:00",
                                                             "to": "2026-11-05T19:59"}})["body"])["minutes"]
    assert len(rows) == 30 and all(r["count"] == 300 for r in rows)
    assert sum(r["count"] for r in rows) == up.stats["read"] == 9000
