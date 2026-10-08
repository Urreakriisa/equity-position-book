"""Web push for price alerts, ported from Tlalocai (Estacion Virreyes).

Same design: a VAPID key pair created on first use and kept with the app's
state (never in the repository), one subscription per device, delivery in a
background thread so a slow push service can never hold up a price refresh,
and dead endpoints (404/410) pruned. The differences: state lives in this
app's database instead of a /data volume, and subscription endpoints must
belong to a known browser push service.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from urllib.parse import urlparse

from .store import Store

log = logging.getLogger("push")
MAX_SUBS = 50
# The push services behind Chrome/Edge/Android, Firefox, Safari/iOS and Windows.
PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com",
              "web.push.apple.com", "notify.windows.com", "push.apple.com")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _webpush(**kw):                       # imported lazily; replaced in tests
    from pywebpush import webpush
    return webpush(**kw)


def clean_subscription(sub) -> dict | None:
    """Accept only a well-formed subscription for a real push service."""
    if not isinstance(sub, dict):
        return None
    endpoint, keys = sub.get("endpoint"), sub.get("keys")
    if not isinstance(endpoint, str) or len(endpoint) > 1000 or not isinstance(keys, dict):
        return None
    url = urlparse(endpoint)
    host = (url.hostname or "").lower()
    if url.scheme != "https" or not any(host == h or host.endswith("." + h) for h in PUSH_HOSTS):
        return None
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not all(isinstance(k, str) and 0 < len(k) < 200 for k in (p256dh, auth)):
        return None
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


class Push:
    def __init__(self, store: Store, subject: str):
        self.store, self.subject = store, subject
        self._lock = threading.Lock()

    # ---- keys ---------------------------------------------------------------
    def _vapid(self) -> dict:
        with self._lock:
            rec = self.store.get("vapid")
            if not rec:
                from cryptography.hazmat.primitives import serialization
                from py_vapid import Vapid
                v = Vapid()
                v.generate_keys()
                rec = {
                    "private": _b64(v.private_key.private_numbers().private_value.to_bytes(32, "big")),
                    "public": _b64(v.public_key.public_bytes(serialization.Encoding.X962,
                                                             serialization.PublicFormat.UncompressedPoint)),
                    "created": time.time(),
                }
                self.store.put("vapid", rec)
            return rec

    def public_key(self) -> str:
        return self._vapid()["public"]

    # ---- subscriptions ------------------------------------------------------
    def subscriptions(self) -> list[dict]:
        return (self.store.get("push_subs") or {}).get("items", [])

    def subscribe(self, sub: dict) -> int:
        with self._lock:
            subs = [s for s in self.subscriptions() if s["endpoint"] != sub["endpoint"]]
            subs.append(sub)
            self.store.put("push_subs", {"items": subs[-MAX_SUBS:]})
            return len(subs[-MAX_SUBS:])

    def unsubscribe(self, endpoint: str) -> int:
        with self._lock:
            subs = [s for s in self.subscriptions() if s["endpoint"] != endpoint]
            self.store.put("push_subs", {"items": subs})
            return len(subs)

    # ---- sending ------------------------------------------------------------
    def deliver(self, title: str, body: str, tag: str, only: str | None = None) -> dict:
        """Send to every subscribed device (or just `only`). Returns counts."""
        subs = [s for s in self.subscriptions() if only is None or s["endpoint"] == only]
        key, sent, failed, gone = self._vapid()["private"], 0, 0, []
        for s in subs:
            try:
                _webpush(subscription_info=s,
                         data=json.dumps({"title": title, "body": body, "tag": tag}),
                         vapid_private_key=key, vapid_claims={"sub": self.subject},
                         ttl=4 * 3600, timeout=10, headers={"Urgency": "high"})
                sent += 1
            except Exception as exc:                      # noqa: BLE001 - a push must never raise
                failed += 1
                code = getattr(getattr(exc, "response", None), "status_code", None)
                if code in (404, 410):                    # the device unsubscribed
                    gone.append(s["endpoint"])
                else:
                    log.warning("push failed (%s): %s", code, type(exc).__name__)
        for endpoint in gone:
            self.unsubscribe(endpoint)
        result = {"sent": sent, "failed": failed, "devices": len(subs), "at": time.time(), "title": title}
        self.store.put("push_last", result)
        return result

    def send(self, title: str, body: str, tag: str) -> None:
        """Fire and forget, off the caller's thread."""
        if self.subscriptions():
            threading.Thread(target=self.deliver, args=(title, body, tag), daemon=True).start()
