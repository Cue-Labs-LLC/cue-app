"""Apple Push Notification (APNs) sending for the Cue iOS apps.

Layout mirrors ``tickets/services/webhooks/``:

- ``apns``     — low-level HTTP/2 sender + ES256 provider-JWT auth (hand-rolled
                 on ``httpx`` + ``PyJWT``, same spirit as the webhook HMAC signer).
- ``payloads`` — the verbatim notification bodies (§6.3 launch, Tap to Pay ready).
- ``dispatch`` — fan a payload out to every device token in an organization via
                 the ``send_push_notification_task`` Celery task.
"""
