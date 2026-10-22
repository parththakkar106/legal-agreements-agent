"""A minimal webhook receiver: verify each event from the Agreement Desk, then act on it.

    DESK_WEBHOOK_SECRET=... python examples/webhook_receiver.py   # listens on :9000

Point the desk at it with DESK_WEBHOOK_URLS=http://<host>:9000/. Uses only the standard library.
"""

import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.environ["DESK_WEBHOOK_SECRET"].encode()


class Receiver(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        timestamp = self.headers["X-Desk-Timestamp"]
        expected = "sha256=" + hmac.new(SECRET, f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()
        fresh = abs(time.time() - int(timestamp)) < 300  # older than 5 minutes = a replay
        if not (fresh and hmac.compare_digest(expected, self.headers["X-Desk-Signature"])):
            self.send_response(401)
            self.end_headers()
            return
        event = json.loads(body)
        # Act on it here: post to Slack, update the CRM deal, file the signed PDF ...
        print(event["event"], event["agreement"]["title"], event["agreement"]["status"], event["detail"], flush=True)
        self.send_response(204)
        self.end_headers()


HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "9000"))), Receiver).serve_forever()
