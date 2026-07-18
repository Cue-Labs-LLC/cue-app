"""HMAC-SHA256 signing for outbound webhook deliveries.

The signed content binds the routing metadata (timestamp, event type, delivery
id) to the body so none of it is tamperable independently of the signature:

    signed_content = f"{timestamp}.{event_type}.{delivery_id}.".encode() + body
    signature      = hex( HMAC_SHA256(secret, signed_content) )

Receivers recompute HMAC over the same bytes (reading event type and delivery id
from the signed headers, NOT trusting them blindly) and reject if the timestamp
is outside a tolerance window (recommend ~300s) to prevent replay.

Headers:
    Content-Type:      application/json
    X-Cue-Event:       <event_type>       (covered by the signature)
    X-Cue-Delivery-Id: <delivery_id>      (covered by the signature; stable across retries — dedupe key)
    X-Cue-Timestamp:   <unix_ts>          (per-attempt send time)
    X-Cue-Signature:   t=<unix_ts>,v1=<hex signature>
"""

import hashlib
import hmac
import json
import time


def canonical_body(payload: dict) -> bytes:
    """Serialize a payload to the exact bytes that get signed and POSTed."""
    return json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('utf-8')


def compute_signature(secret: str, timestamp: str, event_type: str, delivery_id: str, body: bytes) -> str:
    signed_content = f"{timestamp}.{event_type}.{delivery_id}.".encode('utf-8') + body
    return hmac.new(secret.encode('utf-8'), signed_content, hashlib.sha256).hexdigest()


def build_signed_request(secret: str, event_type: str, delivery_id: str, payload: dict, timestamp=None):
    """Return (body_bytes, headers) for a signed delivery.

    `delivery_id` is stable across retries of the same delivery so consumers can
    dedupe at-least-once redeliveries. `timestamp` is injectable for
    deterministic tests; defaults to now.
    """
    body = canonical_body(payload)
    ts = str(int(time.time())) if timestamp is None else str(timestamp)
    signature = compute_signature(secret, ts, event_type, delivery_id, body)
    headers = {
        'Content-Type': 'application/json',
        'X-Cue-Event': event_type,
        'X-Cue-Delivery-Id': delivery_id,
        'X-Cue-Timestamp': ts,
        'X-Cue-Signature': f"t={ts},v1={signature}",
    }
    return body, headers
