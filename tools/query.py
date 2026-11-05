#!/usr/bin/env python3
"""query.py - call the API function URL, signed with your AWS credentials (it is not public).

    python3 tools/query.py URL pi-01                                       # newest reading
    python3 tools/query.py URL pi-01 2026-11-05T19:00 2026-11-05T20:00     # per-minute table (UTC)
"""
import json, sys, urllib.parse, urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def call(url, params, region=None):
    session = boto3.Session()
    region = region or session.region_name or "us-east-1"
    full = url.rstrip("/") + "/?" + urllib.parse.urlencode(params)
    req = AWSRequest(method="GET", url=full)
    SigV4Auth(session.get_credentials().get_frozen_credentials(), "lambda", region).add_auth(req)
    with urllib.request.urlopen(urllib.request.Request(full, headers=dict(req.headers))) as r:
        return json.loads(r.read())


def main(argv):
    if len(argv) not in (2, 4):
        print(__doc__); return 2
    url, device = argv[0], argv[1]
    if len(argv) == 2:
        print(json.dumps(call(url, {"device": device}), indent=1)); return 0
    rows = call(url, {"device": device, "from": argv[2], "to": argv[3]})["minutes"]
    print(f"{'minute (UTC)':17} {'n':>4} {'temp min':>8} {'mean':>6} {'max':>6} {'dist mean':>9} {'light':>6}")
    for r in rows:
        print(f"{r['minute']:17} {r['count']:4d} {r.get('temp_c_min', ''):8} {r.get('temp_c_mean', ''):6} "
              f"{r.get('temp_c_max', ''):6} {r.get('dist_cm_mean', ''):9} {r.get('light_pct_mean', ''):6}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
