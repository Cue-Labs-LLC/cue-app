"""SSRF guard for outbound webhook URLs.

An org admin supplies an arbitrary URL that a Celery worker then POSTs to. Without
validation that's a server-side request forgery vector: an endpoint pointed at
`http://169.254.169.254/` (cloud metadata) or `http://localhost:6379` (internal
services) would let a tenant reach the server's private network and read the
response back out of the delivery log.

We enforce, both at save time (`WebhookEndpoint.clean`) and again at send time
(defense in depth against DNS rebinding and rows created before validation):

  - scheme must be https (http allowed only when settings.DEBUG)
  - the host must not resolve to a private / loopback / link-local / reserved IP
"""

import ipaddress
import socket
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_webhook_url(url: str, *, allow_http: bool = None) -> None:
    """Raise ValidationError if `url` is unsafe to deliver to."""
    if allow_http is None:
        allow_http = bool(getattr(settings, 'DEBUG', False))

    parsed = urlparse(url or '')
    scheme = (parsed.scheme or '').lower()
    allowed_schemes = {'https'} | ({'http'} if allow_http else set())
    if scheme not in allowed_schemes:
        raise ValidationError(
            'Webhook URL must use https://'
            + (' (http:// allowed only in local dev).' if not allow_http else '.')
        )

    host = parsed.hostname
    if not host:
        raise ValidationError('Webhook URL must include a host.')

    default_port = 443 if scheme == 'https' else 80
    try:
        infos = socket.getaddrinfo(host, parsed.port or default_port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValidationError('Webhook URL host could not be resolved.')

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _ip_is_blocked(ip):
            raise ValidationError(
                'Webhook URL resolves to a private, loopback, or reserved address, '
                'which is not allowed.'
            )


def is_webhook_url_allowed(url: str) -> bool:
    """Boolean form for the delivery path (never raises)."""
    try:
        validate_webhook_url(url)
        return True
    except ValidationError:
        return False
