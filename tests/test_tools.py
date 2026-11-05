"""The cost model and the signed API client."""
import http.server, json, threading

from helpers import load_module
import cost  # noqa: E402  (helpers puts tools/ on the path)

query = load_module("query", "tools/query.py")


def test_per_minute_summaries_are_much_cheaper_than_one_item_per_reading():
    per_minute = sum(cost.monthly(1, 300)[0].values())
    per_reading = sum(cost.monthly(1, 300, per_reading_ddb=True)[0].values())
    assert per_minute < 1.0 < 10 < per_reading


def test_compression_and_file_count():
    raw, gz = cost.bytes_per_reading()
    assert 100 < raw < 150 and gz < raw / 5
    assert cost.monthly(1, 300)[1]["files"] == 8640          # 12 an hour, 30 days


def test_query_signs_the_request():
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["auth"], seen["path"] = self.headers.get("Authorization", ""), self.path
            body = json.dumps({"device": "pi-01", "ts": "2026-11-05T19:02:59Z"}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    r = query.call(f"http://127.0.0.1:{srv.server_port}", {"device": "pi-01"}, region="us-east-1")
    assert r["ts"] == "2026-11-05T19:02:59Z"
    assert seen["auth"].startswith("AWS4-HMAC-SHA256 Credential=testing/") and "/us-east-1/lambda/aws4_request" in seen["auth"]
    assert seen["path"] == "/?device=pi-01"
