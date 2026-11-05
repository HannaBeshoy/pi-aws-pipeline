"""api - AWS Lambda behind a function URL (IAM-authenticated): read the readings back.

    GET ?device=pi-01                                  -> the newest reading
    GET ?device=pi-01&from=2026-11-05T19:00&to=2026-11-05T20:00
                                                       -> one row per minute: count, min/mean/max

Environment: TABLE.
"""
import json, os, re
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

MINUTE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d$")
DEVICE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_table = None


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE"])
    return _table


def plain(x):
    if isinstance(x, Decimal):
        return int(x) if x == x.to_integral_value() else float(x)
    if isinstance(x, dict):
        return {k: plain(v) for k, v in x.items()}
    if isinstance(x, list):
        return [plain(v) for v in x]
    return x


def latest(t, device):
    item = t.get_item(Key={"device": device, "sk": "LATEST"}).get("Item")
    return plain({k: v for k, v in item.items() if k not in ("sk", "order")}) if item else None


def minutes(t, device, frm, to, limit=1440):
    rows, kw = [], {"KeyConditionExpression": Key("device").eq(device) & Key("sk").between(f"M#{frm}", f"M#{to}")}
    while len(rows) < limit:
        page = t.query(**kw)
        for it in page["Items"]:
            it = plain(it)
            row = {"minute": it["minute"], "count": it["count"]}
            for q in ("temp_c", "dist_cm", "light_pct"):
                for k in ("min", "mean", "max"):
                    if k in it.get(q, {}):
                        row[f"{q}_{k}"] = round(it[q][k], 2)
            rows.append(row)
        if "LastEvaluatedKey" not in page:
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return rows[:limit]


def respond(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def handler(event, context=None):
    q = event.get("queryStringParameters") or {}
    device = q.get("device", "")
    if not DEVICE.match(device):
        return respond(400, {"error": "device must be 1-32 letters, digits, _ or -"})
    frm, to = q.get("from"), q.get("to")
    if frm is None and to is None:
        r = latest(table(), device)
        return respond(200, r) if r else respond(404, {"error": f"no readings from {device}"})
    if not (frm and to and MINUTE.match(frm) and MINUTE.match(to) and frm <= to):
        return respond(400, {"error": "from and to must both be given as YYYY-MM-DDTHH:MM (UTC), from <= to"})
    return respond(200, {"device": device, "minutes": minutes(table(), device, frm, to)})
