"""ingest - AWS Lambda, triggered when the Pi uploads a file to s3://BUCKET/raw/...

For every object:
  1. read it (gzip JSON lines) and check every reading (types and physical ranges);
  2. readings that fail go to s3://BUCKET/quarantine/... with the reason, nothing is dropped silently;
  3. the good ones are summarised per minute (count, sum, min, max of each quantity) and merged
     into DynamoDB, one item per device and minute; raw readings stay in S3, which is far cheaper
     than one DynamoDB write per reading (see docs/COST.md);
  4. the device's newest reading is kept in one item, SK = "LATEST";
  5. a temperature above ALARM_TEMP_C publishes one SNS message per object (optional);
  6. one CloudWatch log line in embedded metric format per object: the metrics Readings and
     BadReadings (two custom metrics; each one costs money per month, see docs/COST.md).

Exactly-once effect on at-least-once delivery: S3 may deliver an event twice, and the device
may upload the same minute twice after a crash. Each minute item keeps the digests of the
parts merged into it; a part whose digest is already there is skipped, and every write is
conditional on the version read, so two Lambdas running at once cannot lose an update.

Environment: TABLE, ALARM_TEMP_C (default 40), ALARM_TOPIC_ARN (optional), RETENTION_DAYS (90).
"""
import datetime as dt, gzip, hashlib, json, os, re, urllib.parse
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

RANGES = {"temp_c": (-40.0, 125.0), "dist_cm": (2.0, 400.0), "light_pct": (0.0, 100.0)}
QUANTITIES = ("temp_c", "dist_cm", "light_pct")

_s3 = _ddb = _sns = None


def clients():
    global _s3, _ddb, _sns
    if _s3 is None:
        _s3, _ddb, _sns = boto3.client("s3"), boto3.resource("dynamodb"), boto3.client("sns")
    return _s3, _ddb, _sns


def check(r, device=None):
    """None if the reading is valid, else the reason. device: the device the file's key belongs to."""
    if not isinstance(r, dict):
        return "not an object"
    if device is not None and r.get("device") != device:
        return "device does not match the upload path"
    for k in ("device", "ts", "uptime_ms", "temp_c", "light_pct"):
        if k not in r:
            return f"missing {k}"
    try:
        t = dt.datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return "bad ts"
    if t > dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(days=1):
        return "ts in the future"
    for q, (lo, hi) in RANGES.items():
        v = r.get(q)
        if v is None and q == "dist_cm":
            continue                                   # no echo: allowed
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return f"{q} not a number"
        if not lo <= v <= hi:
            return f"{q} out of range"
    return None


def summarise(readings):
    """Per-minute partial aggregates. Returns {minute: part}."""
    by_min = {}
    for r in readings:
        by_min.setdefault(r["ts"][:16], []).append(r)
    parts = {}
    for minute, rs in by_min.items():
        rs.sort(key=lambda r: (r["ts"], r["uptime_ms"]))
        digest = hashlib.sha256("\n".join(f'{r["ts"]},{r["uptime_ms"]}' for r in rs).encode()).hexdigest()[:16]
        p = {"digest": digest, "count": len(rs)}
        for q in QUANTITIES:
            vs = [r[q] for r in rs if r.get(q) is not None]
            p[q] = {"n": len(vs), "sum": sum(vs), "min": min(vs) if vs else None, "max": max(vs) if vs else None}
        parts[minute] = p
    return parts


def merge(item, part):
    """Merge a part into a minute item (both plain dicts, numbers as float). Returns the new item."""
    new = dict(item)
    new["count"] = item.get("count", 0) + part["count"]
    new["parts"] = list(item.get("parts", [])) + [part["digest"]]
    for q in QUANTITIES:
        old, p = item.get(q, {"n": 0, "sum": 0.0, "min": None, "max": None}), part[q]
        n = old["n"] + p["n"]
        mins = [v for v in (old["min"], p["min"]) if v is not None]
        maxs = [v for v in (old["max"], p["max"]) if v is not None]
        new[q] = {"n": n, "sum": old["sum"] + p["sum"], "min": min(mins) if mins else None,
                  "max": max(maxs) if maxs else None, "mean": (old["sum"] + p["sum"]) / n if n else None}
    return new


def to_ddb(x):
    if isinstance(x, float):
        return Decimal(str(round(x, 4)))
    if isinstance(x, dict):
        return {k: to_ddb(v) for k, v in x.items() if v is not None}
    if isinstance(x, list):
        return [to_ddb(v) for v in x]
    return x


def from_ddb(x):
    if isinstance(x, Decimal):
        return float(x)
    if isinstance(x, dict):
        return {k: from_ddb(v) for k, v in x.items()}
    if isinstance(x, list):
        return [from_ddb(v) for v in x]
    return x


def put_minute(table, device, minute, part, ttl, tries=5):
    """Merge one part into its minute item with optimistic locking. Returns 'merged' or 'duplicate'."""
    for _ in range(tries):
        got = table.get_item(Key={"device": device, "sk": f"M#{minute}"}, ConsistentRead=True).get("Item")
        old = from_ddb(got) if got else {}
        if part["digest"] in old.get("parts", []):
            return "duplicate"
        new = merge(old, part)
        new.update(device=device, sk=f"M#{minute}", minute=minute, version=int(old.get("version", 0)) + 1, expires=ttl)
        for q in QUANTITIES:
            for k in ("min", "max", "mean"):
                if new[q].get(k) is None:
                    new[q].pop(k, None)
        try:
            if got:
                table.put_item(Item=to_ddb(new), ConditionExpression="version = :v",
                               ExpressionAttributeValues={":v": int(old["version"])})
            else:
                table.put_item(Item=to_ddb(new), ConditionExpression="attribute_not_exists(sk)")
            return "merged"
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    raise RuntimeError(f"too much contention on {device} {minute}")


def put_latest(table, device, r):
    """Keep the newest reading; an older file arriving late must not overwrite a newer one."""
    order = f'{r["ts"]}#{int(r["uptime_ms"]):010d}'
    item = to_ddb({**{k: v for k, v in r.items() if k != "device"}, "device": device, "sk": "LATEST", "order": order})
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(sk) OR #o < :o",
                       ExpressionAttributeNames={"#o": "order"}, ExpressionAttributeValues={":o": order})
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def process_object(bucket, key, s3, table, sns=None, alarm_c=40.0, topic=None, retention_days=90):
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        text = gzip.decompress(body).decode()
    except (OSError, EOFError):
        text = body.decode(errors="replace")
    m = re.match(r"raw/device=([A-Za-z0-9_-]{1,32})/", key)
    key_device = m.group(1) if m else None
    good, bad = [], []
    if key_device is None:
        bad.append({"line": 0, "reason": "key is not raw/device=<id>/...", "text": key})
        text = ""
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            bad.append({"line": n, "reason": "not JSON", "text": line[:200]})
            continue
        why = check(r, key_device)
        if why:
            bad.append({"line": n, "reason": why, "text": line[:200]})
        else:
            good.append(r)
    if bad:
        qkey = "quarantine/" + key.split("/", 1)[-1].replace(".jsonl.gz", "") + ".bad.json"
        s3.put_object(Bucket=bucket, Key=qkey, Body=json.dumps({"source": key, "bad": bad}, indent=1).encode(),
                      ContentType="application/json")
    result = {"key": key, "readings": len(good), "bad": len(bad), "merged": 0, "duplicate": 0, "alarm": False}
    devices = {r["device"] for r in good}
    for device in sorted(devices):
        rs = [r for r in good if r["device"] == device]
        for minute, part in sorted(summarise(rs).items()):
            ttl = int((dt.datetime.strptime(minute, "%Y-%m-%dT%H:%M") + dt.timedelta(days=retention_days))
                      .replace(tzinfo=dt.timezone.utc).timestamp())
            result[put_minute(table, device, minute, part, ttl)] += 1
        put_latest(table, device, max(rs, key=lambda r: (r["ts"], r["uptime_ms"])))
        hot = [r for r in rs if r["temp_c"] > alarm_c]
        if hot and topic and sns:
            sns.publish(TopicArn=topic, Subject=f"{device}: temperature above {alarm_c} C",
                        Message=f"{len(hot)} reading(s) above {alarm_c} C, highest {max(r['temp_c'] for r in hot)} C "
                                f"at {max(hot, key=lambda r: r['temp_c'])['ts']} (file {key})")
        result["alarm"] = result["alarm"] or bool(hot)
    print(json.dumps({"_aws": {"Timestamp": int(dt.datetime.now().timestamp() * 1000), "CloudWatchMetrics": [{
        "Namespace": "SensorPipeline", "Dimensions": [[]], "Metrics": [
            {"Name": "Readings", "Unit": "Count"}, {"Name": "BadReadings", "Unit": "Count"}]}]},
        "Readings": result["readings"], "BadReadings": result["bad"], "MinutesMerged": result["merged"],
        "key": key}))
    return result


def handler(event, context=None):
    s3, ddb, sns = clients()
    table = ddb.Table(os.environ["TABLE"])
    out = []
    for rec in event.get("Records", []):
        bucket = rec["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(rec["s3"]["object"]["key"])
        if not key.startswith("raw/"):
            continue
        out.append(process_object(bucket, key, s3, table, sns,
                                  float(os.environ.get("ALARM_TEMP_C", "40")),
                                  os.environ.get("ALARM_TOPIC_ARN") or None,
                                  int(os.environ.get("RETENTION_DAYS", "90"))))
    return {"processed": out}
