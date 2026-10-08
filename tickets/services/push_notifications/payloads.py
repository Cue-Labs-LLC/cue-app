"""Verbatim APNs payloads.

The launch-announcement copy is Apple's "Value Proposition" push template from
their Tap to Pay on iPhone marketing guide — reproduced verbatim per the spec.
Each payload is the full APNs body: an ``aps`` dict with an alert + sound.
"""


def _alert(title, body):
    return {
        'aps': {
            'alert': {'title': title, 'body': body},
            'sound': 'default',
        },
    }


# Apple §6.3 one-time launch broadcast (Value Proposition template, verbatim).
LAUNCH_ANNOUNCEMENT = _alert(
    'Tap to Pay on iPhone is here',
    'Accept contactless payments directly on your iPhone — no extra hardware needed.',
)

# Sent when an organizer's Tap to Pay status flips pending -> enabled.
TAP_TO_PAY_ENABLED = _alert(
    'Tap to Pay is ready',
    'You can now accept in-person payments at your events.',
)


def instagram_escalation_payload(category='', conversation_id=None):
    """Push body for an Instagram DM escalated to human review.

    Unlike the static constants above this carries per-conversation context: a
    ``instagram_conversation_id`` custom key (sibling to ``aps``, where APNs custom
    data lives) so the app can deep-link straight to the thread.
    """
    label = (category or 'message').replace('_', ' ')
    payload = _alert(
        'Instagram DM needs review',
        f'A DM was flagged ({label}). Tap to review and reply.',
    )
    if conversation_id:
        payload['instagram_conversation_id'] = str(conversation_id)
    return payload
