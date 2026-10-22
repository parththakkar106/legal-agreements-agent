"""Outbound webhooks: tell other systems what happened, signed so they can trust it.

Every entry in an agreement's audit trail is also an event (see store.add_event):
  version.sent, version.received, review.completed, review.failed, round.closed,
  agreement.signed, plus agreement.status_changed (e.g. to "ready to sign").
Each is POSTed as JSON to every DESK_WEBHOOK_URLS address, in a background thread,
with up to 3 tries. Nothing is sent unless DESK_WEBHOOK_SECRET is set.

Signing works like Stripe's and GitHub's: the receiver recomputes
HMAC-SHA256(secret, "<timestamp>.<body>") and compares it with X-Desk-Signature,
and rejects old timestamps so a captured request cannot be replayed (see verify()).
The events carry ids, statuses and short summaries, never contract text.
"""

import hashlib
import hmac
import json
import logging
import time
import uuid
from threading import Thread

import httpx

from legal_agent import config

log = logging.getLogger(__name__)
EVENTS = {"sent": "version.sent", "received": "version.received", "reviewed": "review.completed",
          "review failed": "review.failed", "round closed": "round.closed", "sign": "agreement.signed"}
ATTEMPTS = 3
TOLERANCE_SECONDS = 300


def publish(event: str, agreement: dict, detail: str = "", **extra):
    """Send one event to every configured URL, without making the caller wait."""
    urls, secret = config.webhook_urls(), config.webhook_secret()
    if not urls:
        return
    if not secret:
        log.warning("Webhook %s not sent: DESK_WEBHOOK_SECRET is not set.", event)
        return
    link = f"{config.public_url()}/api/v1/agreements/{agreement['id']}"
    body = json.dumps({"id": str(uuid.uuid4()), "event": event, "occurred_at": int(time.time()),
                       "agreement": {**agreement, "url": link}, "detail": detail, **extra})
    for url in urls:
        Thread(target=deliver, args=(url, event, body, secret), daemon=True).start()


def deliver(url: str, event: str, body: str, secret: str) -> bool:
    for attempt in range(ATTEMPTS):
        timestamp = str(int(time.time()))
        headers = {"Content-Type": "application/json", "X-Desk-Event": event, "X-Desk-Timestamp": timestamp,
                   "X-Desk-Signature": signature(secret, timestamp, body)}
        try:
            if 200 <= post(url, body, headers) < 300:
                return True
        except httpx.HTTPError as error:
            log.warning("Webhook %s to %s failed: %s", event, url, error)
        if attempt < ATTEMPTS - 1:
            time.sleep(2 ** attempt)  # 1 s, then 2 s
    log.error("Webhook %s to %s gave up after %d tries.", event, url, ATTEMPTS)
    return False


def post(url: str, body: str, headers: dict) -> int:
    return httpx.post(url, content=body, headers=headers, timeout=10).status_code


def signature(secret: str, timestamp: str, body: str) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify(secret: str, timestamp: str, body: str, received: str, now: float | None = None) -> bool:
    """What a receiver runs: the signature matches and the event is recent."""
    if abs((now or time.time()) - int(timestamp)) > TOLERANCE_SECONDS:
        return False
    return hmac.compare_digest(signature(secret, timestamp, body), received)
